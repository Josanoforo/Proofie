"""Stored outcomes of the evidence send and of Check again (post/redirect/get).

POST /send and POST /check used to answer with the result page itself, so a browser reload offered to repeat the
request. Now each outcome is saved here under an opaque random id and the POST answers 303 to
/disputes/{id}/packages/{pid}/result/{rid}. That page only shows what was stored: it never calls PayPal and never
writes, so reloading it any number of times changes nothing.

What is stored is exactly what the page shows: the outcome's title, message, technical text and state, the HTTP
status it had, the note and the "last read" label. States still come from PayPal responses and the attempt registry,
decided before the outcome is saved; nothing here decides one.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone

DDL = """
CREATE TABLE IF NOT EXISTS send_results (
    rid TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL,
    package_id INTEGER NOT NULL,
    http_status INTEGER NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    technical TEXT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    note TEXT,
    retry_check INTEGER NOT NULL DEFAULT 0,
    last_read TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
"""
# the read-back time of a confirmed send is stored WITH the outcome, so reopening the stored page later
# shows the time that was true when it was saved, whatever was edited afterwards. Tables made before this column get it.
_ALTER = "ALTER TABLE send_results ADD COLUMN confirmed_at TEXT"
_FIELDS = ("http_status", "title", "message", "technical", "kind", "state", "note", "retry_check", "last_read",
           "confirmed_at")


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def save(db_path, dispute_id: str, package_id: int, record: dict) -> str:
    """Stores one outcome and returns its opaque id (22 URL-safe characters of randomness)."""
    rid = secrets.token_urlsafe(16)
    conn = _connect(db_path)
    try:
        conn.executescript(DDL)
        try:
            conn.execute(_ALTER)
        except sqlite3.OperationalError:
            pass   # the column is already there (a table made by this version, or altered before)
        conn.execute(
            "INSERT INTO send_results (rid, dispute_id, package_id, http_status, title, message, technical, kind, "
            "state, note, retry_check, last_read, created_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, dispute_id, int(package_id), int(record["http_status"]), record["title"], record["message"],
             record.get("technical"), record["kind"], record["state"], record.get("note"),
             1 if record.get("retry_check") else 0, record.get("last_read"),
             datetime.now(timezone.utc).isoformat(timespec="seconds"), record.get("confirmed_at")))
        conn.commit()
    finally:
        conn.close()
    return rid


def load(db_path, rid: str, dispute_id: str, package_id: int) -> dict | None:
    """The stored outcome, only when it belongs to this dispute and package; otherwise None. Read only: it does not
    create the table, so looking up an id on a database that never stored one writes nothing."""
    if not isinstance(rid, str) or not 8 <= len(rid) <= 64:
        return None
    conn = _connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='send_results'").fetchone():
            return None
        row = conn.execute("SELECT * FROM send_results WHERE rid=? AND dispute_id=? AND package_id=?",
                           (rid, dispute_id, int(package_id))).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    keys = row.keys()
    record = {name: (row[name] if name in keys else None) for name in _FIELDS}
    record["retry_check"] = bool(record["retry_check"])
    return record
