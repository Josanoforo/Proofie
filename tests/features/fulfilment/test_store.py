"""The fulfilment_files table on a temp database (conftest isolates config.db_path() per test) and the lookup."""
import json
import sqlite3

import pytest

from copilot import config
from copilot.db import repo
from copilot.features import fulfilment
from copilot.features.fulfilment import lookup, store
from copilot.features.fulfilment import tracker_request as tr

CAP = "5S647034CC1979815"
BODY = tr.build_tracker_body(CAP, "TRK1TEST20261006", "FEDEX", "FedEx", status="DELIVERED")
SHA = tr.request_sha256(BODY)
READBACK = {"tracking_number": "TRK1TEST20261006", "status": "DELIVERED", "carrier": "FEDEX",
            "shipment_date": "2026-10-06", "last_updated_time": 1, "link_rels": ["edit", "self"]}
CHECKS = {"checks": [{"id": "tracking_number", "state": "verified"}], "protection_eligibility": "01"}


def _save(**kw):
    args = dict(capture_id=CAP, request=BODY, request_sha=SHA, approval_sha=SHA, tracker_readback=READBACK,
                checks=CHECKS, status="registered")
    args.update(kw)
    return store.save_file(**args)


def test_round_trip_and_the_recomputed_file_sha256_equals_the_stored_one():
    saved = _save()
    got = store.get_file(CAP)
    assert got == saved
    assert got["file_sha256"] == store.recompute_file_sha256(got)
    assert json.loads(got["request_json"]) == BODY and json.loads(got["checks_json"]) == CHECKS
    assert (got["request_sha256"], got["approval_sha256"], got["status"]) == (SHA, SHA, "registered")


def test_a_tampered_row_no_longer_matches_its_hash():
    _save()
    conn = sqlite3.connect(str(config.db_path()))
    conn.execute("UPDATE fulfilment_files SET checks_json = ?", (json.dumps({"checks": []}),))
    conn.commit()
    conn.close()
    got = store.get_file(CAP)
    assert got["file_sha256"] != store.recompute_file_sha256(got)


def test_the_hash_covers_request_readback_and_checks():
    base = store.file_sha256(BODY, READBACK, CHECKS)
    assert store.file_sha256(BODY, READBACK, CHECKS) == base
    assert store.file_sha256(BODY, {**READBACK, "status": "SHIPPED"}, CHECKS) != base
    assert store.file_sha256(BODY, READBACK, {"checks": []}) != base
    assert store.file_sha256(tr.build_tracker_body(CAP, "N2", "UPS", None), READBACK, CHECKS) != base


def test_get_returns_none_for_an_unknown_capture():
    assert store.get_file("ZZZZZZZZZZZZZZZZZ") is None


def test_get_returns_the_latest_and_list_has_one_row_per_capture_newest_first():
    _save(status="registered_unconfirmed", tracker_readback=None)
    _save(status="registered")
    other = "1HM74165DE778954K"
    _save(capture_id=other, request=tr.build_tracker_body(other, "N9", "UPS", None), status="failed",
          tracker_readback=None, limitation_text="PayPal could not register the tracking (error 500).")
    assert store.get_file(CAP)["status"] == "registered"
    rows = store.list_files()
    assert [r["capture_id"] for r in rows] == [other, CAP]
    assert rows[0]["limitation_text"].startswith("PayPal could not")


def test_an_unknown_status_is_refused():
    with pytest.raises(ValueError):
        _save(status="done")


def test_one_audit_row_per_registration_with_hashes_only():
    _save()
    rows = repo.list_audit(config.db_path(), CAP)
    mine = [r for r in rows if r["action"] == "fulfilment_registered"]
    assert len(mine) == 1 and mine[0]["actor"] == "vendor"
    assert set(mine[0]["detail"]) == {"request_sha256", "approval_sha256", "file_sha256", "status"}
    assert "TRK1TEST20261006" not in json.dumps(mine[0]["detail"])


def test_lookup_finds_the_file_and_misses_an_unknown_transaction():
    saved = _save()
    found = lookup.file_for_transaction(CAP)
    assert found == {"capture_id": CAP, "created_at": saved["created_at"], "status": "registered",
                     "file_sha256": saved["file_sha256"], "request_sha256": SHA,
                     "tracking_number": "TRK1TEST20261006"}
    assert fulfilment.file_for_transaction(CAP) == found
    assert lookup.file_for_transaction("ZZZZZZZZZZZZZZZZZ") is None
