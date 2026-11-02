"""Read/write functions for the app-data tables (docs/ARCHITECTURE.md section 5), one group per table. Each
function opens a short, connection-per-call transaction (simple and safe for a single-worker local app;
see docs/ARCHITECTURE.md section 8 (addendum A, idempotency) for why send_attempts specifically is NOT here - that is attempt_store.py,
with its own connection-lifetime and locking story). Money/dates here are whatever the caller already
computed by code elsewhere (pilot/core logic); this layer does not interpret dispute data, it only stores
what the rest of the app decided.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from copilot.db.schema import ensure_schema


def _connect(db_path: Path | str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)  # a clean `git clone` has no data/ yet
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(db_path, conn=conn)
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ------------------------------------------------------------------------------------------- documents
def insert_document(db_path, dispute_id: str, filename: str, content_type: str, size_bytes: int,
                    sha256: str, extracted_text: str | None, extraction_ok: bool) -> int:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO documents (dispute_id, filename, content_type, size_bytes, sha256, "
            "extracted_text, extraction_ok, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, filename, content_type, size_bytes, sha256, extracted_text,
             int(extraction_ok), _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _doc_row(row) -> dict:
    d = dict(row)
    d["extraction_ok"] = bool(d["extraction_ok"])  # SQLite has no native bool; see test_db_repo.py's
                                                    # validation_ok regression test for why this matters
    d["excluded"] = bool(d["excluded"])
    return d


def set_document_excluded(db_path, document_id: int, excluded: bool) -> None:
    """A checklist conflict's 'remove the document' resolution (never deletes the row - see schema.py)."""
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE documents SET excluded = ? WHERE id = ?", (int(excluded), document_id))
        conn.commit()
    finally:
        conn.close()


def get_document(db_path, document_id: int) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return _doc_row(row) if row else None
    finally:
        conn.close()


def list_documents(db_path, dispute_id: str) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM documents WHERE dispute_id = ? ORDER BY id", (dispute_id,)).fetchall()
        return [_doc_row(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------------------------- packages
def insert_package(db_path, dispute_id: str, source: str, validation_ok: bool, review_flags: list[str],
                   model: str | None = None, model_digest: str | None = None,
                   prompt_sha256: str | None = None, raw_output: str | None = None,
                   effective_next_step: str | None = None) -> int:
    if source not in ("ai", "human_edit", "auto_manual"):
        # 'auto_manual' is a still-empty shell copilot/core/auto_prep.py created automatically,
        # distinct from 'human_edit' (a human explicitly chose manual mode and typed something).
        raise ValueError(f"source must be 'ai', 'human_edit' or 'auto_manual', got {source!r}")
    conn = _connect(db_path)
    try:
        version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM packages WHERE dispute_id = ?",
                                (dispute_id,)).fetchone()[0] or 0) + 1
        cur = conn.execute(
            "INSERT INTO packages (dispute_id, version, source, model, model_digest, prompt_sha256, "
            "raw_output, validation_ok, effective_next_step, review_flags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, version, source, model, model_digest, prompt_sha256, raw_output,
             int(validation_ok), effective_next_step, json.dumps(review_flags, ensure_ascii=False), _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_package(db_path, package_id: int) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["review_flags"] = json.loads(d.pop("review_flags_json"))
        d["validation_ok"] = bool(d["validation_ok"])
        return d
    finally:
        conn.close()


def list_packages(db_path, dispute_id: str) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM packages WHERE dispute_id = ? ORDER BY version",
                            (dispute_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["review_flags"] = json.loads(d.pop("review_flags_json"))
            d["validation_ok"] = bool(d["validation_ok"])
            out.append(d)
        return out
    finally:
        conn.close()


# ----------------------------------------------------------------------------------------------- claims
def insert_claims(db_path, package_id: int, claims: list[dict]) -> None:
    """claims: [{"evidence_index", "claim_index", "text", "source_ids", "basis", "edited_by_human"?,
    "suggested_field"?, "suggested_value"?}]. The last two are set only for a vision-proposed claim that
    maps to one of checklist.py's field names (copilot/core/vision_model.py) - NULL for everything else."""
    conn = _connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO claims (package_id, evidence_index, claim_index, text, source_ids_json, basis, "
            "edited_by_human, suggested_field, suggested_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(package_id, c["evidence_index"], c["claim_index"], c["text"],
             json.dumps(c["source_ids"], ensure_ascii=False), c["basis"], int(c.get("edited_by_human", False)),
             c.get("suggested_field"), c.get("suggested_value"))
             for c in claims])
        conn.commit()
    finally:
        conn.close()


def list_claims(db_path, package_id: int) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM claims WHERE package_id = ? ORDER BY evidence_index, claim_index, id",
                            (package_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["source_ids"] = json.loads(d.pop("source_ids_json"))
            d["edited_by_human"] = bool(d["edited_by_human"])
            out.append(d)
        return out
    finally:
        conn.close()


def delete_claim(db_path, package_id: int, claim_id: int) -> bool:
    """Removes one statement of one package. Scoped by the package: a claim of another package is
    never touched. True when a row was deleted. The caller owns the approval, the audit entry and the sent-version
    refusal."""
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM claims WHERE id = ? AND package_id = ?", (claim_id, package_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def mark_claim_edited(db_path, claim_id: int, new_text: str) -> None:
    """A human edit to a claim's text: records edited_by_human=1. The caller (routes_package.py) is
    responsible for the basis rule (an edited claim becomes 'seller_statement' unless a new source is
    attached) - this function only persists the text/flag, it does not decide policy."""
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE claims SET text = ?, edited_by_human = 1 WHERE id = ?", (new_text, claim_id))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------------------------ package_attachments
def attach_document(db_path, package_id: int, document_id: int) -> str:
    """Attaches a document to a package's send, snapshotting its current sha256. Returns that sha256
    (the caller needs it to extend approval_fingerprint)."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT sha256 FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"no document with id {document_id}")
        sha256 = row["sha256"]
        conn.execute(
            "INSERT INTO package_attachments (package_id, document_id, sha256) VALUES (?, ?, ?) "
            "ON CONFLICT(package_id, document_id) DO UPDATE SET sha256=excluded.sha256",
            (package_id, document_id, sha256))
        conn.commit()
        return sha256
    finally:
        conn.close()


def list_attachment_sha256s(db_path, package_id: int) -> list[str]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT sha256 FROM package_attachments WHERE package_id = ? ORDER BY document_id",
                            (package_id,)).fetchall()
        return [r["sha256"] for r in rows]
    finally:
        conn.close()


def list_attachments(db_path, package_id: int) -> list[dict]:
    """Full metadata (not just the sha256) for each document attached to this package, in attach order -
    a real send needs filename/content_type/size to build the multipart file parts. `excluded` (additive) is
    whether the document is left out of the checks: the file is still part of the send, so the page must say so."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT d.id AS document_id, d.filename, d.content_type, d.size_bytes, pa.sha256, d.excluded "
            "FROM package_attachments pa JOIN documents d ON d.id = pa.document_id "
            "WHERE pa.package_id = ? ORDER BY pa.document_id", (package_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["excluded"] = bool(d["excluded"])
            out.append(d)
        return out
    finally:
        conn.close()


def detach_document(db_path, package_id: int, document_id: int) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM package_attachments WHERE package_id = ? AND document_id = ?",
                    (package_id, document_id))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------- approvals
def insert_approval(db_path, package_id: int, approval_sha256: str, approved_by: str,
                    sufficiency_override: str | None = None, *, conn=None) -> int:
    owns_conn = conn is None
    if owns_conn:
        conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO approvals (package_id, approval_sha256, approved_by, approved_at, "
            "invalidated_at, sufficiency_override) VALUES (?, ?, ?, ?, NULL, ?)",
            (package_id, approval_sha256, approved_by, _now(), sufficiency_override))
        if owns_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def invalidate_approval(db_path, approval_id: int) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE approvals SET invalidated_at = ? WHERE id = ? AND invalidated_at IS NULL",
                     (_now(), approval_id))
        conn.commit()
    finally:
        conn.close()


def list_approvals(db_path, package_id: int) -> list[dict]:
    """Every approval row of a package, oldest first (valid or not). Display only: the page shows which approval a
    send used and whether it was cleared afterwards."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM approvals WHERE package_id = ? ORDER BY id", (package_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def has_invalidated_approval(db_path, package_id: int) -> bool:
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM approvals WHERE package_id = ? AND invalidated_at IS NOT NULL LIMIT 1",
                            (package_id,)).fetchone() is not None
    finally:
        conn.close()


def get_latest_valid_approval(db_path, package_id: int) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM approvals WHERE package_id = ? AND invalidated_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (package_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------- audit_log
def log_action(db_path, actor: str, action: str, dispute_id: str, detail: dict, *, conn=None) -> int:
    if actor not in ("vendor", "system"):
        raise ValueError(f"actor must be 'vendor' or 'system' (never the model), got {actor!r}")
    owns_conn = conn is None
    if owns_conn:
        conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO audit_log (ts, actor, action, dispute_id, detail) VALUES (?, ?, ?, ?, ?)",
            (_now(), actor, action, dispute_id, json.dumps(detail, ensure_ascii=False)))
        if owns_conn:
            conn.commit()
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------------------- sale_audit_log
_SALE_CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9]{17}$")
_SALE_BUYER_NOTE_STATES = ("delivered", "not_received")


def append_sale_action(db_path, capture_id: str, action: str, detail: dict, *, actor: str = "vendor") -> int:
    """Append the one supported seller-only pre-dispute response to the capture-keyed core ledger."""
    if not isinstance(capture_id, str) or not _SALE_CAPTURE_ID_RE.fullmatch(capture_id):
        raise ValueError("capture_id must be a 17-character transaction reference")
    if actor != "vendor" or action != "buyer_note_response":
        raise ValueError("only vendor buyer-note responses are supported in the sale ledger")
    if (not isinstance(detail, dict) or set(detail) != {"response", "source"}
            or detail.get("response") not in _SALE_BUYER_NOTE_STATES or detail.get("source") != "seller_recorded"):
        raise ValueError("sale response detail must contain only an allowed seller-recorded response")
    raw = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > 256:
        raise ValueError("sale response detail exceeds its storage bound")
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sale_audit_log (ts, actor, action, capture_id, detail) VALUES (?, ?, ?, ?, ?)",
            (_now(), actor, action, capture_id, raw))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_sale_actions(db_path, capture_id: str, action: str = "buyer_note_response") -> list[dict]:
    """Read only the validated capture's sale events from the core ledger."""
    if not isinstance(capture_id, str) or not _SALE_CAPTURE_ID_RE.fullmatch(capture_id):
        raise ValueError("capture_id must be a 17-character transaction reference")
    if action != "buyer_note_response":
        raise ValueError("unsupported sale ledger action")
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, ts, actor, action, capture_id, detail FROM sale_audit_log "
            "WHERE capture_id = ? AND action = ? ORDER BY id", (capture_id, action)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------------------- manual_fields
def get_manual_fields(db_path, dispute_id: str) -> dict:
    """Vendor-typed shipment facts (checklist.py's field vocabulary), or {} if none entered yet."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT fields_json FROM manual_fields WHERE dispute_id = ?",
                           (dispute_id,)).fetchone()
        return json.loads(row["fields_json"]) if row else {}
    finally:
        conn.close()


def set_manual_fields(db_path, dispute_id: str, fields: dict) -> None:
    """Replaces the full set of vendor-typed fields for this dispute (the form posts them all at once)."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO manual_fields (dispute_id, fields_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(dispute_id) DO UPDATE SET fields_json=excluded.fields_json, "
            "updated_at=excluded.updated_at",
            (dispute_id, json.dumps(fields, ensure_ascii=False), _now()))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------- auto_prep
# How long a claimed-but-never-finished row is presumed abandoned (the process that claimed it
# crashed or was killed) before a later caller may claim it again. Documented constants, not bare numbers.
# This is a SILENCE budget, not a total-runtime budget - prepare_dispute() heartbeats
# (touch_auto_prep) between stages, so a genuinely still-running prep (the model alone has a 300s timeout,
# docs/MCP.md; claims_for_dispute_images has no cap on how many images it processes at ~180s each) keeps
# bumping updated_at and never looks stale. Only real silence (a crash, a killed process) lets this fire.
AUTO_PREP_STALE_PREPARING_AFTER_SECONDS = 600   # 10 min of silence with no heartbeat at all
AUTO_PREP_FAILED_RETRY_AFTER_SECONDS = 300      # 5 min: a failed prep may well fail again immediately
                                                # (same transient cause) - give it a short cool-down rather
                                                # than retrying on literally the very next page view


def get_auto_prep(db_path, dispute_id: str) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM auto_prep WHERE dispute_id = ?", (dispute_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _cutoff(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - seconds))


def claim_auto_prep(db_path, dispute_id: str, version_hash: str) -> str | None:
    """ATOMIC claim - a single INSERT ... ON CONFLICT ... WHERE statement, not the previous
    read-then-write (a real TOCTOU gap: the web's background task and the MCP's prepare_draft tool can
    both reach this for the same dispute at nearly the same instant). Exactly one caller's statement can ever have rowcount==1
    for the same dispute_id at the same instant; SQLite serialises the rest.

    Returns a fresh claim_token (to be passed to touch_auto_prep/set_auto_prep_result) if this call won;
    None if not. Wins when: no row yet; OR the existing row is for a DIFFERENT version_hash (new data to
    prepare - 'una sola preparación por disputa y por versión' still holds, since the SAME version is
    never reclaimed while it is 'ready'); OR the existing row is 'preparing' and stale (presumed
    crashed, AUTO_PREP_STALE_PREPARING_AFTER_SECONDS of SILENCE, not total runtime); OR it is 'failed' and
    old enough to retry (AUTO_PREP_FAILED_RETRY_AFTER_SECONDS).

    The token changes on every win, even for the SAME version_hash - reclaiming a
    stale 'preparing' row hands out a NEW token, so the original (crashed-looking, but maybe just slow)
    run's eventual set_auto_prep_result/touch_auto_prep calls carry the OLD token and can never land,
    even though they would still pass a hash-only check."""
    token = uuid.uuid4().hex
    conn = _connect(db_path)
    try:
        try:
            cur = conn.execute(
                "INSERT INTO auto_prep (dispute_id, version_hash, claim_token, status, updated_at) "
                "VALUES (?, ?, ?, 'preparing', ?) "
                "ON CONFLICT(dispute_id) DO UPDATE SET version_hash=excluded.version_hash, "
                "claim_token=excluded.claim_token, status='preparing', package_id=NULL, detail=NULL, "
                "updated_at=excluded.updated_at "
                "WHERE auto_prep.version_hash != excluded.version_hash "
                "OR (auto_prep.status = 'preparing' AND auto_prep.updated_at < ?) "
                "OR (auto_prep.status = 'failed' AND auto_prep.updated_at < ?)",
                (dispute_id, version_hash, token, _now(),
                 _cutoff(AUTO_PREP_STALE_PREPARING_AFTER_SECONDS),
                 _cutoff(AUTO_PREP_FAILED_RETRY_AFTER_SECONDS)))
        except sqlite3.OperationalError:
            # Lock contention from a genuinely simultaneous caller: never a silent double-claim - treat
            # exactly like losing the race (same defensive stance as attempt_store.SQLiteAttemptStore.claim).
            return None
        won = cur.rowcount == 1
        conn.commit() if won else conn.rollback()
        return token if won else None
    finally:
        conn.close()


def touch_auto_prep(db_path, dispute_id: str, version_hash: str, claim_token: str) -> bool:
    """A heartbeat during 'preparing' - bumps updated_at only, leaving status/
    package_id/detail untouched, so claim_auto_prep's staleness check measures silence, not total
    runtime. Guarded by (version_hash, claim_token) exactly like set_auto_prep_result: a run that was
    already reclaimed out from under it (stale, or a newer version arrived) gets False and must stop -
    its own heartbeat can never resurrect a claim that moved on."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE auto_prep SET updated_at = ? WHERE dispute_id = ? AND version_hash = ? "
            "AND claim_token = ?", (_now(), dispute_id, version_hash, claim_token))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def set_auto_prep_result(db_path, dispute_id: str, version_hash: str, claim_token: str, status: str,
                         package_id: int | None = None, detail: str | None = None) -> bool:
    """Filters by BOTH the version_hash and the claim_token THIS caller was
    handed by claim_auto_prep - a prep still running for an OLD version, OR one that was reclaimed (same
    version_hash, but a NEW token handed to someone else) while this one was presumed stale, can never
    overwrite a newer claim's row. Returns False (and writes nothing) when the row no longer matches
    both exactly - same principle as attempt_store's atomic writes."""
    if status not in ("ready", "needs_documents", "failed"):
        raise ValueError(f"status must be a terminal auto_prep status, got {status!r}")
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE auto_prep SET status = ?, package_id = ?, detail = ?, updated_at = ? "
            "WHERE dispute_id = ? AND version_hash = ? AND claim_token = ?",
            (status, package_id, detail, _now(), dispute_id, version_hash, claim_token))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def list_audit(db_path, dispute_id: str) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM audit_log WHERE dispute_id = ? ORDER BY id", (dispute_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out
    finally:
        conn.close()


def confirm_document_reading(db_path, document_id: int, field_name: str, value: str) -> None:
    """The vendor explicitly confirming a
    suggested reading of an image as accurate - "seller-confirmed reading of <file>: <value>". Always
    confirmed_by='vendor' (the database's own CHECK constraint also enforces this - never the model).
    One current value per (document_id, field_name); confirming again replaces it, never duplicates -
    the caller (routes_package.py) is responsible for the audit_log entry, this only persists the fact."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO document_confirmations (document_id, field_name, value, confirmed_by, confirmed_at) "
            "VALUES (?, ?, ?, 'vendor', ?) "
            "ON CONFLICT(document_id, field_name) DO UPDATE SET value = excluded.value, "
            "confirmed_at = excluded.confirmed_at",
            (document_id, field_name, value, _now()))
        conn.commit()
    finally:
        conn.close()


def get_confirmed_readings(db_path, document_id: int) -> dict[str, dict]:
    """{field_name: {"value", "confirmed_at"}} for one document - [] (an empty dict) for a document with
    no confirmations, never an error."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT field_name, value, confirmed_at FROM document_confirmations WHERE document_id = ?",
            (document_id,)).fetchall()
        return {r["field_name"]: {"value": r["value"], "confirmed_at": r["confirmed_at"]} for r in rows}
    finally:
        conn.close()
