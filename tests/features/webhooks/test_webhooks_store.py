"""webhook_events on a throwaway database (conftest isolates config.db_path() per test)."""
import sqlite3

import pytest

from copilot import config
from copilot.db import repo
from copilot.features.webhooks import store

SHA = "a" * 64


def _save(event_id="WH-TEST-1", verification="verified", **kw):
    args = dict(event_id=event_id, event_type="CUSTOMER.DISPUTE.CREATED", resource_type="dispute",
                dispute_id="PP-R-TEST-0000001", transmission_id="TRANS-TEST-1", body_sha256=SHA,
                verification=verification, readback_status="WAITING_FOR_SELLER_RESPONSE", readback_sha256="b" * 64)
    args.update(kw)
    return store.save_event(**args)


def test_save_and_get_round_trip_with_every_column():
    saved = _save()
    assert saved["id"] == 1 and saved["received_at"]
    got = store.get_event("WH-TEST-1")
    assert got == saved
    assert (got["event_type"], got["resource_type"], got["dispute_id"], got["transmission_id"], got["body_sha256"],
            got["verification"], got["readback_status"], got["readback_sha256"], got["limitation_text"]) == (
        "CUSTOMER.DISPUTE.CREATED", "dispute", "PP-R-TEST-0000001", "TRANS-TEST-1", SHA, "verified",
        "WAITING_FOR_SELLER_RESPONSE", "b" * 64, None)
    assert store.get_event("WH-NOPE") is None


def test_list_is_newest_first_and_limited():
    for i in range(5):
        _save(event_id=f"WH-TEST-{i}")
    assert [e["event_id"] for e in store.list_events()] == [f"WH-TEST-{i}" for i in (4, 3, 2, 1, 0)]
    assert [e["event_id"] for e in store.list_events(limit=2)] == ["WH-TEST-4", "WH-TEST-3"]


def test_events_for_a_dispute_are_newest_first_and_only_that_dispute():
    _save("WH-TEST-1")
    _save("WH-TEST-2", dispute_id="PP-R-TEST-0000002")
    _save("WH-TEST-3")
    assert [e["event_id"] for e in store.events_for_dispute("PP-R-TEST-0000001")] == ["WH-TEST-3", "WH-TEST-1"]
    assert store.events_for_dispute("PP-R-TEST-0000009") == []


def test_the_event_id_is_unique_for_a_verified_event():
    _save()
    with pytest.raises(sqlite3.IntegrityError):
        _save()
    assert len(store.list_events()) == 1


@pytest.mark.parametrize("first", ["failed", "rejected_headers", "limitation", "unverified"])
def test_a_row_that_is_not_verified_is_replaced_by_the_retry_never_ignored(first):
    _save(verification=first, readback_status=None, readback_sha256=None, limitation_text="text")
    again = _save(verification="verified")
    assert len(store.list_events()) == 1 and again["verification"] == "verified" and again["limitation_text"] is None
    assert again["readback_status"] == "WAITING_FOR_SELLER_RESPONSE"


def test_unknown_verification_value_is_refused():
    with pytest.raises(ValueError):
        _save(verification="duplicate")


def test_a_null_dispute_id_is_stored_and_listed():
    _save(dispute_id=None, readback_status=None, readback_sha256=None, event_type="PAYMENT.CAPTURE.COMPLETED")
    assert store.list_events()[0]["dispute_id"] is None


def test_one_audit_row_per_stored_event_actor_system_with_hashes_and_statuses_only():
    _save()
    audit = [a for a in repo.list_audit(config.db_path(), "PP-R-TEST-0000001") if a["action"] == "webhook_event"]
    assert len(audit) == 1 and audit[0]["actor"] == "system"
    assert audit[0]["detail"] == {"event_type": "CUSTOMER.DISPUTE.CREATED", "verification": "verified",
                                  "body_sha256": SHA, "readback_status": "WAITING_FOR_SELLER_RESPONSE"}
    _save("WH-TEST-2", dispute_id=None)
    assert [a["dispute_id"] for a in repo.list_audit(config.db_path(), "-") if a["action"] == "webhook_event"] == ["-"]


def test_unverified_round_trip_has_no_readback_and_cannot_degrade_verified():
    unverified = _save(verification="unverified", readback_status=None, readback_sha256=None)
    assert store.get_event("WH-TEST-1") == unverified
    assert unverified["verification"] == "unverified"
    assert unverified["readback_status"] is None and unverified["readback_sha256"] is None
    verified = _save(verification="verified")
    assert verified["id"] == unverified["id"]
    assert store.get_event("WH-TEST-1") == verified
    assert verified["verification"] == "verified"
    assert verified["readback_status"] == "WAITING_FOR_SELLER_RESPONSE"
    assert verified["readback_sha256"] == "b" * 64
    with pytest.raises(sqlite3.IntegrityError):
        _save(verification="unverified", readback_status=None, readback_sha256=None)
    assert store.get_event("WH-TEST-1") == verified
    assert len(store.list_events()) == 1


def test_concurrent_verified_writer_cannot_be_downgraded(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    _save(verification="unverified")
    original = store._connect
    raced = []
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchone(self):
            row = self.cursor.fetchone()
            if not raced:
                raced.append(True)
                def writer():
                    conn = original()
                    try:
                        conn.execute("UPDATE webhook_events SET verification='verified',body_sha256=? WHERE event_id='WH-TEST-1'", ("c"*64,))
                        conn.commit()
                    finally:
                        conn.close()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(writer).result(timeout=3)
            return row
    class Connection:
        def __init__(self):
            self.conn = original()
        def execute(self, sql, args=()):
            cursor = self.conn.execute(sql, args)
            return Cursor(cursor) if sql.startswith("SELECT id, verification") else cursor
        def __getattr__(self, key):
            return getattr(self.conn, key)
    monkeypatch.setattr(store, "_connect", Connection)
    with pytest.raises(sqlite3.IntegrityError):
        _save(verification="unverified")
    row = store.get_event("WH-TEST-1")
    assert raced and row["verification"] == "verified" and row["body_sha256"] == "c"*64
