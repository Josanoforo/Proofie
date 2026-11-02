"""Appeal attempts, in the app's SQLite database (config.db_path()), own table `outcome_appeals`.

Created here with CREATE TABLE IF NOT EXISTS on every connection (copilot/db/schema.py is not touched). Columns: id,
dispute_id, created_at, request_json, request_sha256, approval_sha256, readback_json (stage, status, link rels,
outcome only), status (sent_confirmed | sent_unconfirmed | failed), limitation_text. One audit row per POST attempt
through repo.log_action (hashes and status only)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from copilot import config
from copilot.db import repo

_DDL = """
CREATE TABLE IF NOT EXISTS outcome_appeals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    request_sha256  TEXT NOT NULL,
    approval_sha256 TEXT NOT NULL,
    readback_json   TEXT NOT NULL,
    status          TEXT NOT NULL,
    limitation_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcome_appeals_dispute ON outcome_appeals(dispute_id);
"""
STATUSES = ("sent_confirmed", "sent_unconfirmed", "failed")
COLUMNS = ("id", "dispute_id", "created_at", "request_json", "request_sha256", "approval_sha256", "readback_json",
           "status", "limitation_text")


def _connect() -> sqlite3.Connection:
    path = Path(config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def ensure_table() -> None:
    _connect().close()


def save_appeal(dispute_id: str, request: dict, request_sha: str, approval_sha: str, readback, status: str,
                limitation_text: str | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO outcome_appeals (dispute_id, created_at, request_json, request_sha256, approval_sha256, "
            "readback_json, status, limitation_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, time.strftime("%Y-%m-%dT%H:%M:%S%z"), json.dumps(request, sort_keys=True, ensure_ascii=False),
             request_sha, approval_sha, json.dumps(readback, sort_keys=True, ensure_ascii=False), status,
             limitation_text))
        conn.commit()
        saved = {k: conn.execute("SELECT * FROM outcome_appeals WHERE id = ?", (cur.lastrowid,)).fetchone()[k]
                 for k in COLUMNS}
    finally:
        conn.close()
    repo.log_action(config.db_path(), "vendor", "outcome_appeal", dispute_id,
                    {"request_sha256": request_sha, "approval_sha256": approval_sha, "status": status})
    return saved


def list_appeals(dispute_id: str) -> list:
    """Every stored appeal attempt of a dispute, oldest first."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM outcome_appeals WHERE dispute_id = ? ORDER BY id", (dispute_id,)).fetchall()
        return [{k: r[k] for k in COLUMNS} for r in rows]
    finally:
        conn.close()
