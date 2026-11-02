"""The pre-dispute file, in the app's SQLite database (config.db_path()) in its own table `fulfilment_files`.

The table is created here with CREATE TABLE IF NOT EXISTS on every connection (copilot/db/schema.py is not touched).
Columns: id, capture_id, created_at, request_json, request_sha256, approval_sha256, tracker_readback_json,
checks_json, file_sha256, status, limitation_text.
file_sha256 canon: sha256_json (sorted keys, ensure_ascii=False, UTF-8) over
{"request": <request_json parsed>, "tracker_readback": <tracker_readback_json parsed>, "checks": <checks_json parsed>}.
Saving writes one audit_log row through repo.log_action (hashes and status only, never the tracking number).

A second table, `fulfilment_checks`, holds what the seller prepared on the form (the body, its sha256,
the address declared and what was typed) behind an opaque random token, so the check page is a separate GET that
renders from the stored body and the confirm form carries no tracking details. It is display and register input
only: list_files, get_file and the delivery-facts reader never read it, and a check is never a file.

A file whose status is `unknown` is one whose POST ended without an answer PayPal confirmed (transport failure, a 5xx
or a 429): the tracking may or may not exist in PayPal. Rows saved before this status existed record that case as
`failed` with a limitation that says so; effective_status() reads them as `unknown`."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from copilot import config
from copilot.core.send import sha256_json
from copilot.db import repo

_DDL = """
CREATE TABLE IF NOT EXISTS fulfilment_files (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    request_json          TEXT NOT NULL,
    request_sha256        TEXT NOT NULL,
    approval_sha256       TEXT NOT NULL,
    tracker_readback_json TEXT NOT NULL,
    checks_json           TEXT NOT NULL,
    file_sha256           TEXT NOT NULL,
    status                TEXT NOT NULL,
    limitation_text       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fulfilment_capture ON fulfilment_files(capture_id);
CREATE TABLE IF NOT EXISTS fulfilment_checks (
    token          TEXT PRIMARY KEY,
    capture_id     TEXT NOT NULL,
    body_json      TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    declared_json  TEXT NOT NULL,
    form_json      TEXT NOT NULL,
    notes_json     TEXT NOT NULL,
    created_at     REAL NOT NULL,
    superseded     INTEGER NOT NULL DEFAULT 0
);
"""
STATUSES = ("registered", "registered_unconfirmed", "unknown", "failed", "saved_without_registration")
# The statuses whose POST may have reached PayPal: a repeat of the same body is never sent on its own.
SENT_POSSIBLE = ("registered", "registered_unconfirmed", "unknown")
COLUMNS = ("id", "capture_id", "created_at", "request_json", "request_sha256", "approval_sha256",
           "tracker_readback_json", "checks_json", "file_sha256", "status", "limitation_text")
CHECK_TTL_SECONDS = 24 * 3600
_LEGACY_UNKNOWN = re.compile(r"what happened is unknown|\(error (?:5\d\d|429)\)", re.I)


def _connect() -> sqlite3.Connection:
    path = Path(config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def ensure_table() -> None:
    _connect().close()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def file_sha256(request: dict, tracker_readback, checks) -> str:
    return sha256_json({"request": request, "tracker_readback": tracker_readback, "checks": checks})


def recompute_file_sha256(row: dict) -> str:
    return file_sha256(json.loads(row["request_json"]), json.loads(row["tracker_readback_json"]),
                       json.loads(row["checks_json"]))


def _row(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in COLUMNS}


def effective_status(row: dict | None) -> str | None:
    """The status to show and to reason with. A legacy `failed` row whose limitation says the outcome is unknown (a
    transport failure, a 5xx or a 429 saved before the `unknown` status existed) is `unknown`: PayPal may have it."""
    if not row:
        return None
    if row["status"] == "failed" and _LEGACY_UNKNOWN.search(row.get("limitation_text") or ""):
        return "unknown"
    return row["status"]


def could_have_reached_paypal(row: dict | None) -> bool:
    return effective_status(row) in SENT_POSSIBLE


def save_file(capture_id: str, request: dict, request_sha: str, approval_sha: str, tracker_readback,
              checks, status: str, limitation_text: str | None = None,
              audit_action: str = "fulfilment_registered") -> dict:
    """Insert one file, write one audit row, return the stored row. `tracker_readback` and `checks` are
    JSON-serialisable (None for no read-back). A later check of PayPal appends a NEW row (audit_action
    `fulfilment_checked`); a saved row is never updated."""
    if status not in STATUSES:
        raise ValueError(f"unknown file status {status!r}")
    fsha = file_sha256(request, tracker_readback, checks)
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO fulfilment_files (capture_id, created_at, request_json, request_sha256, approval_sha256, "
            "tracker_readback_json, checks_json, file_sha256, status, limitation_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, time.strftime("%Y-%m-%dT%H:%M:%S%z"), canonical_json(request), request_sha, approval_sha,
             canonical_json(tracker_readback), canonical_json(checks), fsha, status, limitation_text))
        conn.commit()
        row = conn.execute("SELECT * FROM fulfilment_files WHERE id = ?", (cur.lastrowid,)).fetchone()
        saved = _row(row)
    finally:
        conn.close()
    repo.log_action(config.db_path(), "vendor", audit_action, capture_id,
                    {"request_sha256": request_sha, "approval_sha256": approval_sha, "file_sha256": fsha,
                     "status": status})
    return saved


def get_file(capture_id: str) -> dict | None:
    """The latest file of a capture, or None."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM fulfilment_files WHERE capture_id = ? ORDER BY id DESC LIMIT 1",
                           (capture_id,)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def get_file_by_id(capture_id: str, file_id: int) -> dict | None:
    """One stored file of this capture, or None (a file of another capture is never returned)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM fulfilment_files WHERE id = ? AND capture_id = ?",
                           (file_id, capture_id)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def latest_for_request(capture_id: str, request_sha: str) -> dict | None:
    """The latest stored file of exactly this approved request (capture id + request sha256), or None."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM fulfilment_files WHERE capture_id = ? AND request_sha256 = ? "
                           "ORDER BY id DESC LIMIT 1", (capture_id, request_sha)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def released(row: dict | None) -> bool:
    """True when this file version records a release: a read-only check found PayPal does not list the tracking, by the
    release rule of copilot/core/write_guard.py (routes.check_paypal_again)."""
    if not row:
        return False
    try:
        recheck = json.loads(row["checks_json"]).get("recheck") or {}
    except (ValueError, TypeError, AttributeError):
        return False
    return isinstance(recheck, dict) and recheck.get("released") is True


def latest_sent_for_request(capture_id: str, request_sha: str) -> dict | None:
    """The latest stored file of this exact request whose POST could have reached PayPal (registered, registered but
    not confirmed, unknown). Never a file saved without registration and never a definite PayPal refusal. None when the
    newest file of this request records a release (PayPal's own complete read did not list it, old enough): the seller
    may then approve the same tracking again on a new check page; a new send writes a newer file that blocks again."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM fulfilment_files WHERE capture_id = ? AND request_sha256 = ? "
                            "ORDER BY id DESC", (capture_id, request_sha)).fetchall()
    finally:
        conn.close()
    rows = [_row(r) for r in rows]
    if rows and released(rows[0]):
        return None
    return next((r for r in rows if could_have_reached_paypal(r)), None)


def first_sent_epoch(capture_id: str, request_sha: str) -> float | None:
    """When the first file of this request that could have reached PayPal was saved (epoch seconds), or None. The
    fallback clock of the release rule when no reservation row exists (a legacy file)."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM fulfilment_files WHERE capture_id = ? AND request_sha256 = ? "
                            "ORDER BY id", (capture_id, request_sha)).fetchall()
    finally:
        conn.close()
    first = next((r for r in (_row(r) for r in rows) if could_have_reached_paypal(r)), None)
    if first is None:
        return None
    try:
        return datetime.strptime(first["created_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (ValueError, TypeError):
        return None


def list_files() -> list:
    """The latest file of every capture, newest first."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM fulfilment_files WHERE id IN "
                            "(SELECT MAX(id) FROM fulfilment_files GROUP BY capture_id) ORDER BY id DESC").fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------------- the check
def save_check(capture_id: str, body: dict, request_sha: str, declared: dict, form: dict, notes: dict | None = None) -> str:
    """Store what the seller prepared and return its opaque token. `form` is what was typed (to refill the form),
    `notes` what Proofie learned while preparing it (for example that PayPal could not be read)."""
    token = secrets.token_urlsafe(18)
    conn = _connect()
    try:
        conn.execute("INSERT INTO fulfilment_checks (token, capture_id, body_json, request_sha256, declared_json, "
                      "form_json, notes_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (token, capture_id, canonical_json(body), request_sha, canonical_json(declared),
                       canonical_json(form), canonical_json(notes or {}), time.time()))
        conn.commit()
    finally:
        conn.close()
    return token


def get_check(capture_id: str, token: str) -> dict | None:
    """The prepared check of THIS capture, or None for an unknown, foreign, expired or superseded token."""
    if not isinstance(token, str) or not token or len(token) > 80:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM fulfilment_checks WHERE token = ? AND capture_id = ?",
                           (token, capture_id)).fetchone()
    finally:
        conn.close()
    if row is None or row["superseded"] or time.time() - row["created_at"] > CHECK_TTL_SECONDS:
        return None
    return {"token": row["token"], "capture_id": row["capture_id"], "body": json.loads(row["body_json"]),
            "request_sha256": row["request_sha256"], "declared": json.loads(row["declared_json"]),
            "form": json.loads(row["form_json"]), "notes": json.loads(row["notes_json"]),
            "created_at": row["created_at"]}


def supersede_checks_for_request(capture_id: str, request_sha: str, created_before: float) -> None:
    """A release spends every check page of this exact request prepared before it (created_at <= created_before): the
    approval of the released send is spent. A repeat needs a new check page and a new approval."""
    conn = _connect()
    try:
        conn.execute("UPDATE fulfilment_checks SET superseded = 1 WHERE capture_id = ? AND request_sha256 = ? "
                     "AND created_at <= ?", (capture_id, request_sha, created_before))
        conn.commit()
    finally:
        conn.close()


def supersede_check(capture_id: str, token: str) -> None:
    """The seller went back to change the details: this token is spent (it behaves like an expired one)."""
    conn = _connect()
    try:
        conn.execute("UPDATE fulfilment_checks SET superseded = 1 WHERE token = ? AND capture_id = ?",
                     (token, capture_id))
        conn.commit()
    finally:
        conn.close()
