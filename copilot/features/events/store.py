"""Owned SQLite tables. Network reads happen outside transactions; commit observations atomically."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from copilot import config
from .machine import Snapshot, choose, unknown_readback, instant, dispute_id

DDL = """
CREATE TABLE IF NOT EXISTS event_snapshots (
 dispute_id TEXT PRIMARY KEY, version TEXT NOT NULL, status TEXT NOT NULL, due TEXT,
 body_hash TEXT NOT NULL, source TEXT NOT NULL, checked_at TEXT NOT NULL, review_version TEXT, appeal_until TEXT);
CREATE TABLE IF NOT EXISTS event_log (
 event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, create_time TEXT, dispute_id TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_pending (
 event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, create_time TEXT, dispute_id TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_unknown (
 dispute_id TEXT PRIMARY KEY, baseline_version TEXT NOT NULL, expected_status TEXT NOT NULL,
 observation TEXT, observed_version TEXT, human_reviewed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS event_leases (key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
"""


class Store:
    def __init__(self, path=None):
        if not config.feature_enabled("events"):
            raise RuntimeError("events feature is disabled")
        self.path = Path(path if path is not None else config.db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript(DDL)

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def snapshots(self):
        with self.connection() as c:
            return [Snapshot(**dict(r)) for r in c.execute("SELECT * FROM event_snapshots")]

    def get(self, did):
        with self.connection() as c:
            r = c.execute("SELECT * FROM event_snapshots WHERE dispute_id=?", (did,)).fetchone()
            return Snapshot(**dict(r)) if r else None

    def meta(self, key, default=None):
        with self.connection() as c:
            r = c.execute("SELECT value FROM event_meta WHERE key=?", (key,)).fetchone()
            return r[0] if r else default

    @staticmethod
    def _set(c, key, value):
        c.execute("INSERT OR REPLACE INTO event_meta VALUES (?,?)", (key, str(value)))

    def set_meta(self, key, value):
        with self.connection() as c:
            self._set(c, key, value)

    def count(self, key, amount=1):
        with self.connection() as c:
            c.execute("INSERT INTO event_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+?",
                      (key, str(amount), amount))

    def has_event(self, event_id):
        with self.connection() as c:
            return c.execute("SELECT 1 FROM event_log WHERE event_id=?", (event_id,)).fetchone() is not None

    def queue_event(self, event, source, receiver_row=None):
        """Persist only the five allowed metadata fields before GET, so retention cannot erase pending work."""
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            eid = event["event_id"]
            if c.execute("SELECT 1 FROM event_log WHERE event_id=?", (eid,)).fetchone():
                return
            cursor = c.execute("INSERT OR IGNORE INTO event_pending VALUES (?,?,?,?,?)", tuple(event[k] for k in
                               ("event_id", "event_type", "create_time", "dispute_id", "sha256")))
            if cursor.rowcount:
                self._set(c, "pending_source:" + eid, source)
                if receiver_row is not None:
                    self._set(c, "pending_row:" + eid, receiver_row)

    def pending(self, source=None):
        with self.connection() as c:
            result = []
            for row in c.execute("SELECT * FROM event_pending ORDER BY event_id"):
                eid = row["event_id"]
                origin = c.execute("SELECT value FROM event_meta WHERE key=?", ("pending_source:" + eid,)).fetchone()
                watermark = c.execute("SELECT value FROM event_meta WHERE key=?", ("pending_row:" + eid,)).fetchone()
                if source is None or (origin and origin[0] == source):
                    result.append((dict(row), origin[0] if origin else "replay", int(watermark[0]) if watermark else None))
            return result

    def commit_get(self, observed, event=None, receiver_row=None):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            # One atomic observation covers snapshot, event receipt, unknown readback and receiver watermark.
            if event is not None and c.execute("SELECT 1 FROM event_log WHERE event_id=?", (event["event_id"],)).fetchone():
                return "duplicate_event"
            r = c.execute("SELECT * FROM event_snapshots WHERE dispute_id=?", (observed.dispute_id,)).fetchone()
            current = Snapshot(**dict(r)) if r else None
            selected, result = choose(current, observed)
            if result == "accepted":
                data = asdict(selected)
                c.execute("INSERT OR REPLACE INTO event_snapshots VALUES (?,?,?,?,?,?,?,?,?)", tuple(data.values()))
            if event is not None:
                c.execute("INSERT INTO event_log VALUES (?,?,?,?,?)", tuple(event[k] for k in
                          ("event_id", "event_type", "create_time", "dispute_id", "sha256")))
                c.execute("DELETE FROM event_pending WHERE event_id=?", (event["event_id"],))
                c.execute("DELETE FROM event_meta WHERE key IN (?,?)", ("pending_source:" + event["event_id"], "pending_row:" + event["event_id"]))
            if receiver_row is not None:
                old = c.execute("SELECT value FROM event_meta WHERE key='receiver_row'", ()).fetchone()
                self._set(c, "receiver_row", max(int(old[0]) if old else 0, receiver_row))
            unknown = c.execute("SELECT * FROM event_unknown WHERE dispute_id=? AND human_reviewed=0", (observed.dispute_id,)).fetchone()
            if unknown and (current is None or instant(observed.version) >= instant(current.version)):
                c.execute("UPDATE event_unknown SET observation=?,observed_version=? WHERE dispute_id=?",
                          (unknown_readback(dict(unknown), observed), observed.version, observed.dispute_id))
            c.execute("INSERT INTO event_meta VALUES (?,1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1", ("reads_" + result,))
            return result

    def mark_unknown(self, did, expected_status):
        """Local missing-ack hook only; does not send anything or connect to the send path."""
        if expected_status not in ("UNDER_REVIEW", "WAITING_FOR_SELLER_RESPONSE", "WAITING_FOR_BUYER_RESPONSE", "RESOLVED", "OPEN"):
            raise ValueError("expected readback must be a status token")
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT version FROM event_snapshots WHERE dispute_id=?", (did,)).fetchone()
            if current is None:
                raise ValueError("UNKNOWN requires a verified baseline")
            c.execute("INSERT OR REPLACE INTO event_unknown VALUES (?,?,?,NULL,NULL,0)", (did, current[0], expected_status))

    def unknown(self, did):
        with self.connection() as c:
            r = c.execute("SELECT * FROM event_unknown WHERE dispute_id=?", (did,)).fetchone()
            return dict(r) if r else None

    def human_review(self, did, observed_version):
        """Explicit human step bound to the latest GET version, never a timer or automatic resend."""
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM event_unknown WHERE dispute_id=?", (did,)).fetchone()
            current = c.execute("SELECT version FROM event_snapshots WHERE dispute_id=?", (did,)).fetchone()
            if r is None or r["observation"] is None or r["observed_version"] != observed_version or current is None or current[0] != observed_version:
                raise ValueError("human review needs a current GET observation")
            c.execute("UPDATE event_unknown SET human_reviewed=1 WHERE dispute_id=?", (did,))

    def receiver_rows(self):
        """Read all pending verified rows, including late verified replacements below the watermark."""
        with self.connection() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='webhook_events'").fetchone()
            if not exists:
                return []
            mark = c.execute("SELECT value FROM event_meta WHERE key='receiver_row'").fetchone()
            watermark = int(mark[0]) if mark else 0
            select = "SELECT id,event_id,event_type,dispute_id,received_at,body_sha256 FROM webhook_events WHERE verification='verified' "
            # Advance by row id while replaying older failures/verification replacements not yet committed.
            query = select + "AND id>? AND event_id NOT IN (SELECT event_id FROM event_log) UNION ALL " + select + "AND id<=? AND event_id NOT IN (SELECT event_id FROM event_log) ORDER BY id"
            return [dict(r) for r in c.execute(query, (watermark, watermark))]

    def local_ids(self):
        """Existing core case references, read-only; core has no dispute snapshot table."""
        found = set()
        with self.connection() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            found.update(r[0] for r in c.execute("SELECT DISTINCT dispute_id FROM event_pending"))
            for table in ("documents", "packages", "manual_fields", "auto_prep"):
                if table in tables:
                    found.update(r[0] for r in c.execute("SELECT DISTINCT dispute_id FROM " + table) if dispute_id(r[0]))
        return found

    @contextmanager
    def lease(self, key, now, seconds=180):
        owner = uuid4().hex
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT expires FROM event_leases WHERE key=?", (key,)).fetchone()
            if r is not None and r[0] > now:
                raise RuntimeError("owned read already in flight")
            c.execute("INSERT OR REPLACE INTO event_leases VALUES (?,?,?)", (key, owner, now + seconds))
        try:
            yield
        finally:
            with self.connection() as c:
                c.execute("DELETE FROM event_leases WHERE key=? AND owner=?", (key, owner))


def view(path):
    """Read-only UI: neither creates a database nor populates a table on page open."""
    path = Path(path)
    empty = {"rows": [], "counters": {}, "unknown_count": 0}
    if not path.is_file():
        return empty
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='event_snapshots'").fetchone():
            return empty
        rows = [dict(r) for r in c.execute("SELECT s.*,u.observation,u.human_reviewed FROM event_snapshots s LEFT JOIN event_unknown u USING(dispute_id) ORDER BY s.dispute_id")]
        unknowns = c.execute("SELECT COUNT(*) FROM event_unknown WHERE human_reviewed=0").fetchone()[0]
        return {"rows": rows, "counters": dict(c.execute("SELECT key,value FROM event_meta")), "unknown_count": unknowns}
    finally:
        c.close()
