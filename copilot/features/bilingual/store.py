"""The Spanish version of each evidence statement, in the app's SQLite database, own table `bilingual_statements`.

Created here with CREATE TABLE IF NOT EXISTS on every connection (copilot/db/schema.py is not touched). Append-only: a new
Spanish text is a new row and the latest row of a statement wins, so what was shown before stays traceable. Columns: id,
created_at, package_id, claim_id, en_sha256 (of the stripped English text the Spanish was written for: an English edit makes
the row stale), es, source (seller | ai), model (NULL for seller text). Nothing here is sent to PayPal."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

TABLE = "bilingual_statements"
SOURCES = ("seller", "ai")
DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    package_id  INTEGER NOT NULL,
    claim_id    INTEGER NOT NULL,
    en_sha256   TEXT NOT NULL,
    es          TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('seller', 'ai')),
    model       TEXT
);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_claim ON {TABLE}(claim_id);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_en ON {TABLE}(en_sha256);
"""
COLUMNS = ("id", "created_at", "package_id", "claim_id", "en_sha256", "es", "source", "model")


def _connect(db_path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def _row(r: sqlite3.Row | None) -> dict | None:
    return {k: r[k] for k in COLUMNS} if r is not None else None


def save(db_path, package_id: int, claim_id: int, en_sha256: str, es: str, source: str, model: str | None = None) -> dict:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO {TABLE} (created_at, package_id, claim_id, en_sha256, es, source, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S%z"), int(package_id), int(claim_id), en_sha256, es, source, model))
        conn.commit()
        return _row(conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def latest_by_claim(db_path, claim_ids) -> dict:
    """{claim_id: latest row} for the given statements."""
    ids = [int(i) for i in claim_ids]
    if not ids:
        return {}
    conn = _connect(db_path)
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT * FROM {TABLE} WHERE id IN (SELECT MAX(id) FROM {TABLE} WHERE claim_id IN ({marks}) "
                            "GROUP BY claim_id)", ids).fetchall()
        return {r["claim_id"]: _row(r) for r in rows}
    finally:
        conn.close()


def latest_by_english(db_path, package_ids, en_sha256s) -> dict:
    """{en_sha256: latest row} written for that exact English text in any of the given packages (the earlier versions of
    the same dispute), so a new version keeps the Spanish of an unchanged statement."""
    packages, hashes = [int(i) for i in package_ids], list(en_sha256s)
    if not packages or not hashes:
        return {}
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE id IN (SELECT MAX(id) FROM {TABLE} WHERE package_id IN ({','.join('?' * len(packages))}) "
            f"AND en_sha256 IN ({','.join('?' * len(hashes))}) GROUP BY en_sha256)", [*packages, *hashes]).fetchall()
        return {r["en_sha256"]: _row(r) for r in rows}
    finally:
        conn.close()
