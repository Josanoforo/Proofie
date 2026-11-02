"""write_guard.reserve is atomic. Two threads call it with a delay injected between its SELECT and its INSERT (the
window a check-then-insert variant loses); exactly one gets the key. A control runs the same race against a naive variant
that does not hold BEGIN IMMEDIATE and shows the test can fail."""
import threading
import time

import pytest

from copilot.core import write_guard


class _SlowAfterSelect:
    """Wraps the connection write_guard._connect returns: sleeps right after the SELECT of the reservation row."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        result = self._conn.execute(sql, *args)
        if sql.lstrip().startswith("SELECT state, created_at"):
            time.sleep(0.15)
        return result

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _race(fn, subject, sha):
    claims, barrier = [], threading.Barrier(2)

    def go():
        barrier.wait()
        claims.append(fn(subject, "message", sha))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert len(claims) == 2
    return claims


def test_exactly_one_claim_when_a_delay_is_injected_between_select_and_insert(monkeypatch):
    real_connect = write_guard._connect
    monkeypatch.setattr(write_guard, "_connect", lambda: _SlowAfterSelect(real_connect()))
    claims = _race(write_guard.reserve, "PP-R-TST-RACE-1", "a" * 64)
    assert sorted(c.reserved for c in claims) == [False, True]


def test_a_naive_check_then_insert_variant_fails_the_same_race(monkeypatch):
    real_connect = write_guard._connect
    monkeypatch.setattr(write_guard, "_connect", lambda: _SlowAfterSelect(real_connect()))

    def naive_reserve(subject, action, sha):
        conn = write_guard._connect()                                     # autocommit: no lock is held between the two statements
        try:
            row = conn.execute("SELECT state, created_at FROM write_reservations WHERE subject_id=? AND action=? "
                               "AND request_sha256=?", (subject, action, sha)).fetchone()
            if row is not None:
                return write_guard.Claim(False, row["state"])
            now = time.time()
            conn.execute("INSERT OR REPLACE INTO write_reservations VALUES (?, ?, ?, ?, ?, ?)",
                         (subject, action, sha, "reserved", now, now))
            return write_guard.Claim(True)
        finally:
            conn.close()

    claims = _race(naive_reserve, "PP-R-TST-RACE-2", "b" * 64)
    assert [c.reserved for c in claims] == [True, True]                   # both own the key: the real test above would catch it
