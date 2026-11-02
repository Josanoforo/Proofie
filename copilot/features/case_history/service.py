"""The case history: what Proofie recorded for one dispute, in time order, with four kinds kept apart in words.

local     work done in Proofie: a package prepared, a statement edited, an approval given or cleared.
attempt   a send attempt Proofie registered before it called PayPal.
response  what the PayPal write call answered (a refusal, or no usable answer).
read      a later read of PayPal's dispute record, with its time and its source.

Everything is read from rows the app already keeps (packages, approvals, the audit log, the send-attempt registry).
Nothing is written, nothing calls PayPal, and a PayPal state is shown only from a recorded PayPal read or answer.
Hashes and file lists are returned apart from the sentence of each event so the page can fold them away.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.db import repo
from copilot.web import presentation, status, timefmt

KINDS = ("local", "attempt", "response", "read")
KIND_WORDS = {"local": "Local work", "attempt": "Send attempted", "response": "PayPal response",
              "read": "Later read from PayPal"}
_SOURCE_WORDS = {"ai": "with the local assistant", "human_edit": "by hand", "auto_manual": "as an empty shell"}
# Audit actions that another row already shows (a package, an approval) or that are not about this case's work.
_SKIP = {"generate_package", "start_manual_package", "start_manual_package_ai_unavailable",
         "start_manual_package_no_documents", "approve_package", "invalidate_active_approval",
         "invalidate_approval_after_edit", "send_attempt_registered", "send_failed", "send_outcome_unknown",
         "send_confirmed", "webhook_event"}
_READ_WORDS = {"send_result_pending": ("PayPal's record did not yet list the evidence when Proofie read it",
                                       "PayPal dispute record"),
               "send_missing_attachment": ("PayPal's record did not list every attached file when Proofie read it",
                                           "PayPal dispute record"),
               "abandoned_attempt_later_confirmed_by_paypal":
                   ("A later read of PayPal's record listed an attempt that had been marked as not received",
                    "PayPal dispute record"),
               "fulfilment_checked": ("Proofie read the tracking registration in PayPal", "PayPal tracking record")}
_FAR = datetime(1, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Event:
    at: str
    kind: str
    text: str
    detail: tuple = ()            # plain lines, safe to show beside the sentence
    hashes: tuple = ()            # ((label, value), ...) shown on demand
    source: str = ""              # for a read: where the information came from


@dataclass(frozen=True)
class Version:
    number: int
    package_id: int
    created_at: str
    how: str
    approval: str
    send_label: str
    send_kind: str


def _key(event: Event):
    return timefmt.instant(event.at) or _FAR


def _v(versions: dict, package_id) -> str:
    version = versions.get(package_id)
    return f"package v{version}" if version else "a package"


def _short(value) -> str:
    return value if isinstance(value, str) and value else "Not recorded"


def build(db_path, dispute_id: str) -> dict:
    packages = repo.list_packages(db_path, dispute_id)
    versions = {p["id"]: p["version"] for p in packages}
    audit = repo.list_audit(db_path, dispute_id)
    store = SQLiteAttemptStore(db_path)
    try:
        attempts = store.list_by_dispute(dispute_id)
    finally:
        close = getattr(store, "close", None)
        if close:
            close()
    registered = send_service.registered_attempts(db_path, dispute_id)
    readbacks = {}                                   # package id -> the read-back of its latest confirmed attempt
    for attempt in attempts:
        owner = registered.get(attempt.get("request_id"), (None, {}))[0]
        if attempt.get("state") == "confirmed" and owner is not None and isinstance(attempt.get("readback"), dict):
            readbacks[owner] = attempt["readback"]
    invalidations = {}                               # approval id -> the audit action that cleared it
    for entry in audit:
        if entry["action"] in ("invalidate_approval_after_edit", "invalidate_active_approval"):
            invalidations[(entry.get("detail") or {}).get("approval_id")] = entry["action"]

    events, rows = [], []
    for package in packages:
        number = package["version"]
        how = _SOURCE_WORDS.get(package["source"], "")
        events.append(Event(package["created_at"], "local", f"Prepared package v{number}" + (f" {how}" if how else "")))
        approvals = repo.list_approvals(db_path, package["id"])
        for approval in approvals:
            note = " with a written note on open notices" if approval.get("sufficiency_override") else ""
            events.append(Event(approval["approved_at"], "local", f"You approved package v{number}{note}",
                                hashes=(("Approved content hash (SHA-256)", _short(approval["approval_sha256"])),)))
            if approval.get("invalidated_at"):
                why = ("because a source changed" if invalidations.get(approval["id"]) == "invalidate_active_approval"
                       else "because the package was edited")
                events.append(Event(approval["invalidated_at"], "local",
                                    f"The approval of package v{number} was cleared {why}",
                                    detail=("The earlier approval is not restored; approve again after reviewing.",)))
        record = status.send_record(db_path, dispute_id, package)
        valid = next((a for a in reversed(approvals) if not a.get("invalidated_at")), None)
        view = status.evidence_status(record, approval_valid=valid is not None)
        rows.append(Version(number, package["id"], package["created_at"], how or "prepared",
                            "Approved" if valid else ("Approval cleared" if approvals else "Not approved"),
                            view.label, view.kind))

    for entry in audit:
        action, detail = entry["action"], entry.get("detail") or {}
        owner = _v(versions, detail.get("package_id"))
        if action == "send_attempt_registered":
            files = tuple(detail.get("attachments") or ())
            hashes = [("Approved content hash (SHA-256)", _short(detail.get("approval_sha256")))]
            hashes += [(f"File {i} hash (SHA-256)", _short(sha))
                       for i, sha in enumerate(detail.get("attachment_sha256") or (), 1)]
            if detail.get("request_id"):
                hashes.append(("Request id", _short(detail.get("request_id"))))
            events.append(Event(entry["ts"], "attempt", f"A send attempt for {owner} was recorded before PayPal was called",
                                detail=(("Files: " + ", ".join(str(f) for f in files),) if files else ()),
                                hashes=tuple(hashes)))
        elif action == "send_failed":
            code = detail.get("http_status")
            events.append(Event(entry["ts"], "response",
                                f"PayPal refused the send of {owner}" + (f" (HTTP {code})" if isinstance(code, int) else ""),
                                detail=(("PayPal's issue code: " + str(detail["issue"]),) if detail.get("issue") else ())))
        elif action == "send_outcome_unknown":
            code = detail.get("http_status")
            events.append(Event(entry["ts"], "response",
                                f"No usable answer from PayPal to the send of {owner}"
                                + (f" (HTTP {code})" if isinstance(code, int) else ""),
                                detail=("Nothing was sent again automatically.",)))
        elif action == "send_confirmed":
            back = readbacks.get(detail.get("package_id")) or {}
            files = back.get("documents_present")
            events.append(Event(back.get("read_at") or entry["ts"], "read",
                                f"PayPal's dispute record listed the evidence from {owner}",
                                detail=(("Files listed: " + ", ".join(str(f) for f in files),)
                                        if isinstance(files, list) and files else ()),
                                source="PayPal dispute record, read after the send"))
        elif action == "outcome_appeal":
            state = detail.get("status")
            events.append(Event(entry["ts"], "attempt", "An appeal attempt was recorded",
                                hashes=(("Request hash (SHA-256)", _short(detail.get("request_sha256"))),)))
            if state == "failed":
                events.append(Event(entry["ts"], "response", "The appeal was rejected",
                                    detail=("Review the rejection before considering another attempt; no automatic resend.",)))
            elif state == "sent_unconfirmed":
                events.append(Event(entry["ts"], "response", "The appeal outcome is uncertain",
                                    detail=("Refresh and review the recorded state; no automatic resend.",)))
            elif state == "sent_confirmed":
                events.append(Event(entry["ts"], "read", "A later dispute read showed a change after the appeal",
                                    detail=("Review the recorded outcome before another action.",),
                                    source="Recorded appeal readback"))
            else:
                events.append(Event(entry["ts"], "response", "The recorded appeal result is unknown",
                                    detail=("Review the attempt before another action; no automatic resend.",)))
        elif action in _READ_WORDS:
            text, source = _READ_WORDS[action]
            events.append(Event(entry["ts"], "read", text, source=source))
        elif action not in _SKIP:
            events.append(Event(entry["ts"], "local", presentation.action_label(action)))
    events.sort(key=_key)
    return {"events": events, "versions": rows, "kinds": KIND_WORDS}
