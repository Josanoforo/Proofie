"""Case-scoped local drawer, deliberately separate from the case ledger and documents."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def _connect(path):
    target = Path(path).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise OSError("Third-party statement drawer is unavailable.")
    conn = sqlite3.connect(target, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS statements (id INTEGER PRIMARY KEY, dispute_id TEXT NOT NULL, "
                 "source_party TEXT NOT NULL, kind TEXT NOT NULL, content_type TEXT NOT NULL, filename TEXT, "
                 "payload BLOB NOT NULL, recorded_at TEXT NOT NULL, uncorroborated INTEGER NOT NULL DEFAULT 1 "
                 "CHECK(uncorroborated=1))")
    return conn


def add(path, dispute_id: str, source_party: str, kind: str, content_type: str,
        filename: str | None, payload: bytes) -> int:
    if source_party not in {"buyer", "packer", "reception", "neighbor", "carrier"}:
        raise ValueError("Invalid source party.")
    if kind not in ("statement", "photo") or not isinstance(payload, bytes) or not payload:
        raise ValueError("Invalid third-party statement.")
    recorded_at = datetime.now(timezone.utc).isoformat()
    conn = _connect(path)
    try:
        cur = conn.execute("INSERT INTO statements(dispute_id,source_party,kind,content_type,filename,payload,recorded_at) "
                           "VALUES(?,?,?,?,?,?,?)", (dispute_id, source_party, kind, content_type, filename, payload, recorded_at))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_for_case(path, dispute_id: str) -> list[dict]:
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT id,source_party,kind,content_type,filename,payload,recorded_at,uncorroborated "
                            "FROM statements WHERE dispute_id=? ORDER BY id", (dispute_id,)).fetchall()
        return [{**dict(row), "payload": bytes(row["payload"]), "uncorroborated": bool(row["uncorroborated"])}
                for row in rows]
    finally:
        conn.close()


def get_for_case(path, dispute_id: str, statement_id: int) -> dict | None:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM statements WHERE id=? AND dispute_id=?",
                           (statement_id, dispute_id)).fetchone()
        return ({**dict(row), "payload": bytes(row["payload"]), "uncorroborated": bool(row["uncorroborated"])}
                if row else None)
    finally:
        conn.close()
