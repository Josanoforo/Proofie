"""One closed vocabulary for every status word the seller sees.

DISPLAY ONLY. Nothing here writes, calls PayPal or decides whether a write may happen. A status from this module
may be used to REFUSE or to explain a write; it must never be used to PERMIT one - every server guard (approval
fingerprint, attempt registry, write guard, PayPal's own action links) stays the only authority.

States come from stored PayPal responses (the attempt registry, resolution rows, tracking rows) and from the
dispute PayPal returned; never from model text. One table per domain, US spelling. An unknown code gives
'Status not recognized' (warn), never a raw enum.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core import send_service
from copilot.db import repo
from copilot.web.dispute_state import BUYER_CLOSED_LABEL, buyer_closed_raw

UNKNOWN_LABEL = "Status not recognized"
MISSING_FILE_PREFIX = "missing_attachment:"


@dataclass(frozen=True)
class StatusView:
    code: str
    label: str
    kind: str                       # a tag() state: ok | warn | bad | na | info | todo | due
    needs_seller: bool = False


def _view(table: dict, code: str, needs_seller: bool = False) -> StatusView:
    entry = table.get(code)
    if entry is None:
        return StatusView(str(code), UNKNOWN_LABEL, "warn")
    kind, label = entry
    return StatusView(code, label, kind, needs_seller)


# ----------------------------------------------------------------------------------------- the dispute
DISPUTE_TABLE = {
    "waiting_for_you": ("todo", "Waiting for your response"),
    "waiting_for_buyer": ("info", "Waiting for the buyer"),
    "settling": ("info", "PayPal is updating this dispute"),
    "claim_under_review": ("na", "PayPal is reviewing this claim"),
    "under_review": ("na", "Under review by PayPal"),
    "just_opened": ("info", "Just opened"),
    "resolved": ("na", "Resolved"),
    "buyer_closed": ("na", BUYER_CLOSED_LABEL),
    "unknown": ("warn", "PayPal returned a status Proofie does not know. Check the dispute in PayPal."),
}
_PAYPAL_STATUS = {"WAITING_FOR_SELLER_RESPONSE": "waiting_for_you", "WAITING_FOR_BUYER_RESPONSE": "waiting_for_buyer",
                  "UNDER_REVIEW": "under_review", "OPEN": "just_opened", "RESOLVED": "resolved"}


def is_settling(norm) -> bool:
    """PayPal shows UNDER_REVIEW with no link but `self` for roughly a minute after any seller write.
    Own copy of that rule (normalize() already drops `self`); no feature import."""
    if not isinstance(norm, dict):
        return False
    return norm.get("status") == "UNDER_REVIEW" and not norm.get("action_links")


def dispute_status(norm, raw=None) -> StatusView:
    norm = norm if isinstance(norm, dict) else {}
    if buyer_closed_raw(raw):
        return _view(DISPUTE_TABLE, "buyer_closed")
    status = norm.get("status")
    if status == "UNDER_REVIEW":
        if config.feature_enabled("outcome") and norm.get("stage") == "CHARGEBACK":
            return _view(DISPUTE_TABLE, "claim_under_review")
        if is_settling(norm):
            return _view(DISPUTE_TABLE, "settling")
    code = _PAYPAL_STATUS.get(status, "unknown")
    return _view(DISPUTE_TABLE, code, needs_seller=code == "waiting_for_you")


# ------------------------------------------------------------------------------------- the send record
@dataclass(frozen=True)
class SendRecord:
    state: str = "none"             # none | reserved | attempted | partial | confirmed | refused | blocked | abandoned
    attempt: dict | None = None
    confirmed_at: str | None = None
    attachment_filenames: tuple = ()
    attachments_match: bool = True
    content_matches: bool = False
    approval_sha256: str | None = None
    missing_files: tuple = ()       # partial: the files PayPal does not list; confirmed + mismatch: the files added after
    sent_at: str | None = None      # the time of the send itself (the audit entry that registered the attempt)
    changed_after_sending: bool = False   # the send was confirmed although a source changed while sending (audit flag)
    package_id: int | None = None   # the package the attempt is registered for (audit), None for a legacy attempt
    sent_document_ids: tuple = ()   # the attached documents (ids) the attempt carried, never matched by file name
    removed_files: tuple = ()       # files the attempt carried (by sha256) that are no longer attached: in PayPal, not here
    sent_shas: tuple = ()           # the sha256 values the attempt carried (documents are marked sent by these, never by name)
    removed_unlisted: tuple = ()    # the removed files that PayPal's read did not list, or that cannot be told apart: "was sent", never "is in PayPal"
    readback: dict | None = None    # confirmed only: {evidence_type, documents_present, paypal_date, read_at} from the read that confirmed it;
                                    # None for any other state and for an attempt written before the record was kept


NO_RECORD = SendRecord()
_STORE_STATE = {"pending": "reserved", "sent_unconfirmed": "attempted", "confirmed": "confirmed", "failed": "refused",
                "blocked": "blocked", "abandoned": "abandoned"}


def send_record(db_path, dispute_id: str, package: dict | None) -> SendRecord:
    """The PayPal-facing send history of ONE package. Attempts are mapped to the package through the audit entry
    `send_attempt_registered` (detail.package_id -> request_id -> attempt row), so a later source change cannot hide
    an earlier confirmed send and another package's attempt never speaks for this one; attempts with no such entry
    fall back to the package's current content sha. Files are compared by sha256 (an attempt that predates the sha
    records, by name)."""
    if not package:
        return NO_RECORD
    store = SQLiteAttemptStore(db_path)
    try:
        attempts = store.list_by_dispute(dispute_id)
    finally:
        store.close()
    if not attempts:
        return NO_RECORD
    registered = send_service.registered_attempts(db_path, dispute_id)
    attempt = send_service.pick_attempt(send_service.package_attempts(db_path, dispute_id, package, attempts, registered))
    if attempt is None:
        return NO_RECORD
    state = _STORE_STATE.get(attempt["state"], "none")
    reason = str(attempt.get("reason") or "")
    if attempt["state"] == "sent_unconfirmed" and reason.startswith(MISSING_FILE_PREFIX):
        state = "partial"
    owner, detail = registered.get(attempt.get("request_id"), (None, {}))
    sent = send_service.sent_files_of(attempt, detail)
    rows = repo.list_attachments(db_path, package["id"])
    carried, unsent = send_service.split_rows(sent, rows)
    # "match" means every file attached now was carried by the attempt. A file that was sent and is no
    # longer attached is in PayPal; it is worded on its own (removed_files), never as a file added afterwards.
    match = not unsent
    removed = tuple(send_service.removed_files(sent, rows))
    missing = ()
    if state == "partial":
        missing = tuple(n for n in reason[len(MISSING_FILE_PREFIX):].split(",") if n)
    elif state == "confirmed" and not match:
        missing = tuple(r["filename"] for r in unsent)
    # "is in PayPal" only for a file PayPal's read listed. A partial send lists the files it
    # missed; an attempt that predates the sha records cannot be tied to a document, so it never claims it either.
    if state == "partial":
        unlisted = tuple(n for n in removed if n in set(missing))
    elif state == "confirmed" and sent.shas is None:
        unlisted = removed
    elif state == "confirmed":
        unlisted = ()
    else:
        unlisted = removed
    sent_at = next((e.get("ts") for e in repo.list_audit(db_path, dispute_id)
                    if e.get("action") == "send_attempt_registered" and isinstance(e.get("detail"), dict)
                    and e["detail"].get("request_id") == attempt.get("request_id")), None)
    changed = any(e.get("action") == "send_confirmed" and isinstance(e.get("detail"), dict)
                  and e["detail"].get("package_id") == package["id"] and e["detail"].get("changed_after_sending")
                  for e in repo.list_audit(db_path, dispute_id))
    current = send_service.content_sha(db_path, dispute_id, package["id"])
    return SendRecord(state=state, attempt=attempt,
                      confirmed_at=attempt.get("ts") if state == "confirmed" else None,
                      attachment_filenames=sent.names, attachments_match=match,
                      content_matches=current is not None and attempt["package_sha256"] == current,
                      approval_sha256=detail.get("approval_sha256"), missing_files=missing,
                      sent_at=sent_at, changed_after_sending=changed, package_id=owner,
                      sent_document_ids=tuple(r["document_id"] for r in carried),
                      removed_files=removed, sent_shas=tuple(sent.shas or ()), removed_unlisted=unlisted,
                      readback=attempt.get("readback") if state == "confirmed" else None)


@dataclass(frozen=True)
class Collision:
    """The statements of a package are already held by PayPal (or may be) through an EARLIER package. With other files the
    send path refuses this package with "Same statements, other files". With the SAME files
    (same_files) it is the same submission: the send path never posts it a second time (a confirmed twin is answered
    "Package already received", an unconfirmed one only reconciles), so the page offers no Send either."""
    state: str                      # confirmed | attempted | partial (PayPal listed the statements, not a file)
    earlier_version: int | None
    earlier_package_id: int | None
    unsent: tuple = ()              # the files of this package that the earlier send did not carry
    same_files: bool = False        # the earlier send carried exactly the files this package has now
    missing: tuple = ()             # state partial: the files PayPal's read did not list (from the attempt's reason)

    @property
    def unconfirmed(self) -> bool:
        """The earlier send is not confirmed: nothing says PayPal holds it whole (attempted, or partial)."""
        return self.state in ("attempted", "partial")


def statement_collision(db_path, dispute_id: str, package: dict | None) -> Collision | None:
    """The same test the send path makes at its claim conflict (copilot/core/send_service._send_locked), read only: the
    registry row for this package's exact statements and tracking belongs to another package and carried other files.
    Display only; the send path keeps its own refusal."""
    if not package:
        return None
    sha = send_service.content_sha(db_path, dispute_id, package["id"])
    if sha is None:
        return None
    store = SQLiteAttemptStore(db_path)
    try:
        existing = store.read(dispute_id, sha)
    finally:
        store.close()
    if not existing or not existing.get("request_id") or existing["state"] not in ("confirmed", "sent_unconfirmed"):
        return None
    owner, detail = send_service.registered_attempts(db_path, dispute_id).get(existing["request_id"], (None, {}))
    if owner in (None, package["id"]):
        return None
    rows = repo.list_attachments(db_path, package["id"])
    sent = send_service.sent_files_of(existing, detail)
    same_files = send_service.files_match(sent, rows)
    earlier = repo.get_package(db_path, owner)
    reason = str(existing.get("reason") or "")
    state, missing = ("confirmed" if existing["state"] == "confirmed" else "attempted"), ()
    if state == "attempted" and reason.startswith(MISSING_FILE_PREFIX):
        # the earlier send reached PayPal without a file (the read-back listed the statements, not every file);
        # the way out is the same, the words are not ("not there" is false for it).
        state, missing = "partial", tuple(n for n in reason[len(MISSING_FILE_PREFIX):].split(",") if n)
    return Collision(state, earlier["version"] if earlier else None, owner,
                     tuple(send_service.unsent_files(sent, rows)), same_files, missing)


def latest_package_record(db_path, dispute_id: str, packages) -> tuple:
    """(package, SendRecord) for the highest version, or (None, None)."""
    packages = [p for p in (packages or []) if p]
    if not packages:
        return None, None
    latest = max(packages, key=lambda p: (p.get("version") or 0, p.get("id") or 0))
    return latest, send_record(db_path, dispute_id, latest)


# ------------------------------------------------------------------------------- evidence and package
EVIDENCE_TABLE = {
    "not_sent": ("todo", "Not sent yet"),
    "approved": ("info", "Approved, not sent yet"),
    "reserved": ("warn", "Sending started - not confirmed yet"),
    "attempted": ("warn", "Send attempted - not confirmed by PayPal"),
    "partial": ("warn", "Statements in PayPal, file missing"),
    "later_file": ("warn", "Confirmed in PayPal, a file was added afterwards"),
    "confirmed": ("ok", "Received by PayPal (confirmed by reading the dispute back)"),
    "refused": ("bad", "PayPal did not accept it"),
    "blocked": ("bad", "Not sent: Proofie stopped it"),
    "abandoned": ("bad", "You marked it as not shown in PayPal"),      # red on every page, like the outcome history
}
PACKAGE_TABLE = {
    "draft": ("todo", "Draft"),
    "draft_attention": ("bad", "Draft: needs attention"),
    "approved": ("info", "Approved, not sent"),
    "attempted": ("warn", "Send attempted, not confirmed by PayPal"),
    "partial": ("warn", "Statements in PayPal, file missing"),
    "later_file": ("warn", "Sent, a file was added afterwards"),
    "sent": ("ok", "Sent to PayPal"),
    "refused": ("bad", "PayPal did not accept it"),
    "blocked": ("bad", "Not sent: Proofie stopped it"),
    "abandoned": ("bad", "You marked it as not shown in PayPal"),
}


def effective_state(record) -> str:
    """The record's state as the seller should see it. A confirmed send whose attached files no longer match what PayPal
    received is its own state, `later_file`: PayPal confirmed it, a file added afterwards is not in
    PayPal. It is neither pending nor a partial send."""
    state = getattr(record, "state", "none") if record is not None else "none"
    if state == "confirmed" and not record.attachments_match:
        return "later_file"
    return state


_effective_state = effective_state


def is_sent_record(record) -> bool:
    """One rule for "this version was sent and can never change": PayPal holds its statements (confirmed, including a
    confirmed send with a file added afterwards, or a true partial send). Every page and route that refuses an edit or an
    approval of a sent version uses this."""
    return getattr(record, "state", "none") in ("confirmed", "partial")


ABANDONED_PARTIAL_LABEL = "You marked it as not received; when Proofie checked, PayPal showed the statements without the file"


def abandoned_partial(record) -> bool:
    """An abandoned attempt whose reason is send_service.ABANDONED_PARTIAL_REASON was a true partial send (PayPal listed
    the statements, not every file) that the seller then marked as not received."""
    reason = str((getattr(record, "attempt", None) or {}).get("reason") or "")
    return getattr(record, "state", None) == "abandoned" and reason == send_service.ABANDONED_PARTIAL_REASON


def evidence_status(record, approval_valid: bool = False) -> StatusView:
    state = _effective_state(record)
    if state == "none":
        return _view(EVIDENCE_TABLE, "approved" if approval_valid else "not_sent")
    if abandoned_partial(record):
        return StatusView("abandoned", ABANDONED_PARTIAL_LABEL, EVIDENCE_TABLE["abandoned"][0])
    return _view(EVIDENCE_TABLE, state)


def package_status(record, approval_valid: bool = False, has_conflict: bool = False,
                   missing_required: bool = False) -> StatusView:
    state = _effective_state(record)
    if state == "none":
        if approval_valid:
            return _view(PACKAGE_TABLE, "approved")
        if has_conflict or missing_required:
            return _view(PACKAGE_TABLE, "draft_attention")
        return _view(PACKAGE_TABLE, "draft")
    if abandoned_partial(record):
        return StatusView("abandoned", ABANDONED_PARTIAL_LABEL, PACKAGE_TABLE["abandoned"][0])
    code = {"reserved": "attempted", "confirmed": "sent"}.get(state, state)
    return _view(PACKAGE_TABLE, code)


# --------------------------------------------------------------------------- messages and tracking
MESSAGE_TABLE = {
    "sent_pending_settle": ("warn", "PayPal accepted the request; not confirmed yet"),
    "sent_confirmed_message": ("ok", "PayPal lists your message"),
    "sent_confirmed_offer": ("ok", "PayPal no longer shows the refund-offer option"),
    "sent_unconfirmed": ("warn", "Send attempted - not confirmed by PayPal"),
    "failed": ("bad", "Not sent: PayPal returned an error"),
    "refused_422": ("bad", "Not sent: PayPal was still updating the dispute"),
    "rejected_hash": ("bad", "Not sent: the approval did not match"),
    "rejected_link": ("bad", "Not sent: PayPal did not offer this option at that moment"),
    "rejected_body": ("bad", "Not sent: the request was not valid"),
    "read_failed": ("warn", "Not sent: PayPal could not be read"),
}


LEGACY_CONFIRMED_LABEL = "Confirmed by reading the dispute back (message not checked)"


def _message_listed(row: dict):
    """True only when the row's stored read-back says PayPal's list held the message. A row confirmed
    before that check existed has no such key. Reads the row only; no import from the resolution feature."""
    readback = row.get("readback")
    if readback is None:
        try:
            readback = json.loads(row.get("readback_json") or "null")
        except (TypeError, ValueError):
            readback = None
    return readback.get("message_listed") if isinstance(readback, dict) else None


def message_status(row) -> StatusView:
    row = row if isinstance(row, dict) else {}
    status = row.get("status")
    key = status
    if status == "sent_confirmed":
        action = row.get("action") or row.get("kind")
        key = "sent_confirmed_" + action if action in ("offer", "message") else None
        if action == "message" and _message_listed(row) is not True:
            # Proofie never saw PayPal list this message: say what was true (the dispute was read back), nothing more.
            return StatusView(str(status), LEGACY_CONFIRMED_LABEL, "info")
    view = _view(MESSAGE_TABLE, key)
    return StatusView(str(status), view.label, view.kind)


TRACKING_TABLE = {
    "registered": ("ok", "Registered and confirmed"),
    "registered_unconfirmed": ("warn", "Accepted by PayPal, not confirmed"),
    "unknown": ("warn", "Send attempted - not confirmed by PayPal"),
    "failed": ("bad", "Not accepted by PayPal"),
    "saved_without_registration": ("info", "Saved in Proofie only"),
}


def tracking_status(status) -> StatusView:
    return _view(TRACKING_TABLE, status)


DOMAIN_TABLES = {"dispute": DISPUTE_TABLE, "evidence": EVIDENCE_TABLE, "package": PACKAGE_TABLE,
                 "message": MESSAGE_TABLE, "tracking": TRACKING_TABLE}
