"""The tracker PayPal registers for the tracking number this app sends in a provide-evidence request.

Observed in the sandbox: GET trackers answers `[]` before the send; after it, one MERCHANT tracker with exactly
the number we sent, status SHIPPED. That is the send's OWN effect, not something PayPal or the buyer reported, so:
  * the post-send comparison of the approval must not read it as a foreign change (OwnTrackerMask, whole entry removed);
  * the checks on the pages must not read its SHIPPED as "not delivered" against a document that says delivered
    (own_tracker_client, only the `status` of the entry removed: the number and carrier stay visible and compared).

Core only: no feature import, no table is created by a read."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing


def sent_tracking_numbers(request_input: dict | None) -> list:
    """The tracking numbers inside the evidence request this app sends (evidence_info.tracking_info)."""
    return [t.get("tracking_number") for e in (request_input or {}).get("evidences", [])
            for t in ((e.get("evidence_info") or {}).get("tracking_info") or []) if t.get("tracking_number")]


def tracker_numbers(answer) -> list:
    """The tracking numbers of a PayPal trackers answer (`{"trackers": [...]}`)."""
    items = answer.get("trackers") if isinstance(answer, dict) else None
    return [t["tracking_number"] for t in items or [] if isinstance(t, dict) and t.get("tracking_number")]


class OwnTrackerMask:
    """PayPal registers the tracking number of a provide-evidence request as a tracker of the
    transaction (observed in the sandbox: GET trackers [] before the send, one MERCHANT tracker with
    exactly the number we sent, status SHIPPED, last_updated at the second of the POST, after it). That is the send's
    OWN effect, so the post-send comparison must not report it as a foreign change.

    The ONE field excluded: a tracker entry of the transaction's trackers answer (the context_tracking read) that was
    NOT present before the send (`baseline`) and whose tracking_number is one this app sent in the evidence
    (`sent`). Nothing else is touched: a tracker with another number, a tracker that already existed before the send
    (whatever changes in it), a changed transaction fact, document, statement or evidence entry still differs. The
    dispute's own status, stage, links and allowed options are NOT excluded because they never enter the checks or
    the review digest (verified by replaying the recorded answers: only the tracker flipped tracking_number, carrier and
    delivery_status). The pre-send check is not loosened: this wrapper is used only after the send or on a finished
    reservation whose approval recorded the baseline.

    `status_only=True` keeps that entry in the answer and drops only its `status` (what the pages use: the own tracker's
    number and carrier still show and compare, its SHIPPED just is not a delivery report).

    v1.1.2, a second sandbox fact (take-902, copilot_web.jsonl rows 14 and 261): when the tracker of the number we send
    ALREADY exists, PayPal sets it back to SHIPPED at the second of the POST (it read DELIVERED four minutes before).
    That reset is the send's own effect too. `approved_status` maps a tracking number to the status PayPal showed when
    the seller approved: an entry that was there before the send, whose number we sent, whose status is now SHIPPED
    and was something else at approval, is read with the status of the approval. Any other change of an existing
    tracker (another status, another carrier, another number) still differs."""
    def __init__(self, client, sent, baseline, status_only=False, approved_status=None):
        self._client, self._sent, self._baseline = client, set(sent), set(baseline)
        self._status_only = status_only
        self._approved_status = {k: v for k, v in (approved_status or {}).items() if isinstance(v, str) and v != "SHIPPED"}

    def _reset(self, entry) -> bool:
        number = entry.get("tracking_number") if isinstance(entry, dict) else None
        return (number in self._sent and number in self._baseline and entry.get("status") == "SHIPPED"
                and number in self._approved_status)

    def _restored(self, result):
        if not any(self._reset(t) for t in result["trackers"]):
            return result
        return {**result, "trackers": [{**t, "status": self._approved_status[t["tracking_number"]]} if self._reset(t) else t
                                       for t in result["trackers"]]}

    def _own(self, entry) -> bool:
        return (isinstance(entry, dict) and entry.get("tracking_number") in self._sent
                and entry.get("tracking_number") not in self._baseline)

    def call(self, method, path, op, **kwargs):
        result = self._client.call(method, path, op, **kwargs)
        if op == "context_tracking" and isinstance(result, dict) and isinstance(result.get("trackers"), list):
            result = self._restored(result)
            if self._status_only:
                if any(self._own(t) and "status" in t for t in result["trackers"]):
                    return {**result, "trackers": [{k: v for k, v in t.items() if k != "status"} if self._own(t) else t
                                                   for t in result["trackers"]]}
                return result
            kept = [t for t in result["trackers"] if not self._own(t)]
            if len(kept) != len(result["trackers"]):
                return {**result, "trackers": kept}
        return result


def _confirmed_sends(db_path, dispute_id: str) -> list:
    """The request_input of every provide-evidence attempt PayPal's own read-back confirmed on this dispute."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='send_attempts'").fetchone():
            return []
        rows = conn.execute("SELECT request_input_json FROM send_attempts WHERE dispute_id=? AND state='confirmed'",
                            (dispute_id,)).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


def _approval_statuses(db_path, dispute_id: str) -> dict:
    """number -> the tracker status PayPal showed when an approval of this dispute was recorded (latest approval wins)."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='active_approval_receipts'").fetchone():
            return {}
        rows = conn.execute("SELECT r.confirmations_json FROM active_approval_receipts r "
                            "JOIN approvals a ON a.id = r.approval_id JOIN packages p ON p.id = a.package_id "
                            "WHERE p.dispute_id=? ORDER BY a.id", (dispute_id,)).fetchall()
    statuses = {}
    for (raw,) in rows:
        try:
            recorded = json.loads(raw)[0]["approved_case_evidence"].get("tracker_status")
        except (TypeError, ValueError, KeyError, IndexError, AttributeError):
            continue
        if isinstance(recorded, dict):
            statuses.update({k: v for k, v in recorded.items() if isinstance(k, str) and isinstance(v, str)})
    return statuses


def _approval_baselines(db_path, dispute_id: str) -> set:
    """Every tracker number any approval of this dispute recorded as already registered when it was approved."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='active_approval_receipts'").fetchone():
            return set()
        rows = conn.execute("SELECT r.confirmations_json FROM active_approval_receipts r "
                            "JOIN approvals a ON a.id = r.approval_id JOIN packages p ON p.id = a.package_id "
                            "WHERE p.dispute_id=?", (dispute_id,)).fetchall()
    numbers = set()
    for (raw,) in rows:
        try:
            confirmations = json.loads(raw)
            numbers |= {n for n in confirmations[0]["approved_case_evidence"]["trackers"] if isinstance(n, str)}
        except (TypeError, ValueError, KeyError, IndexError):
            continue        # a receipt of an earlier release has no trackers baseline: it adds nothing
    return numbers


def own_tracker_client(client, db_path, dispute_id: str):
    """`client` itself unless this dispute has a send PayPal confirmed; then the client whose trackers answer carries no
    status on the entry of a number we sent that was not registered when the seller approved. A foreign tracker, and a
    tracker that was already there at approval, keep their status. Before the first confirmed send nothing changes."""
    sent = {n for request_input in _confirmed_sends(db_path, dispute_id) for n in sent_tracking_numbers(request_input)}
    if not sent:
        return client
    return OwnTrackerMask(client, sent, _approval_baselines(db_path, dispute_id), status_only=True,
                          approved_status=_approval_statuses(db_path, dispute_id))
