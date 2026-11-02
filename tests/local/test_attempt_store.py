"""The SQLite AttemptStore is a different storage engine from pilot's file-based
registry, so its concurrency/crash-recovery guarantees are re-proven here from scratch, against SQLite
specifically - double claim, real threads racing, REAL separate OS processes (subprocess, not just
separate connections in this interpreter) racing, and a claim left mid-flight across a simulated restart.

Determinism: every claim-racing test below pre-warms the
database (one single-threaded open+close) before spawning any concurrent worker, so what is actually being
raced is claim() - a BEGIN IMMEDIATE + INSERT against a UNIQUE key, which SQLite's busy_timeout handles
deterministically. The WAL-mode-transition race (cold start on a brand-new file) is a separate, narrower
concern covered on its own below, made deterministic in-process by a threading.Lock in attempt_store.py;
no test here depends on a retry "usually" winning a timing race.
"""
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.attempt_store import STATES, SQLiteAttemptStore, WalModeError

DID = "PP-R-TEST-0001"
SHA = "abc123"
_WORKER_SCRIPT = Path(__file__).resolve().parent / "_attempt_store_claim_worker.py"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "copilot.db"


def _prewarm(db_path) -> None:
    """Single-threaded schema/WAL setup, so concurrency tests race only claim(), never the (separate,
    already-covered) cold-start WAL-mode-transition path."""
    SQLiteAttemptStore(db_path).close()


def test_read_returns_none_before_any_claim(db_path):
    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, SHA) is None


def test_claim_creates_pending_state_readable_immediately(db_path):
    store = SQLiteAttemptStore(db_path)
    assert store.claim(DID, SHA) is True
    rec = store.read(DID, SHA)
    assert rec["state"] == "pending" and rec["dispute_id"] == DID and rec["package_sha256"] == SHA


def test_claim_fails_the_second_time_same_connection(db_path):
    store = SQLiteAttemptStore(db_path)
    assert store.claim(DID, SHA) is True
    assert store.claim(DID, SHA) is False
    assert store.read(DID, SHA)["state"] == "pending"   # second failed claim never touched the row


def test_write_upserts_and_persists(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "rid-1"})
    rec = store.read(DID, SHA)
    assert rec["state"] == "sent_unconfirmed" and rec["request_id"] == "rid-1"
    store.write(DID, SHA, {"state": "confirmed"})
    assert store.read(DID, SHA)["state"] == "confirmed"


def test_write_rejects_an_invalid_state_value(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    with pytest.raises(ValueError):
        store.write(DID, SHA, {"state": "not_a_real_state"})


@pytest.mark.parametrize("state", STATES)
def test_write_accepts_every_documented_state(db_path, state):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": state})
    assert store.read(DID, SHA)["state"] == state


# ---- concurrency: real threads, each its own connection (the realistic FastAPI/single-worker shape) ----
def test_two_threads_racing_the_same_claim_only_one_succeeds(db_path):
    _prewarm(db_path)
    results = []

    def worker():
        store = SQLiteAttemptStore(db_path)
        results.append(store.claim(DID, SHA))
        store.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True], results
    # exactly one row exists, in 'pending' (whichever thread won), never a second row or a corrupted state
    store = SQLiteAttemptStore(db_path)
    rec = store.read(DID, SHA)
    assert rec is not None and rec["state"] == "pending"


def test_twenty_threads_opening_a_fresh_store_concurrently_never_crashes(db_path):
    """Regression: opening SQLiteAttemptStore sets WAL mode, which itself needs a brief exclusive lock;
    enough simultaneous first-time opens could raise 'database is locked' from inside __init__ (found via
    the 10-thread claim race below, before the fix). Deliberately NOT pre-warmed - this is the one test
    that exercises the cold-start WAL-mode-transition path on purpose. Deterministic by construction, not
    by luck: attempt_store.py serialises this specific section with a threading.Lock, so within one
    process no two threads ever actually contend for the pragma - this asserts that guarantee holds, not
    that a timing-dependent retry "usually" wins."""
    errors = []

    def worker():
        try:
            store = SQLiteAttemptStore(db_path)
            store.close()
        except Exception as exc:  # noqa: BLE001 - we want to catch and report literally anything here
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"{len(errors)}/20 connection opens raised: {errors}"


def test_ten_threads_racing_the_same_claim_exactly_one_succeeds(db_path):
    """Wider race than the minimum 2, to catch a lock-handling bug that only shows up under more
    contention (e.g. a busy_timeout too short, or a missed BEGIN IMMEDIATE)."""
    _prewarm(db_path)
    results = []
    lock = threading.Lock()

    def worker():
        store = SQLiteAttemptStore(db_path)
        ok = store.claim(DID, SHA)
        with lock:
            results.append(ok)
        store.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1, f"expected exactly 1 successful claim, got {sum(results)}/10"


# ---- reclaim: atomically move a stale 'failed'/'pending' row back to 'pending' ---------------------
def test_reclaim_succeeds_when_state_and_ts_match_and_resets_to_pending(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "failed", "reason": "PayPal rejected it", "http_status": 422})
    existing = store.read(DID, SHA)
    assert store.reclaim(DID, SHA, "failed", existing["ts"]) is True
    rec = store.read(DID, SHA)
    assert rec["state"] == "pending"
    assert rec.get("request_id") is None and rec.get("http_status") is None and rec.get("reason") is None


def test_reclaim_fails_when_the_state_has_already_moved_on(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "failed", "reason": "x"})
    existing = store.read(DID, SHA)
    store.write(DID, SHA, {"state": "confirmed", "request_input": {"evidences": []}})   # moved on since
    assert store.reclaim(DID, SHA, "failed", existing["ts"]) is False
    assert store.read(DID, SHA)["state"] == "confirmed"   # untouched by the failed reclaim attempt


def test_reclaim_fails_when_the_ts_no_longer_matches_even_with_the_right_state(db_path):
    """Guards against reclaiming a row that LOOKS like the same stale state but was actually already
    reclaimed and failed again in between (two different 'failed' episodes, not the one the caller read)."""
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "failed", "reason": "first rejection"})
    stale_ts = store.read(DID, SHA)["ts"]
    store.write(DID, SHA, {"state": "failed", "reason": "second rejection", "ts": "2099-01-01T00:00:00+0000"})
    assert store.reclaim(DID, SHA, "failed", stale_ts) is False
    assert store.read(DID, SHA)["reason"] == "second rejection"   # untouched


def test_reclaim_works_from_a_stale_blocked_row_too(db_path):
    """A 'blocked' row (a local refusal, never a real PayPal rejection) must be just as retriable as
    'failed' once whatever caused it is gone."""
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "blocked", "reason": "no provide-evidence link yet"})
    existing = store.read(DID, SHA)
    assert store.reclaim(DID, SHA, "blocked", existing["ts"]) is True
    assert store.read(DID, SHA)["state"] == "pending"


# ---- supersede: atomically moves a stale 'pending' row (never reached a POST) to
# 'blocked', guarded so it can never overwrite a row that actually went on to POST in the meantime -------
def test_supersede_succeeds_from_pending_and_sets_the_reason(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    existing = store.read(DID, SHA)
    assert store.supersede(DID, SHA, existing["ts"], "superseded, never sent") is True
    rec = store.read(DID, SHA)
    assert rec["state"] == "blocked" and rec["reason"] == "superseded, never sent"


def test_supersede_fails_when_the_state_is_not_pending(db_path):
    """The exact race the guarded supersede exists for: the row actually went on to POST (now
    'sent_unconfirmed') between when the caller listed it and this call - must never be clobbered."""
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "req-1"})
    ts = store.read(DID, SHA)["ts"]
    assert store.supersede(DID, SHA, ts, "superseded, never sent") is False
    assert store.read(DID, SHA)["state"] == "sent_unconfirmed"


def test_supersede_fails_when_the_ts_no_longer_matches(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    stale_ts = store.read(DID, SHA)["ts"]
    store.write(DID, SHA, {"state": "pending", "ts": "2099-01-01T00:00:00+0000"})
    assert store.supersede(DID, SHA, stale_ts, "superseded, never sent") is False
    assert store.read(DID, SHA)["state"] == "pending"


def test_two_threads_racing_the_same_supersede_exactly_one_succeeds(db_path):
    _prewarm(db_path)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    stale_ts = store.read(DID, SHA)["ts"]
    store.close()

    results = []
    lock = threading.Lock()

    def worker():
        s = SQLiteAttemptStore(db_path)
        ok = s.supersede(DID, SHA, stale_ts, "superseded, never sent")
        with lock:
            results.append(ok)
        s.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1, f"expected exactly 1 successful supersede, got {sum(results)}/10"
    assert SQLiteAttemptStore(db_path).read(DID, SHA)["state"] == "blocked"


def test_two_threads_racing_the_same_reclaim_exactly_one_succeeds(db_path):
    _prewarm(db_path)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "failed", "reason": "x"})
    stale_ts = store.read(DID, SHA)["ts"]
    store.close()

    results = []
    lock = threading.Lock()

    def worker():
        s = SQLiteAttemptStore(db_path)
        ok = s.reclaim(DID, SHA, "failed", stale_ts)
        with lock:
            results.append(ok)
        s.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1, f"expected exactly 1 successful reclaim, got {sum(results)}/10"
    assert SQLiteAttemptStore(db_path).read(DID, SHA)["state"] == "pending"


def test_request_id_is_preserved_across_a_later_write_that_omits_it(db_path):
    """request_id means 'this registry row actually POSTed' - a later write (e.g. reconcile's
    'confirmed' write, which never repeats request_id) must never silently erase it, the same guarantee
    write() already gave request_input/attachment_filenames."""
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "req-1",
                          "request_input": {"evidences": []}})
    store.write(DID, SHA, {"state": "confirmed", "request_input": {"evidences": []}})   # omits request_id
    assert store.read(DID, SHA)["request_id"] == "req-1"


def test_a_genuinely_new_request_id_still_overwrites_the_old_one(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "pending", "request_id": "req-1"})
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "req-2"})
    assert store.read(DID, SHA)["request_id"] == "req-2"


# ---- sequential connections: a NEW connection object per step, standing in for a restart, not for ----
# ---- genuine concurrency (see test_two_real_os_processes_... below for that) -------------------------
def test_a_second_connection_opened_after_the_first_claim_sees_it(db_path):
    conn_a = SQLiteAttemptStore(db_path)
    assert conn_a.claim(DID, SHA) is True
    conn_b = SQLiteAttemptStore(db_path)   # opened independently, after A's claim already committed
    assert conn_b.claim(DID, SHA) is False
    # the second opener must reconcile by reading, not by assuming failure - and it sees A's real state
    assert conn_b.read(DID, SHA)["state"] == "pending"


def test_a_write_from_one_connection_is_visible_to_a_new_connection(db_path):
    conn_a = SQLiteAttemptStore(db_path)
    conn_a.claim(DID, SHA)
    conn_a.write(DID, SHA, {"state": "confirmed", "http_status": 200})
    conn_a.close()   # process A exits
    conn_b = SQLiteAttemptStore(db_path)   # a fresh process/connection opens the same db file
    rec = conn_b.read(DID, SHA)
    assert rec["state"] == "confirmed" and rec["http_status"] == 200


# ---- REAL OS processes (two connections in this interpreter are not two processes) ---
def test_two_real_os_processes_racing_the_same_claim_only_one_succeeds(db_path):
    """Launches two actual `python` subprocesses (not threads, not multiprocessing workers sharing this
    interpreter's state) that each call claim() on the same (dispute_id, package_sha) pair, started back
    to back so their claim attempts genuinely overlap. Pre-warmed so only the claim() race is exercised."""
    _prewarm(db_path)
    args = [sys.executable, str(_WORKER_SCRIPT), str(db_path), DID, SHA]
    p1 = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out1, err1 = p1.communicate(timeout=30)
    out2, err2 = p2.communicate(timeout=30)
    assert p1.returncode == 0, f"worker 1 crashed: {err1}"
    assert p2.returncode == 0, f"worker 2 crashed: {err2}"
    results = sorted([out1.strip(), out2.strip()])
    assert results == ["False", "True"], (out1, out2, err1, err2)
    # exactly one row, in 'pending' - no corruption, no duplicate, regardless of which process won
    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, SHA)["state"] == "pending"


# ---- crash / restart recovery: what a new connection sees after the "old process" stopped mid-flight ----
def test_crash_right_after_claim_before_any_write_leaves_pending_for_the_next_reader(db_path):
    """Simulates a process that claimed the attempt and died before writing sent_unconfirmed (e.g. crashed
    building the multipart request). A restarted process must see 'pending', not nothing and not a false
    'confirmed' - 'pending' is the signal that this attempt was claimed but nothing is known to have been
    sent, which send()'s own reconciliation logic (pilot/step5_send_evidence.py) treats as safe to retry
    once preconditions are re-checked."""
    dying_process = SQLiteAttemptStore(db_path)
    assert dying_process.claim(DID, SHA) is True
    # (the dying process never calls write() again - that's the crash)
    dying_process.close()

    restarted_process = SQLiteAttemptStore(db_path)
    rec = restarted_process.read(DID, SHA)
    assert rec is not None and rec["state"] == "pending"


def test_crash_between_post_and_verification_get_leaves_sent_unconfirmed_durably(db_path):
    """Simulates a process that POSTed (so it recorded sent_unconfirmed, the write send() does BEFORE the
    network call per pilot/step5_send_evidence.py) and then crashed before the verification GET."""
    dying_process = SQLiteAttemptStore(db_path)
    dying_process.claim(DID, SHA)
    dying_process.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "rid-42"})
    dying_process.close()

    restarted_process = SQLiteAttemptStore(db_path)
    rec = restarted_process.read(DID, SHA)
    assert rec["state"] == "sent_unconfirmed" and rec["request_id"] == "rid-42"
    # the restarted process can now reconcile via a fresh PayPal GET (send()'s job, not AttemptStore's) and
    # transition to confirmed/failed - AttemptStore's job here is only that the durable state survived.
    restarted_process.write(DID, SHA, {"state": "confirmed"})
    assert restarted_process.read(DID, SHA)["state"] == "confirmed"


# ---- WAL retry: directed tests of the retry/log/clear-error behaviour itself, not timing-dependent -----
class _FlakyConn:
    """Fake standing in for sqlite3.Connection: its execute() fails the pragma call a fixed number of
    times before succeeding. sqlite3.Connection.execute cannot be monkeypatched on an instance (C
    extension type), so _set_wal_mode_with_retry takes an injectable `conn` specifically for this."""
    def __init__(self, fail_times: int):
        self.fail_times, self.calls = fail_times, 0

    def execute(self, sql, *a, **kw):
        if sql == "PRAGMA journal_mode = WAL":
            self.calls += 1
            if self.calls <= self.fail_times:
                raise sqlite3.OperationalError("database is locked")


class _AlwaysLockedConn:
    def execute(self, sql, *a, **kw):
        if sql == "PRAGMA journal_mode = WAL":
            raise sqlite3.OperationalError("database is locked")


class _FailsOnBeginImmediateConn:
    """Unlike _AlwaysLockedConn below (which only fails the WAL pragma specifically), this fails every
    call - standing in for a connection where BEGIN IMMEDIATE itself cannot acquire the write lock."""
    def execute(self, sql, *a, **kw):
        raise sqlite3.OperationalError("database is locked")


def test_reclaim_survives_begin_immediate_failing_without_crashing(db_path):
    """BEGIN IMMEDIATE itself can raise sqlite3.OperationalError (lock
    contention) - it must be caught the same way claim()'s already is, never propagate uncaught."""
    store = SQLiteAttemptStore(db_path)
    store._conn = _FailsOnBeginImmediateConn()
    assert store.reclaim(DID, SHA, "failed", "2026-01-01T00:00:00+0000") is False


def test_abandon_survives_begin_immediate_failing_without_crashing(db_path):
    store = SQLiteAttemptStore(db_path)
    store._conn = _FailsOnBeginImmediateConn()
    assert store.abandon(DID, SHA, "2026-01-01T00:00:00+0000", "some reason") is False


def test_wal_retry_recovers_from_transient_failures_and_logs_each_attempt(db_path, caplog):
    store = SQLiteAttemptStore(db_path)   # real store just to call the method on; conn is overridden below
    fake = _FlakyConn(fail_times=2)
    with caplog.at_level("WARNING"):
        store._set_wal_mode_with_retry(base_delay_s=0.0, conn=fake)
    assert fake.calls == 3   # failed twice, succeeded on the 3rd
    assert sum("journal_mode=WAL attempt" in r.message for r in caplog.records) == 2


def test_wal_retry_raises_a_clear_error_after_exhausting_attempts(db_path):
    store = SQLiteAttemptStore(db_path)
    with pytest.raises(WalModeError, match="journal_mode=WAL"):
        store._set_wal_mode_with_retry(max_attempts=3, base_delay_s=0.0, conn=_AlwaysLockedConn())


def test_different_dispute_or_package_hash_is_a_different_attempt(db_path):
    store = SQLiteAttemptStore(db_path)
    assert store.claim(DID, SHA) is True
    assert store.claim(DID, "other-sha") is True          # same dispute, different package: independent
    assert store.claim("PP-R-OTHER", SHA) is True          # different dispute, same hash: independent
    assert store.read(DID, SHA)["state"] == "pending"
    assert store.read(DID, "other-sha")["state"] == "pending"
    assert store.read("PP-R-OTHER", SHA)["state"] == "pending"


# ---- request_input round-trip (reconciliation needs the exact content, not just its hash) -----------
REQ_INPUT = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                                "tracking_number": "DEMO1"}]},
                           "notes": "demo"}]}


def test_request_input_round_trips_through_write_and_read(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_input": REQ_INPUT})
    assert store.read(DID, SHA)["request_input"] == REQ_INPUT


def test_request_input_is_preserved_across_a_later_write_that_omits_it(db_path):
    """A state transition (e.g. sent_unconfirmed -> confirmed) does not need to repeat request_input -
    COALESCE in the UPDATE keeps the one already on file rather than nulling it out."""
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_input": REQ_INPUT})
    store.write(DID, SHA, {"state": "confirmed"})
    rec = store.read(DID, SHA)
    assert rec["state"] == "confirmed" and rec["request_input"] == REQ_INPUT


def test_request_input_is_none_when_never_set(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    assert store.read(DID, SHA)["request_input"] is None


# ---- attachment_filenames round-trip (needed to re-check PayPal's documents[] during recovery) ------------
def test_attachment_filenames_round_trips_and_defaults_to_empty_list(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    assert store.read(DID, SHA)["attachment_filenames"] == []
    store.write(DID, SHA, {"state": "sent_unconfirmed", "attachment_filenames": ["a.pdf", "b.pdf"]})
    assert store.read(DID, SHA)["attachment_filenames"] == ["a.pdf", "b.pdf"]


def test_attachment_filenames_are_preserved_across_a_later_write_that_omits_them(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "attachment_filenames": ["a.pdf"]})
    store.write(DID, SHA, {"state": "confirmed"})
    rec = store.read(DID, SHA)
    assert rec["state"] == "confirmed" and rec["attachment_filenames"] == ["a.pdf"]


# ---- list_by_dispute (startup/crash recovery scans this) --------------------------------------------------
def test_list_by_dispute_returns_only_that_disputes_attempts_oldest_first(db_path):
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "sha-a")
    store.claim(DID, "sha-b")
    store.claim("PP-R-OTHER", "sha-c")
    rows = store.list_by_dispute(DID)
    assert [r["package_sha256"] for r in rows] == ["sha-a", "sha-b"]


def test_list_by_dispute_is_empty_for_an_unknown_dispute(db_path):
    store = SQLiteAttemptStore(db_path)
    assert store.list_by_dispute("PP-R-NEVER-SEEN") == []


# the writer must timestamp its row after obtaining the real lock.
def test_write_waiting_past_stale_boundary_samples_clock_after_lock(db_path, monkeypatch):
    from copilot.core import attempt_store
    _prewarm(db_path)
    ready, start, waiting = threading.Event(), threading.Event(), threading.Event()
    clock = ["2026-10-09T12:00:00+00:00"]
    errors = []
    monkeypatch.setattr(attempt_store, "_now", lambda: clock[0])

    class ObserveBegin:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                waiting.set()
            return self.connection.execute(sql, *args)

        def close(self):
            self.connection.close()

    def writer():
        store = SQLiteAttemptStore(db_path)
        store._conn = ObserveBegin(store._conn)
        ready.set()
        try:
            assert start.wait(5)
            store.write(DID, SHA, {"state": "pending"})
        except BaseException as error:
            errors.append(error)
        finally:
            store.close()

    worker = threading.Thread(target=writer)
    worker.start()
    assert ready.wait(5)
    blocker = sqlite3.connect(db_path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        start.set()
        assert waiting.wait(5)
        # A deterministic simulated clock crosses the 120-second stale boundary
        # while a separate real SQLite connection owns the write lock.
        clock[0] = "2026-10-09T12:03:00+00:00"
        blocker.execute("COMMIT")
    finally:
        blocker.close()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    store = SQLiteAttemptStore(db_path)
    try:
        assert store.read(DID, SHA)["ts"] == clock[0]
    finally:
        store.close()


def test_write_preserves_explicit_timestamp(db_path):
    store = SQLiteAttemptStore(db_path)
    try:
        explicit = "2026-10-09T10:00:00+00:00"
        store.write(DID, SHA, {"state": "confirmed", "ts": explicit})
        assert store.read(DID, SHA)["ts"] == explicit
    finally:
        store.close()
