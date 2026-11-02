"""Outcome-owned, action-specific appeal receipts; short transactions, no connection during transport."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import secrets
import sqlite3
import time

from copilot import config
from copilot.db import repo

TTL_SECONDS = 300
DDL = """
CREATE TABLE IF NOT EXISTS outcome_appeal_receipts (
 id TEXT PRIMARY KEY, action TEXT NOT NULL CHECK(action='appeal'),
 dispute_id TEXT NOT NULL, package_id INTEGER NOT NULL, package_version INTEGER NOT NULL,
 binding_sha256 TEXT NOT NULL, local_sha256 TEXT NOT NULL, input_json TEXT NOT NULL,
 actor TEXT NOT NULL CHECK(actor='vendor'), created_at REAL NOT NULL, expires_at REAL NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ready','invalidated','reserved','used')),
 reserved_at REAL, outcome TEXT, source_changed INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS outcome_appeal_one_reservation
 ON outcome_appeal_receipts(dispute_id,package_id) WHERE state='reserved';
CREATE UNIQUE INDEX IF NOT EXISTS outcome_appeal_dispute_reservation
 ON outcome_appeal_receipts(dispute_id) WHERE state='reserved';
"""


class ReceiptError(ValueError):
    """No current explicit approval for this exact appeal; no POST is permitted."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@contextmanager
def transaction(db_path):
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(DDL)
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def local_snapshot(db_path, dispute_id, package_id, conn=None):
    if conn is None:
        opened = sqlite3.connect(str(db_path), timeout=5)
        try:
            opened.execute("BEGIN")
            return local_snapshot(db_path, dispute_id, package_id, opened)
        finally:
            opened.close()
    queries = (
        ("SELECT * FROM packages WHERE id=?", (package_id,)),
        ("SELECT * FROM claims WHERE package_id=? ORDER BY id", (package_id,)),
        ("SELECT * FROM documents WHERE dispute_id=? ORDER BY id", (dispute_id,)),
        ("SELECT c.* FROM document_confirmations c JOIN documents d ON d.id=c.document_id "
         "WHERE d.dispute_id=? ORDER BY c.id", (dispute_id,)),
        ("SELECT * FROM manual_fields WHERE dispute_id=?", (dispute_id,)),
        ("SELECT * FROM package_attachments WHERE package_id=? ORDER BY document_id", (package_id,)),
    )
    return digest([[list(row) for row in conn.execute(sql, args)] for sql, args in queries])


def current_input(db_path, dispute_id, package_id, raw=None):
    from copilot.features.outcome import service
    before = local_snapshot(db_path, dispute_id, package_id)
    prepared = service.prepare_appeal(db_path, dispute_id, package_id)
    package = repo.get_package(db_path, package_id)
    if prepared.body is None or not package or local_snapshot(db_path, dispute_id, package_id) != before:
        raise ReceiptError("The appeal or its sources changed. Prepare and review it again.")
    version = package["version"]
    if type(version) is not int or version < 1:
        raise ReceiptError("The package version is invalid. Nothing was sent.")
    value = {"action": "appeal", "dispute_id": dispute_id, "package_id": package_id,
             "package_version": version, "body_sha256": prepared.body_sha256,
             "fingerprint": prepared.sha256, "local_sha256": before,
             "attachments": sorted([{"sha256": hashlib.sha256(a["data"]).hexdigest(),
                                      "filename": a["filename"], "content_type": a["content_type"]}
                                     for a in prepared.attachments], key=lambda a: (a["sha256"], a["filename"], a["content_type"]))}
    if raw is not None:
        value["remote_sha256"] = digest(raw)
    return prepared, value


def review(client, db_path, dispute_id, package_id):
    from datetime import datetime, timezone
    from copilot.core.disputes import normalize
    from copilot.features.outcome.appeal import _link_path, READ_OP
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", READ_OP)
    path, reason = _link_path(normalize(raw, datetime.now(timezone.utc)), dispute_id)
    if path is None:
        raise ReceiptError(reason)
    prepared, value = current_input(db_path, dispute_id, package_id, raw)
    return prepared, value, digest(value)


def expired(row, now):
    return not (row["created_at"] <= now < row["expires_at"] <= row["created_at"] + TTL_SECONDS)


def approve(db_path, value, submitted_sha):
    if value.get("action") != "appeal":
        raise ReceiptError("Only this appeal action can be approved here.")
    binding = digest(value)
    if not secrets.compare_digest(binding, submitted_sha):
        raise ReceiptError("The reviewed appeal or its sources changed. Review again before approving.")
    with transaction(db_path) as conn:
        # Sample after serialization: a queued caller may have observed an older
        # clock before another caller inserted the ready receipt under this lock.
        now = time.time()
        if local_snapshot(db_path, value["dispute_id"], value["package_id"], conn) != value["local_sha256"]:
            raise ReceiptError("The local sources changed before approval. Review again.")
        if conn.execute("SELECT 1 FROM outcome_appeal_receipts WHERE dispute_id=? AND state='reserved'",
                        (value["dispute_id"],)).fetchone():
            raise ReceiptError("An appeal attempt is already reserved. Do not retry it.")
        previous = conn.execute("SELECT * FROM outcome_appeal_receipts WHERE binding_sha256=? ORDER BY created_at DESC",
                                (binding,)).fetchall()
        if any(r["state"] in ("reserved", "used") for r in previous):
            raise ReceiptError("This appeal was already attempted. Do not retry it.")
        for row in previous:
            if row["state"] == "ready" and not expired(row, now):
                return row["id"]
        conn.execute("UPDATE outcome_appeal_receipts SET state='invalidated' WHERE dispute_id=? AND package_id=? AND state='ready'",
                     (value["dispute_id"], value["package_id"]))
        receipt_id = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO outcome_appeal_receipts "
                     "(id,action,dispute_id,package_id,package_version,binding_sha256,local_sha256,input_json,actor,created_at,expires_at,state) "
                     "VALUES (?,'appeal',?,?,?,?,?,?,'vendor',?,?,'ready')",
                     (receipt_id, value["dispute_id"], value["package_id"], value["package_version"], binding,
                      value["local_sha256"], json.dumps(value, sort_keys=True), now, now + TTL_SECONDS))
        repo.log_action(db_path, "vendor", "approve_outcome_appeal", value["dispute_id"],
                        {"package_id": value["package_id"], "binding_sha256": binding}, conn=conn)
        return receipt_id


def receipt(db_path, receipt_id):
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='outcome_appeal_receipts'").fetchone():
            return None
        row = conn.execute("SELECT * FROM outcome_appeal_receipts WHERE id=?", (receipt_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def invalidate(db_path, receipt_id):
    with transaction(db_path) as conn:
        conn.execute("UPDATE outcome_appeal_receipts SET state='invalidated' WHERE id=? AND state='ready'", (receipt_id,))


def precheck(db_path, dispute_id, package_id, receipt_id, body, attachments):
    from copilot.features.outcome.appeal import approval_sha256, attachment_sha256s
    row = receipt(db_path, receipt_id)
    if not row or row["action"] != "appeal" or row["dispute_id"] != dispute_id or row["package_id"] != package_id or row["state"] != "ready":
        raise ReceiptError("Approve this exact appeal first. A different action's approval cannot authorize it.")
    _, local = current_input(db_path, dispute_id, package_id)
    expected = json.loads(row["input_json"])
    supplied = sorted([{"sha256": hashlib.sha256(a["data"]).hexdigest(),
                        "filename": a["filename"], "content_type": a["content_type"]}
                       for a in attachments], key=lambda a: (a["sha256"], a["filename"], a["content_type"]))
    if supplied != local["attachments"] or expired(row, time.time()) or any(expected.get(k) != v for k, v in local.items()) or local["fingerprint"] != approval_sha256(dispute_id, body, attachment_sha256s(attachments)):
        invalidate(db_path, receipt_id)
        raise ReceiptError("The approval expired or its sources changed. Prepare and approve the appeal again.")
    return row


def reserve(db_path, receipt_id, value):
    error = False
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM outcome_appeal_receipts WHERE id=?", (receipt_id,)).fetchone()
        if not row or row["state"] != "ready":
            raise ReceiptError("This appeal receipt is missing or was already consumed. Do not retry.")
        if conn.execute("SELECT 1 FROM outcome_appeal_receipts WHERE dispute_id=? AND state='reserved'",
                        (row["dispute_id"],)).fetchone():
            raise ReceiptError("An appeal attempt is already reserved for this dispute. Do not retry it.")
        if expired(row, time.time()) or row["binding_sha256"] != digest(value) or local_snapshot(db_path, value["dispute_id"], value["package_id"], conn) != value["local_sha256"]:
            conn.execute("UPDATE outcome_appeal_receipts SET state='invalidated' WHERE id=?", (receipt_id,))
            error = True
        else:
            conn.execute("UPDATE outcome_appeal_receipts SET state='reserved',reserved_at=? WHERE id=? AND state='ready'",
                         (time.time(), receipt_id))
            repo.log_action(db_path, "system", "reserve_outcome_appeal", value["dispute_id"],
                            {"package_id": value["package_id"], "binding_sha256": row["binding_sha256"]}, conn=conn)
    if error:
        raise ReceiptError("The appeal or its fresh sources changed. Review and approve it again.")


def finish(db_path, receipt_id, original, result):
    try:
        _, current = current_input(db_path, original["dispute_id"], original["package_id"])
        changed = current["local_sha256"] != original["local_sha256"]
    except (ReceiptError, OSError, sqlite3.Error):
        changed = True
    with transaction(db_path) as conn:
        conn.execute("UPDATE outcome_appeal_receipts SET state='used',outcome=?,source_changed=? WHERE id=? AND state='reserved'",
                     (result.status if result is not None else "unknown", int(changed), receipt_id))
        repo.log_action(db_path, "system", "finish_outcome_appeal", original["dispute_id"],
                        {"package_id": original["package_id"], "source_changed": changed,
                         "status": result.status if result else "unknown"}, conn=conn)
    return changed


def register(client, db_path, dispute_id, package_id, receipt_id, body, attachments, fingerprint, legacy):
    from copy import deepcopy
    body, attachments = deepcopy(body), deepcopy(attachments)
    precheck(db_path, dispute_id, package_id, receipt_id, body, attachments)
    reserved = False
    original = None
    result = None
    def before_post(raw):
        nonlocal reserved, original
        _, value = current_input(db_path, dispute_id, package_id, raw)
        reserve(db_path, receipt_id, value)
        original, reserved = value, True
    try:
        result = legacy(client, dispute_id, body, attachments, fingerprint, _before_post=before_post)
    finally:
        if reserved:
            changed = finish(db_path, receipt_id, original, result)
    if result.status == "rejected_link":
        invalidate(db_path, receipt_id)
    if reserved and changed:
        result = replace(result, reason="The appeal was attempted, but a source changed while sending. Review; do not retry.",
                         source_changed=True)
    return result
