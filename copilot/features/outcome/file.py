"""What Proofie sent, from the existing tables. Reads only; no model, no network.

Sources: packages, claims, attachments and the latest valid approval (copilot.db.repo), the send attempts
(SQLiteAttemptStore.list_by_dispute), the audit lines for the dispute (repo.list_audit). Two optional cross-feature
reads, each a lazy import inside try/except ImportError (the other feature packages may be absent from a tree): the
pre-dispute tracker file (fulfilment lookup, by the dispute's transaction id) and the offers and messages the
resolution feature recorded (its store)."""
from __future__ import annotations

from dataclasses import dataclass, field

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.db import repo


@dataclass(frozen=True)
class SentFile:
    packages: tuple = ()             # dicts: id, version, created_at, validation_ok, approval_sha256, claims, attachments
    send_attempts: tuple = ()        # dicts: package_sha256, state, http_status, ts
    audit: tuple = ()                # dicts: id, ts, actor, action
    tracker: dict | None = None
    resolution_actions: tuple | None = None   # None: the resolution feature is not importable here
    notes: tuple = field(default=())


def _package_view(db_path, pkg: dict) -> dict:
    approval = repo.get_latest_valid_approval(db_path, pkg["id"])
    return {"id": pkg["id"], "version": pkg.get("version"), "created_at": pkg.get("created_at"),
            "validation_ok": pkg.get("validation_ok"), "review_flags": list(pkg.get("review_flags") or []),
            "approval_sha256": approval["approval_sha256"] if approval else None,
            "claims": tuple({"text": c["text"], "edited": bool(c.get("edited_by_human"))}
                            for c in repo.list_claims(db_path, pkg["id"])),
            "attachments": tuple({"filename": a["filename"], "sha256": a["sha256"], "size_bytes": a["size_bytes"]}
                                 for a in repo.list_attachments(db_path, pkg["id"]))}


def sent_file(db_path, dispute_id: str, capture_id: str | None = None) -> SentFile:
    packages = tuple(_package_view(db_path, p) for p in repo.list_packages(db_path, dispute_id))
    store = SQLiteAttemptStore(db_path)
    try:
        attempts = tuple({"package_sha256": a["package_sha256"], "state": a["state"], "http_status": a.get("http_status"),
                          "ts": a["ts"], "attachment_filenames": list(a.get("attachment_filenames") or [])}
                         for a in store.list_by_dispute(dispute_id))
    finally:
        store.close()
    audit = tuple({"id": e["id"], "ts": e["ts"], "actor": e["actor"], "action": e["action"]}
                  for e in repo.list_audit(db_path, dispute_id))
    notes = []
    tracker = None
    if capture_id:
        try:
            from copilot.features.fulfilment.lookup import file_for_transaction
        except ImportError:
            notes.append("The fulfilment feature is not installed here, so the pre-dispute file was not looked up.")
        else:
            tracker = file_for_transaction(capture_id)
            if tracker is not None:
                # the status the fulfilment pages show (a legacy `failed` row whose outcome is unknown reads `unknown`)
                from copilot.features.fulfilment import store as fulfilment_store
                tracker = {**tracker, "status": fulfilment_store.effective_status(fulfilment_store.get_file(capture_id))
                           or tracker["status"]}
    resolution = None
    try:
        from copilot.features.resolution.store import list_actions, readback_of
    except ImportError:
        notes.append("The resolution feature is not installed here, so offers and messages were not looked up.")
    else:
        resolution = tuple({"action": r["action"], "status": r["status"], "created_at": r["created_at"],
                            "request_sha256": r["request_sha256"],
                            "released": bool(readback.get("released")),      # PayPal's own read did not list it
                            # status.message_status says "PayPal lists your message" only with this flag
                            "readback": {"message_listed": readback.get("message_listed")}}
                           for r in list_actions(dispute_id) for readback in (readback_of(r),))
    return SentFile(packages, attempts, audit, tracker, resolution, tuple(notes))
