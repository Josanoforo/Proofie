"""The seller's resolution writes, in the app's SQLite database (config.db_path()), own table `resolution_actions`.

Created here with CREATE TABLE IF NOT EXISTS on every connection (copilot/db/schema.py is not touched). Columns: id,
dispute_id, action (offer | message), created_at, request_json, request_sha256, approval_sha256, readback_json (stage,
status, link rels, outcome, message_listed / message_count, checked_at, origin code only; no buyer data and no message
text), settled (0/1), status (sent_pending_settle | sent_confirmed | sent_unconfirmed | refused_422 | failed),
limitation_text. One audit row per POST attempt through repo.log_action (hashes and status only).

A second table, `resolution_checks` (token, dispute_id, action, body_json, request_sha256, origin, created_at): the
exact body the seller is reviewing on the check page, kept on the server so it never travels in a URL or a form field.
It is NOT a send-once table: copilot.core.write_guard stays the only guard against a repeat send. A check
is SPENT (`superseded` = 1) when a send of its body may have reached PayPal, and every check of a released request that
was prepared before the release is spent too, so a repeat after a release always needs a fresh check page and approval."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from copilot import config
from copilot.db import repo

_DDL = """
CREATE TABLE IF NOT EXISTS resolution_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('offer', 'message')),
    created_at      TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    request_sha256  TEXT NOT NULL,
    approval_sha256 TEXT NOT NULL,
    readback_json   TEXT NOT NULL,
    settled         INTEGER NOT NULL,
    status          TEXT NOT NULL,
    limitation_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_resolution_dispute ON resolution_actions(dispute_id);
CREATE TABLE IF NOT EXISTS resolution_checks (
    token          TEXT PRIMARY KEY,
    dispute_id     TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('offer', 'message')),
    body_json      TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    origin         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    superseded     INTEGER NOT NULL DEFAULT 0
);
"""
CHECK_TTL_SECONDS = 60 * 60            # a check page stops working after 60 minutes
ACTIONS = ("offer", "message")
STATUS_PENDING = "sent_pending_settle"      # the POST was accepted (2xx); PayPal has not settled the dispute yet
STATUSES = (STATUS_PENDING, "sent_confirmed", "sent_unconfirmed", "refused_422", "failed")
COLUMNS = ("id", "dispute_id", "action", "created_at", "request_json", "request_sha256", "approval_sha256",
           "readback_json", "settled", "status", "limitation_text")
# An earlier version stored "failed" for ANY HTTP status and for a transport failure. A legacy "failed" row whose limitation says a
# 5xx, a 429 or no answer in time is not a refusal: PayPal may have it. The same rule as fulfilment's legacy unknown.
_LEGACY_UNKNOWN = re.compile(r"what happened is unknown|\(error (?:5\d\d|429)\)|did not answer in time", re.I)


def effective_status(row: dict | None) -> str | None:
    """The status to show and to reason with: a legacy `failed` row that PayPal may have received (5xx, 429, no answer in
    time) is `sent_unconfirmed`, so it keeps the Check button, blocks a repeat and is never worded as a refusal."""
    if not row:
        return None
    if row["status"] == "failed" and _LEGACY_UNKNOWN.search(row.get("limitation_text") or ""):
        return "sent_unconfirmed"
    return row["status"]


def _stored(row) -> dict:
    stored = {k: row[k] for k in COLUMNS}
    stored["status"] = effective_status(stored)
    return stored


def _connect() -> sqlite3.Connection:
    path = Path(config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(resolution_checks)")}
    if "superseded" not in columns:                    # a database made before this column existed: add it once
        try:
            conn.execute("ALTER TABLE resolution_checks ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:               # another connection added it first
            pass
    return conn


def ensure_table() -> None:
    _connect().close()


def save_action(dispute_id: str, action: str, request: dict, request_sha: str, approval_sha: str, readback,
                settled: bool, status: str, limitation_text: str | None = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO resolution_actions (dispute_id, action, created_at, request_json, request_sha256, "
            "approval_sha256, readback_json, settled, status, limitation_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, action, time.strftime("%Y-%m-%dT%H:%M:%S%z"), json.dumps(request, sort_keys=True,
             ensure_ascii=False), request_sha, approval_sha, json.dumps(readback, sort_keys=True, ensure_ascii=False),
             int(bool(settled)), status, limitation_text))
        conn.commit()
        saved = {k: v for k, v in dict(conn.execute("SELECT * FROM resolution_actions WHERE id = ?",
                                                    (cur.lastrowid,)).fetchone()).items() if k in COLUMNS}
    finally:
        conn.close()
    repo.log_action(config.db_path(), "vendor", f"resolution_{action}", dispute_id,
                    {"request_sha256": request_sha, "approval_sha256": approval_sha, "status": status})
    return saved


def list_actions(dispute_id: str) -> list:
    """Every stored action of a dispute, oldest first."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM resolution_actions WHERE dispute_id = ? ORDER BY id",
                            (dispute_id,)).fetchall()
        return [_stored(r) for r in rows]
    finally:
        conn.close()


SENT_STATUSES = (STATUS_PENDING, "sent_confirmed", "sent_unconfirmed")   # the POST may have reached PayPal: blocks a repeat


def find_sent(dispute_id: str, action: str, request_sha: str) -> dict | None:
    """The latest stored row of this exact request (dispute, action, request sha256) whose POST may have reached
    PayPal (pending, confirmed or unconfirmed), or None. refused_422 and failed (a PayPal refusal) do not count, a legacy
    failed row that PayPal may have received (effective_status) does, and a row released after a read-only check showed PayPal
    does not list the message does not."""
    conn = _connect()
    try:
        marks = ", ".join("?" for _ in SENT_STATUSES)
        rows = conn.execute(f"SELECT * FROM resolution_actions WHERE dispute_id = ? AND action = ? AND request_sha256 = ? "
                            f"AND status IN ({marks}, 'failed') ORDER BY id DESC",
                            (dispute_id, action, request_sha, *SENT_STATUSES)).fetchall()
        for row in rows:
            stored = _stored(row)
            if stored["status"] not in SENT_STATUSES:        # a failed row that PayPal definitely refused
                continue
            if not readback_of(stored).get("released"):      # released: PayPal's own read showed it was not there
                return stored
        return None
    finally:
        conn.close()


def get_action(dispute_id: str, row_id) -> dict | None:
    """One stored action of THIS dispute, or None (a row of another dispute is never returned)."""
    if isinstance(row_id, bool) or not isinstance(row_id, int):
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM resolution_actions WHERE id = ? AND dispute_id = ?",
                           (row_id, dispute_id)).fetchone()
        return _stored(row) if row else None
    finally:
        conn.close()


def save_check(dispute_id: str, action: str, body: dict, request_sha: str, origin: str, now=None) -> str:
    """Stores the exact body under a random token (secrets.token_urlsafe) and returns the token."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    token = secrets.token_urlsafe(24)
    conn = _connect()
    try:
        conn.execute("INSERT INTO resolution_checks (token, dispute_id, action, body_json, request_sha256, origin, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (token, dispute_id, action, json.dumps(body, sort_keys=True, ensure_ascii=False), request_sha,
                      origin, time.time() if now is None else now))
        conn.commit()
    finally:
        conn.close()
    return token


def get_check(token: str, dispute_id: str, now=None, include_spent: bool = False) -> dict | None:
    """The stored check of this token for THIS dispute, or None when it is unknown, belongs to another dispute, is older
    than CHECK_TTL_SECONDS or is spent (`include_spent` keeps a spent one, so a page can tell "used" from "expired").
    The body comes back parsed under `body`."""
    if not isinstance(token, str) or not token or len(token) > 200:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM resolution_checks WHERE token = ? AND dispute_id = ?",
                           (token, dispute_id)).fetchone()
        if row is None or (time.time() if now is None else now) - row["created_at"] > CHECK_TTL_SECONDS:
            return None
        if row["superseded"] and not include_spent:
            return None
        found = dict(row)
        found["body"] = json.loads(found["body_json"])
        found["spent"] = bool(found.pop("superseded"))
        return found
    finally:
        conn.close()


def spend_check(token: str, dispute_id: str) -> None:
    """A send of this token's body may have reached PayPal: the check page is used up (it behaves like an expired one)."""
    conn = _connect()
    try:
        conn.execute("UPDATE resolution_checks SET superseded = 1 WHERE token = ? AND dispute_id = ?", (token, dispute_id))
        conn.commit()
    finally:
        conn.close()


def supersede_checks_for_request(dispute_id: str, action: str, request_sha: str, created_before) -> None:
    """A release spends every check page of this exact request prepared at or before `created_before`: the approval of the
    released send is spent. A repeat needs a new check page and a new approval."""
    conn = _connect()
    try:
        conn.execute("UPDATE resolution_checks SET superseded = 1 WHERE dispute_id = ? AND action = ? AND request_sha256 = ? "
                     "AND created_at <= ?", (dispute_id, action, request_sha, created_before))
        conn.commit()
    finally:
        conn.close()


def readback_of(row: dict) -> dict:
    """The stored read-back of a row as a dict ({} when there is none)."""
    try:
        value = json.loads(row.get("readback_json") or "null")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def update_action_status(row_id: int, status: str, readback, settled: bool, limitation_text: str | None = None) -> None:
    """Moves one stored action from sent_pending_settle to its final status with the read-back PayPal showed. The origin
    code stored at send time survives the update."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    conn = _connect()
    try:
        old = conn.execute("SELECT readback_json FROM resolution_actions WHERE id = ?", (row_id,)).fetchone()
        origin = readback_of({"readback_json": old["readback_json"]}).get("origin") if old else None
        if origin and (readback is None or isinstance(readback, dict) and "origin" not in readback):
            readback = {**(readback or {}), "origin": origin}
        conn.execute("UPDATE resolution_actions SET status = ?, readback_json = ?, settled = ?, limitation_text = ? "
                     "WHERE id = ?", (status, json.dumps(readback, sort_keys=True, ensure_ascii=False), int(bool(settled)),
                                      limitation_text, row_id))
        conn.commit()
    finally:
        conn.close()
