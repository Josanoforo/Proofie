"""Drafts, in the app's SQLite database (config.db_path()), own table `draft_messages`.

Created here with CREATE TABLE IF NOT EXISTS on every connection (copilot/db/schema.py is not touched). Columns: id,
created_at, dispute_id (nullable), transaction_id (nullable), sheet_json (the fact sheet the model saw), en, es, model, seed,
raw_sha256 (of the model's raw answer), validation_ok (0/1), problems_json, source (model | edited). One audit row per model
call through repo.log_action with actor `system` (the model is never an actor; repo.log_action refuses anything else): the
model, the seed, the raw hash and the validation flag only, never the text."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from pathlib import Path

from copilot import config
from copilot.db import repo

_DDL = """
CREATE TABLE IF NOT EXISTS draft_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    dispute_id     TEXT,
    transaction_id TEXT,
    sheet_json     TEXT NOT NULL,
    en             TEXT NOT NULL,
    es             TEXT NOT NULL,
    model          TEXT NOT NULL,
    seed           INTEGER NOT NULL,
    raw_sha256     TEXT,
    validation_ok  INTEGER NOT NULL,
    problems_json  TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('model', 'edited'))
);
CREATE INDEX IF NOT EXISTS idx_draft_messages_dispute ON draft_messages(dispute_id);
"""
SOURCES = ("model", "edited")
COLUMNS = ("id", "created_at", "dispute_id", "transaction_id", "sheet_json", "en", "es", "model", "seed", "raw_sha256",
           "validation_ok", "problems_json", "source")


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
    d = {k: r[k] for k in COLUMNS}
    d["validation_ok"] = bool(d["validation_ok"])
    d["problems"] = json.loads(d["problems_json"])
    d["sheet"] = json.loads(d["sheet_json"])
    return d


def save_draft(dispute_id: str | None, transaction_id: str | None, sheet, en: str, es: str, model: str, seed: int,
               raw_sha256: str | None, validation_ok: bool, problems, source: str = "model") -> dict:
    """Stores a draft, valid or refused (a refused one is kept with its problems; the template stands)."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO draft_messages (created_at, dispute_id, transaction_id, sheet_json, en, es, model, seed, "
            "raw_sha256, validation_ok, problems_json, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S%z"), dispute_id, transaction_id,
             json.dumps(dataclasses.asdict(sheet), sort_keys=True, ensure_ascii=False), en, es, model, int(seed),
             raw_sha256, int(bool(validation_ok)), json.dumps(list(problems), ensure_ascii=False), source))
        conn.commit()
        saved = _row(conn.execute("SELECT * FROM draft_messages WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()
    if source == "model":
        repo.log_action(config.db_path(), "system", "draft_message", dispute_id or "-",
                        {"model": model, "seed": int(seed), "raw_sha256": raw_sha256, "validation_ok": bool(validation_ok)})
    return saved


def latest_draft(dispute_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM draft_messages WHERE dispute_id = ? ORDER BY id DESC LIMIT 1",
                           (dispute_id,)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()
