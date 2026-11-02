"""Reconciliation and crash/startup recovery for send attempts recorded in AttemptStore.
Every function here only ever GETs and updates state - the one POST
that actually sends evidence lives in copilot/core/send_service.py (called from copilot/web/routes_send.py), never in this module. Reused from
pilot/step5_send_evidence.py's verify_after via copilot/core/send.py's verify_after_with_attachments,
kept pure here so both the post-send polling and the on-entry recovery path share one source of truth for
"does PayPal's own GET show our exact content AND every attached file".
"""
from __future__ import annotations

import time

import httpx

from copilot.core.disputes import PayPalError, readback_audit
from copilot.core.send import (MISSING_ATTACHMENT_PREFIX, PARTIAL_MESSAGE, PARTIAL_MESSAGE_MANY, readback_record,
                               verify_after_with_attachments)
from copilot.db import repo

RECEIVED_MESSAGE = "Received by PayPal"
PENDING_MESSAGE = "Result pending — Check again"
# PARTIAL_MESSAGE / PARTIAL_MESSAGE_MANY are defined in copilot/core/send.py (imported above and re-exported here).
PARTIAL_TITLES = (PARTIAL_MESSAGE, PARTIAL_MESSAGE_MANY)


def partial_message(missing) -> str:
    """The partial headline for the files PayPal does not list: plural from two names, singular for one or none named.
    Display only: nothing decides a state from it, the result state comes from the title's membership in
    PARTIAL_TITLES."""
    return PARTIAL_MESSAGE_MANY if len(missing or ()) >= 2 else PARTIAL_MESSAGE


def reconcile_after_send(client, store, dispute_id: str, package_sha: str, req_input: dict,
                         attachment_filenames: list | None = None, delays: tuple = (5, 15, 30), sleep=None,
                         get_op: str = "post_send_get") -> dict:
    """Up to len(delays) fresh GETs, sleeping `delay` seconds before each, stopping at the first one
    where PayPal's own evidences[] shows our exact content. NEVER re-POSTs - only GETs and records state.

    When there are attachments, reaching content_ok also stops the polling immediately (waiting longer
    cannot change whether a file rode along in the POST that already landed) and files_ok is checked: all
    present -> 'confirmed'; the content landed but at least one file did not -> state stays
    'sent_unconfirmed' (a known, specific gap, not the generic ambiguity PENDING_MESSAGE
    describes) and the caller must show PARTIAL_MESSAGE, never silently retry the send.

    Exhausting every delay without content_ok at all leaves the attempt 'sent_unconfirmed' (ambiguous, not
    failed) and the caller must show PENDING_MESSAGE.

    sleep defaults to time.sleep, looked up on the `time` MODULE at call time (not captured at def time)
    so a test can monkeypatch copilot.core.send_recovery.time.sleep and have it take effect here without
    needing every caller to thread a fake through explicitly."""
    sleep = sleep or time.sleep
    attachment_filenames = attachment_filenames or []
    for delay in delays:
        sleep(delay)
        try:
            raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", get_op,
                              log_extra=readback_audit(package_sha))
        except (PayPalError, httpx.TransportError):
            # PayPal being unreachable for one of the delayed checks is exactly as uninformative as
            # "not shown yet" - try again at the next delay rather than crashing; exhausting every delay
            # (whether from this or a genuine non-match) already falls through to the ambiguous return below.
            continue
        result = verify_after_with_attachments(raw, req_input, attachment_filenames)
        if not result["content_ok"]:
            continue
        if result["files_ok"]:
            store.write(dispute_id, package_sha, {"state": "confirmed", "request_input": req_input,
                                                  "attachment_filenames": attachment_filenames,
                                                  "readback": readback_record(result["details"])})
            return {"confirmed": True, "files_ok": True, "missing_files": [], "details": result["details"]}
        # Never downgrade a row some concurrent caller already settled - 'confirmed'
        # (its own GET, at nearly the same moment, landed on every file while this one's did not) or
        # 'abandoned' (a DIFFERENT package's confirm-not-received call can target THIS exact package_sha
        # as the "other" one it abandons, while this send's own reconciliation is still polling it) - to
        # the ambiguous 'sent_unconfirmed'/partial state. Conditional write and effective
        # result share one SQLite transaction (also used by send_service's claim-conflict branch).
        current = store.write_partial(dispute_id, package_sha,
                   {"state": "sent_unconfirmed", "request_input": req_input,
                    "attachment_filenames": attachment_filenames,
                    "reason": MISSING_ATTACHMENT_PREFIX + ",".join(result["missing_files"])})
        if current and current["state"] == "confirmed":
            return {"confirmed": True, "files_ok": True, "missing_files": [], "details": result["details"]}
        if current and current["state"] == "abandoned":
            return {"confirmed": False, "files_ok": False, "missing_files": [], "details": None,
                    "abandoned": True}
        return {"confirmed": False, "files_ok": False, "missing_files": result["missing_files"],
               "details": result["details"]}
    return {"confirmed": False, "files_ok": None, "missing_files": [], "details": None}


def recover_dispute(client, store, dispute_id: str, db_path, get_op: str = "recovery_get") -> list:
    """Startup/crash recovery: every 'sent_unconfirmed' attempt for this dispute gets ONE fresh GET check
    (not the multi-delay polling - that is reconcile_after_send's job, right after a send; this runs on
    ordinary entry to the dispute, so it must not block for up to 50s). An attempt confirmed by this check
    (content AND every attached file present) is updated in place; an attempt whose content landed but is
    missing a file is left exactly as found (still 'sent_unconfirmed', already carrying its
    'missing_attachment:...' reason from reconcile_after_send) - polling again will not add a file that
    was never POSTed. Returns the attempts still unconfirmed afterwards, for the caller to show a banner -
    this function itself never blocks further action, it only tells the truth about what it saw.

    An attempt with no stored request_input (should not happen for anything written after this file
    existed, but defensively: an older row, or a 'pending' claim that crashed before the content was ever
    recorded) cannot be checked against PayPal's evidences[] and is returned as still-pending unchanged.

    A GET failure here (PayPal briefly unreachable) must never raise - this runs both right before
    a send (copilot/core/send_service.py) and on ordinary dispute-page views (copilot/web/
    routes_disputes.py); leaving the attempt exactly as found and moving on is always safe, since nothing
    is written unless PayPal's own GET actually confirms it.

    It only ever confirms an attempt THIS app actually POSTed (request_id set) - content that merely
    looks right from a row with no request_id is never enough (see copilot/core/send_service.py's own
    stricter check at its claim-conflict branch for the fuller rationale).

    An 'abandoned' attempt (the vendor explicitly confirmed PayPal's Resolution
    Center did not show it, copilot/core/send_service.confirm_other_attempts_abandoned) is ALSO re-checked
    here - if PayPal's own GET now shows it after all (a late-landing POST), it is promoted to 'confirmed'
    and the fact that it was abandoned first is kept, audited, never silently overwritten unremarked.
    PayPal's own truth always wins; abandoning it was never a claim about what PayPal actually did."""
    still_pending = []
    for att in store.list_by_dispute(dispute_id):
        if att["state"] not in ("sent_unconfirmed", "abandoned"):
            continue
        req_input = att.get("request_input")
        if req_input is None:
            if att["state"] == "sent_unconfirmed":
                still_pending.append(att)
            continue
        try:
            raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", get_op,
                              log_extra=readback_audit(att["package_sha256"]))
        except (PayPalError, httpx.TransportError):
            if att["state"] == "sent_unconfirmed":
                still_pending.append(att)
            continue
        result = verify_after_with_attachments(raw, req_input, att.get("attachment_filenames") or [])
        if result["content_ok"] and result["files_ok"] and att.get("request_id"):
            store.write(dispute_id, att["package_sha256"], {"state": "confirmed", "request_input": req_input,
                                                             "attachment_filenames": att.get("attachment_filenames"),
                                                             "readback": readback_record(result["details"])})
            if att["state"] == "abandoned":
                repo.log_action(db_path, "system", "abandoned_attempt_later_confirmed_by_paypal", dispute_id,
                                {"package_sha256": att["package_sha256"]})
        elif att["state"] == "sent_unconfirmed":
            still_pending.append(att)
        # an 'abandoned' row that still does not show in PayPal's GET stays 'abandoned' - never added to
        # still_pending (the vendor already dismissed it; this must not resurrect that banner).
    return still_pending


__all__ = ["RECEIVED_MESSAGE", "PENDING_MESSAGE", "PARTIAL_MESSAGE", "PARTIAL_MESSAGE_MANY", "PARTIAL_TITLES",
           "partial_message", "reconcile_after_send", "recover_dispute"]
