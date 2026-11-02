"""AttemptStore: the send-attempt registry behind an interface, so the storage
backend can change without changing pilot/step5_send_evidence.py's send() contract (claim -> read -> write).

pilot/step5_send_evidence.py's file-based registry (O_CREAT|O_EXCL under evidence/send_attempts/) is the
reference implementation this one must behave identically to; it is NOT reused as-is here because the app
needs one registry shared by all disputes in data/copilot.db, not a loose folder of JSON files. This file
is SQLite-only: the concurrency/crash-recovery guarantees are re-tested here from scratch in
tests/local/test_attempt_store.py (double claim, real threads, REAL separate OS processes via subprocess,
a claim left mid-flight across a simulated restart) because the tests for the file backend do not carry
over to a different storage engine.

Determinism note: claim-racing tests pre-warm the database first, so
they race only claim() - deterministic via busy_timeout + BEGIN IMMEDIATE. The separate, narrower cold-start
race (switching a brand-new file to WAL journal mode needs a brief exclusive lock) is made deterministic
in-process by `_wal_init_lock` below; the bounded retry in `_set_wal_mode_with_retry` is defense-in-depth
for genuinely separate OS processes creating the same new file at the same instant, which no in-process
lock can cover - it logs every retry and raises the clear `WalModeError` if attempts are exhausted.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

STATES = ("pending", "sent_unconfirmed", "confirmed", "failed", "blocked", "abandoned")

# Serialises WAL-mode switching across threads IN THIS PROCESS (see SQLiteAttemptStore.__init__): a Python
# lock makes same-process concurrent opens deterministically race-free, rather than relying on SQLite's own
# busy_timeout for a pragma that does not reliably honour it (see _set_wal_mode_with_retry). It cannot help
# across separate OS processes - that case still goes through the bounded retry below.
_wal_init_lock = threading.Lock()


class WalModeError(RuntimeError):
    """Raised when journal_mode=WAL could not be set after all retries - almost certainly because another
    OS process is creating/opening the same new database file at the same instant. Not raised for normal
    operation: once a database file is in WAL mode, re-opening it never needs to retry this."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS send_attempts (
    dispute_id      TEXT NOT NULL,
    package_sha256  TEXT NOT NULL,
    state           TEXT NOT NULL,
    request_id      TEXT,
    http_status     INTEGER,
    reason          TEXT,
    request_input_json TEXT,   -- the exact paypal_request_input this attempt POSTed/would POST - needed
                                -- to reconcile later (copilot/core/send_recovery.py) without trusting
                                -- whatever the package looks like NOW, which may have moved on since
    attachment_filenames_json TEXT,   -- the filenames this attempt attached (if any) - reconciliation
                                       -- must check PayPal's evidences[].documents[] for each of these by
                                       -- name, not just that the tracking number landed (a silently-dropped misnamed multipart file would otherwise look
                                       -- identical to a fully successful send)
    attachment_sha256_json TEXT,      -- the sha256 of each attached file, in the same order: a file is the same file
                                       -- by its bytes, never by its name
    readback_json   TEXT,              -- a confirmed write: {evidence_type, documents_present, paypal_date, read_at} from
                                       -- the read that confirmed it (names only); NULL for rows written before this column
    ts              TEXT NOT NULL,
    PRIMARY KEY (dispute_id, package_sha256)
);
"""


class AttemptStore(Protocol):
    def claim(self, dispute_id: str, package_sha: str) -> bool:
        """Atomically create a 'pending' record if none exists. False = one already exists (claimed by
        another thread/connection/process first); the caller must reconcile, never POST blindly."""
        ...

    def read(self, dispute_id: str, package_sha: str) -> dict | None:
        """Current attempt record, or None if no attempt has ever been claimed for this pair."""
        ...

    def reclaim(self, dispute_id: str, package_sha: str, expected_state: str, expected_ts: str) -> bool:
        """Atomically moves an existing row from exactly (expected_state, expected_ts) back to
        'pending', clearing request_id/http_status/reason - an UPDATE guarded by a WHERE on BOTH the state
        and the exact timestamp the caller read, so it only succeeds if nothing changed the row since. Two
        callers racing to reclaim the same stale 'failed' or stale 'pending' row: at most one UPDATE can
        match (the first one's COMMIT changes the ts, so the second's WHERE no longer matches) - exactly
        one reclaims it, exactly one POST follows. False means someone else already reclaimed it (or
        confirmed/changed it some other way) first; the caller must never POST in that case."""
        ...

    def abandon(self, dispute_id: str, package_sha: str, expected_ts: str, reason: str) -> bool:
        """Atomically moves a row from EXACTLY ('sent_unconfirmed',
        expected_ts) to 'abandoned' - an UPDATE guarded by state AND the exact ts the caller read, same
        shape as reclaim(). False means the row already moved on (confirmed by a just-run
        recover_dispute, or a newer send attempt entirely) since the caller read it - never abandon
        blindly on a stale read, and never abandon anything that is not still 'sent_unconfirmed'."""
        ...

    def supersede(self, dispute_id: str, package_sha: str, expected_ts: str, reason: str) -> bool:
        """Atomically moves a row from EXACTLY ('pending', expected_ts) to 'blocked' -
        same guarded shape as reclaim()/abandon(). Used to neutralize a stale 'pending' row that never
        reached a POST; False means it moved on since the caller listed it (most plausibly: the
        POST actually happened and it is now 'sent_unconfirmed') - the caller must re-read and treat that
        as a real in-flight attempt, never silently drop it."""
        ...

    def write(self, dispute_id: str, package_sha: str, state: dict) -> None:
        """Replace the record for this pair. `state` must include at least {"state": one of STATES}."""
        ...

    def write_partial(self, dispute_id: str, package_sha: str, state: dict) -> dict:
        """Atomically preserve confirmed/abandoned and return the effective stored record."""
        ...

    def list_by_dispute(self, dispute_id: str) -> list:
        """Every attempt ever claimed for this dispute (any state), oldest first - startup/crash recovery
        (copilot/core/send_recovery.py) scans this for anything still 'sent_unconfirmed'."""
        ...


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class SQLiteAttemptStore:
    """One row per (dispute_id, package_sha256); the PRIMARY KEY is the atomic-claim mechanism (an INSERT
    that violates it means someone else claimed first). Every mutation runs inside BEGIN IMMEDIATE so two
    connections racing the same claim serialise on SQLite's write lock instead of both succeeding - this is
    what makes `claim` safe across threads in one process (single uvicorn worker) and across separate
    connections standing in for separate processes."""

    def __init__(self, db_path: Path | str, busy_timeout_s: float = 30.0):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=busy_timeout_s, isolation_level=None)
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_s * 1000)}")
        # Switching journal mode needs a brief exclusive lock, which can itself raise "database is locked"
        # when several connections open concurrently for the first time (found stress-testing with 10
        # threads) - busy_timeout does not reliably cover this specific pragma. The
        # in-process lock makes same-process races deterministic (no retry ever needed there); the retry
        # below is defense-in-depth for genuinely separate OS processes racing to create the same new file.
        with _wal_init_lock:
            self._set_wal_mode_with_retry()
            self._conn.execute(SCHEMA)
            for ddl in ("ALTER TABLE send_attempts ADD COLUMN request_input_json TEXT",
                       "ALTER TABLE send_attempts ADD COLUMN attachment_filenames_json TEXT",
                       "ALTER TABLE send_attempts ADD COLUMN attachment_sha256_json TEXT",
                       "ALTER TABLE send_attempts ADD COLUMN readback_json TEXT"):
                try:
                    self._conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # already has it (CREATE TABLE above just made it fresh, or a prior run added it)

    def _set_wal_mode_with_retry(self, max_attempts: int = 10, base_delay_s: float = 0.05,
                                 conn=None) -> None:
        """conn defaults to self._conn; overridable so tests can exercise the retry/log/error behaviour
        directly with a fake connection, without relying on timing or on monkeypatching a C-extension
        type's method (sqlite3.Connection.execute cannot be set on an instance)."""
        if conn is None:
            conn = self._conn
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if attempt == max_attempts:
                    break
                logger.warning("SQLiteAttemptStore(%s): journal_mode=WAL attempt %d/%d failed (%s), retrying",
                               self.db_path, attempt, max_attempts, exc)
                time.sleep(base_delay_s * attempt)
        raise WalModeError(
            f"could not set journal_mode=WAL on {self.db_path} after {max_attempts} attempts - likely "
            f"another process creating/opening this same new database file at the same instant") from last_exc

    def close(self) -> None:
        self._conn.close()

    def claim(self, dispute_id: str, package_sha: str) -> bool:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO send_attempts (dispute_id, package_sha256, state, ts) VALUES (?, ?, 'pending', ?)",
                (dispute_id, package_sha, _now()))
            self._conn.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self._conn.execute("ROLLBACK")
            return False
        except sqlite3.OperationalError:
            # lock not acquired within busy_timeout: treat as "could not claim" (caller reconciles via
            # GET rather than assuming it is safe to POST), never as a silent success.
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            return False

    def reclaim(self, dispute_id: str, package_sha: str, expected_state: str, expected_ts: str) -> bool:
        try:
            # BEGIN IMMEDIATE itself can raise "database is locked" under contention (the
            # same busy_timeout-exceeded case claim() already guards) - it must be INSIDE the try, or that
            # raise propagates uncaught (no transaction was ever opened, so the bare ROLLBACK in the
            # except below would itself raise "no transaction is active", masking the real error).
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE send_attempts SET state='pending', request_id=NULL, http_status=NULL, reason=NULL, "
                "ts=? WHERE dispute_id=? AND package_sha256=? AND state=? AND ts=?",
                (_now(), dispute_id, package_sha, expected_state, expected_ts))
            ok = cur.rowcount == 1
        except sqlite3.OperationalError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass   # BEGIN IMMEDIATE itself failed: no transaction was ever opened, nothing to roll back
            return False
        self._conn.execute("COMMIT" if ok else "ROLLBACK")
        return ok

    def abandon(self, dispute_id: str, package_sha: str, expected_ts: str, reason: str) -> bool:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE send_attempts SET state='abandoned', reason=?, ts=? WHERE dispute_id=? "
                "AND package_sha256=? AND state='sent_unconfirmed' AND ts=?",
                (reason, _now(), dispute_id, package_sha, expected_ts))
            ok = cur.rowcount == 1
        except sqlite3.OperationalError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            return False
        self._conn.execute("COMMIT" if ok else "ROLLBACK")
        return ok

    def supersede(self, dispute_id: str, package_sha: str, expected_ts: str, reason: str) -> bool:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE send_attempts SET state='blocked', reason=?, ts=? WHERE dispute_id=? "
                "AND package_sha256=? AND state='pending' AND ts=?",
                (reason, _now(), dispute_id, package_sha, expected_ts))
            ok = cur.rowcount == 1
        except sqlite3.OperationalError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            return False
        self._conn.execute("COMMIT" if ok else "ROLLBACK")
        return ok

    def read(self, dispute_id: str, package_sha: str) -> dict | None:
        row = self._conn.execute(
            "SELECT dispute_id, package_sha256, state, request_id, http_status, reason, "
            "request_input_json, attachment_filenames_json, attachment_sha256_json, readback_json, ts FROM send_attempts "
            "WHERE dispute_id = ? AND package_sha256 = ?", (dispute_id, package_sha)).fetchone()
        return self._row(row)

    def list_by_dispute(self, dispute_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT dispute_id, package_sha256, state, request_id, http_status, reason, "
            "request_input_json, attachment_filenames_json, attachment_sha256_json, readback_json, ts FROM send_attempts "
            "WHERE dispute_id = ? ORDER BY ts", (dispute_id,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        keys = ("dispute_id", "package_sha256", "state", "request_id", "http_status", "reason",
                "request_input_json", "attachment_filenames_json", "attachment_sha256_json", "readback_json", "ts")
        d = dict(zip(keys, row))
        req = d.pop("request_input_json")
        d["request_input"] = json.loads(req) if req else None
        files = d.pop("attachment_filenames_json")
        d["attachment_filenames"] = json.loads(files) if files else []
        shas = d.pop("attachment_sha256_json")
        d["attachment_sha256"] = json.loads(shas) if shas else []
        readback = d.pop("readback_json")
        d["readback"] = json.loads(readback) if readback else None      # None is dropped below: absent for older rows
        return {k: v for k, v in d.items() if v is not None or k in ("dispute_id", "package_sha256",
                                                                      "state", "ts", "request_input",
                                                                      "attachment_filenames",
                                                                      "attachment_sha256")}

    def write(self, dispute_id: str, package_sha: str, state: dict) -> None:
        self._write(dispute_id, package_sha, state)

    def write_partial(self, dispute_id: str, package_sha: str, state: dict) -> dict:
        if state.get('state') != 'sent_unconfirmed':
            raise ValueError('Partial reconciliation requires sent_unconfirmed')
        return self._write(dispute_id, package_sha, state, preserve_terminal=True)

    def _write(self, dispute_id: str, package_sha: str, state: dict,
               preserve_terminal: bool = False) -> dict:
        if state.get("state") not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {state.get('state')!r}")
        rec = {"request_id": None, "http_status": None, "reason": None, "request_input": None,
              "attachment_filenames": None, "attachment_sha256": None, "readback": None, **state}
        req_json = json.dumps(rec["request_input"], ensure_ascii=False) if rec["request_input"] is not None else None
        files_json = (json.dumps(rec["attachment_filenames"], ensure_ascii=False)
                     if rec["attachment_filenames"] is not None else None)
        shas_json = (json.dumps(rec["attachment_sha256"]) if rec["attachment_sha256"] is not None else None)
        readback_json = json.dumps(rec["readback"], ensure_ascii=False) if rec["readback"] is not None else None
        self._conn.execute("BEGIN IMMEDIATE")
        rec = {"ts": _now(), **rec}
        self._conn.execute(
            "INSERT INTO send_attempts (dispute_id, package_sha256, state, request_id, http_status, reason, "
            "request_input_json, attachment_filenames_json, attachment_sha256_json, readback_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dispute_id, package_sha256) DO UPDATE SET "
            "state=excluded.state, "
            # request_id is "did THIS registry row ever actually POST" - once true, a later write
            # that simply omits it (e.g. reconcile_after_send's "confirmed"/"sent_unconfirmed" writes,
            # recover_dispute's "confirmed" write) must never silently erase it back to NULL. Every
            # caller that DOES mean to set a genuinely new request_id still wins here, since COALESCE only
            # falls back to the stored value when the incoming one is NULL.
            "request_id=COALESCE(excluded.request_id, send_attempts.request_id), "
            "http_status=excluded.http_status, reason=excluded.reason, "
            "request_input_json=COALESCE(excluded.request_input_json, send_attempts.request_input_json), "
            "attachment_filenames_json=COALESCE(excluded.attachment_filenames_json, "
            "send_attempts.attachment_filenames_json), "
            "attachment_sha256_json=COALESCE(excluded.attachment_sha256_json, "
            "send_attempts.attachment_sha256_json), "
            "readback_json=COALESCE(excluded.readback_json, send_attempts.readback_json), "
            "ts=excluded.ts" + (" WHERE send_attempts.state NOT IN ('confirmed', 'abandoned')"
                               if preserve_terminal else ""),
            (dispute_id, package_sha, rec["state"], rec["request_id"], rec["http_status"], rec["reason"],
             req_json, files_json, shas_json, readback_json, rec["ts"]))
        effective = self.read(dispute_id, package_sha)
        self._conn.execute("COMMIT")
        return effective
