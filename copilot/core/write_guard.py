"""Send-once reservations for PayPal writes that carry no durable guard of their own.

In the sandbox PayPal did not deduplicate send-message by the PayPal-Request-Id header (observed 2026-10-16: a repeat
click 1 min 43 s after a confirmed message produced a second seller message), and the app does not rely on the
header for make-offer or add-tracking either. So the app enforces one send per
approved request: one row per (subject, action, request sha256) in the app's SQLite database, taken with BEGIN
IMMEDIATE BEFORE the POST, so two concurrent submits (threads or processes) cannot both reach PayPal. The same
pattern as `active_send_reservations` (copilot/features/active_approval/store.py): short transaction, no connection
held across the POST.

States: reserved (taken, POST not finished) -> sent (2xx) | unknown (outcome not known: transport failure, crash,
unexpected exception) | refused (a definite PayPal refusal with an HTTP status; frees the key for a new approval).
reserved, sent and unknown BLOCK a new reservation of the same key. `unknown` never auto-resends: the seller is told
to check PayPal. A different body (different sha256) is a different key, so a seller who wants to say something else
simply writes different text and approves it again."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from copilot import config

RESERVED, SENT, UNKNOWN, REFUSED = "reserved", "sent", "unknown", "refused"
BLOCKING = (RESERVED, SENT, UNKNOWN)
_FINAL = (SENT, UNKNOWN, REFUSED)
# A PayPal listing can lag, and a too-early "absent" would invite a duplicate. The one release rule for every
# flow (resolution message, fulfilment tracker): a successful, complete read that does not list the write, AND a POST
# older than the flow's settle window and at least RELEASE_FLOOR_SECONDS old. Only then release_after_absence().
RELEASE_FLOOR_SECONDS = 10 * 60


def release_min_age(settle_window_seconds: float = 0) -> float:
    """The minimum age (seconds) of a POST before a positive absence may release its key."""
    return max(settle_window_seconds, RELEASE_FLOOR_SECONDS)

_DDL = """
CREATE TABLE IF NOT EXISTS write_reservations (
    subject_id     TEXT NOT NULL,
    action         TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('reserved', 'sent', 'unknown', 'refused')),
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (subject_id, action, request_sha256)
);
"""


@dataclass(frozen=True)
class Claim:
    reserved: bool                 # True: the caller owns the key and may POST (once)
    state: str | None = None       # the blocking state when reserved is False
    created_at: float | None = None


def _connect() -> sqlite3.Connection:
    path = Path(config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)    # explicit BEGIN IMMEDIATE below
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def reserve(subject_id: str, action: str, request_sha256: str) -> Claim:
    """Atomically take the key. Not reserved when a row in a BLOCKING state exists; a `refused` row is re-taken."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            row = conn.execute("SELECT state, created_at FROM write_reservations WHERE subject_id=? AND action=? "
                               "AND request_sha256=?", (subject_id, action, request_sha256)).fetchone()
            if row is not None and row["state"] in BLOCKING:
                conn.execute("COMMIT")
                return Claim(False, row["state"], row["created_at"])
            if row is None:
                conn.execute("INSERT INTO write_reservations VALUES (?, ?, ?, ?, ?, ?)",
                             (subject_id, action, request_sha256, RESERVED, now, now))
            else:
                conn.execute("UPDATE write_reservations SET state=?, created_at=?, updated_at=? WHERE subject_id=? "
                             "AND action=? AND request_sha256=?", (RESERVED, now, now, subject_id, action, request_sha256))
            conn.execute("COMMIT")
            return Claim(True)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def finish(subject_id: str, action: str, request_sha256: str, state: str) -> None:
    """Record how the POST ended: sent | unknown | refused. Only a reserved row moves."""
    if state not in _FINAL:
        raise ValueError(f"unknown final state {state!r}")
    conn = _connect()
    try:
        conn.execute("UPDATE write_reservations SET state=?, updated_at=? WHERE subject_id=? AND action=? "
                     "AND request_sha256=? AND state=?", (state, time.time(), subject_id, action, request_sha256, RESERVED))
    finally:
        conn.close()


def release_after_absence(subject_id: str, action: str, request_sha256: str, created_at_most: float | None = None) -> bool:
    """Frees a key whose send PayPal's own read has positively NOT shown: `sent` or `unknown`
    becomes `refused`, so the seller can approve the same content again on a fresh check page. Never called by a send
    path: only after an explicit read-only check found the message missing. `reserved` (a send in flight) is untouched.
    `created_at_most` (epoch seconds) makes the release conditional on the reservation itself: a reservation taken later
    than that (a newer send of the same content) is never released by a check of an older row. Returns True when a row
    changed."""
    conn = _connect()
    try:
        sql = ("UPDATE write_reservations SET state=?, updated_at=? WHERE subject_id=? AND action=? "
               "AND request_sha256=? AND state IN (?, ?)")
        args = [REFUSED, time.time(), subject_id, action, request_sha256, SENT, UNKNOWN]
        if created_at_most is not None:
            sql += " AND created_at <= ?"
            args.append(created_at_most)
        cur = conn.execute(sql, args)
        return cur.rowcount > 0
    finally:
        conn.close()


def lookup(subject_id: str, action: str, request_sha256: str) -> dict | None:
    """The reservation row (state, created_at) or None. Read only apart from creating the empty table."""
    path = Path(config.db_path())
    if not path.exists():
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT state, created_at FROM write_reservations WHERE subject_id=? AND action=? "
                           "AND request_sha256=?", (subject_id, action, request_sha256)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_reservations(subject_id: str) -> list:
    """Every reservation row of one subject, oldest first: action, request_sha256, state, created_at (epoch seconds).
    Read only, apart from creating the empty table. It lets a page keep the trace of an attempt whose end is unknown."""
    path = Path(config.db_path())
    if not path.exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute("SELECT action, request_sha256, state, created_at, updated_at FROM write_reservations "
                            "WHERE subject_id=? ORDER BY created_at, rowid", (subject_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def is_blocking(row: dict | None) -> bool:
    return bool(row and row["state"] in BLOCKING)
