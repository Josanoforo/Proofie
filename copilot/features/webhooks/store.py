"""Webhook events, in the app's SQLite database (config.db_path()), own table `webhook_events`.

Created here with CREATE TABLE IF NOT EXISTS on every connection (copilot/db/schema.py is not touched). Columns: id,
received_at, event_id (unique), event_type, resource_type, dispute_id (nullable, validated before storing),
transmission_id, body_sha256 (of the raw body bytes), verification (verified | failed | rejected_headers | limitation | unverified),
readback_status (the dispute status from OUR GET that followed; nullable), readback_sha256 (sha256_json of that GET's body),
limitation_text. Never a header, a signature or a body. One audit row per stored event through repo.log_action."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from copilot import config
from copilot.db import repo

_DDL = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at      TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    resource_type    TEXT,
    dispute_id       TEXT,
    transmission_id  TEXT,
    body_sha256      TEXT NOT NULL,
    verification     TEXT NOT NULL,
    readback_status  TEXT,
    readback_sha256  TEXT,
    limitation_text  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_events_event_id ON webhook_events(event_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_dispute ON webhook_events(dispute_id);
"""
VERIFICATIONS = ("verified", "failed", "rejected_headers", "limitation", "unverified")
COLUMNS = ("id", "received_at", "event_id", "event_type", "resource_type", "dispute_id", "transmission_id",
           "body_sha256", "verification", "readback_status", "readback_sha256", "limitation_text")


def _connect() -> sqlite3.Connection:
    path = Path(config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def ensure_table() -> None:
    _connect().close()


def _row(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in COLUMNS}


def get_event(event_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def save_event(event_id: str, event_type: str, resource_type: str | None, dispute_id: str | None,
               transmission_id: str | None, body_sha256: str, verification: str, readback_status: str | None = None,
               readback_sha256: str | None = None, limitation_text: str | None = None) -> dict:
    """Stores one event. event_id is unique: a second save for the same id raises sqlite3.IntegrityError, except that a row
    whose verification is not `verified` is replaced (a retry after a failed or unavailable verification must not be
    ignored, and a forged event must not be able to occupy a genuine event's id). A verified row is never touched."""
    if verification not in VERIFICATIONS:
        raise ValueError(f"unknown verification {verification!r}")
    received = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    values = (received, event_id, event_type, resource_type, dispute_id, transmission_id, body_sha256, verification,
              readback_status, readback_sha256, limitation_text)
    conn = _connect()
    try:
        existing = conn.execute("SELECT id, verification FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO webhook_events (received_at, event_id, event_type, resource_type, dispute_id, "
                         "transmission_id, body_sha256, verification, readback_status, readback_sha256, limitation_text) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
        elif existing["verification"] != "verified":
            updated = conn.execute("UPDATE webhook_events SET received_at = ?, event_type = ?, resource_type = ?, dispute_id = ?, "
                         "transmission_id = ?, body_sha256 = ?, verification = ?, readback_status = ?, "
                         "readback_sha256 = ?, limitation_text = ? WHERE id = ? AND verification != 'verified'",
                         values[:1] + values[2:] + (existing["id"],))
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("UNIQUE constraint failed: webhook_events.event_id")
        else:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: webhook_events.event_id")
        conn.commit()
        saved = _row(conn.execute("SELECT * FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone())
    finally:
        conn.close()
    repo.log_action(config.db_path(), "system", "webhook_event", dispute_id or "-",
                    {"event_type": event_type, "verification": verification, "body_sha256": body_sha256,
                     "readback_status": readback_status})
    return saved


def list_events(limit: int = 100) -> list:
    """The latest events, newest first."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def events_for_dispute(dispute_id: str) -> list:
    """Every stored event of one dispute, newest first."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM webhook_events WHERE dispute_id = ? ORDER BY id DESC", (dispute_id,)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()
