"""resolution_actions on a temp database (conftest isolates config.db_path() per test)."""
import json

import pytest

from copilot import config
from copilot.db import repo
from copilot.features.resolution import store

BODY = {"message": "hello"}


def _save(**kw):
    args = dict(dispute_id="PP-R-TST-0000001", action="message", request=BODY, request_sha="a" * 64,
                approval_sha="a" * 64, readback={"stage": "INQUIRY", "status": "X", "links": [], "outcome": None},
                settled=True, status="sent_confirmed")
    args.update(kw)
    return store.save_action(**args)


def test_round_trip_and_oldest_first_listing():
    first = _save()
    second = _save(action="offer", request={"offer_type": "REFUND"}, status="refused_422", settled=False,
                   readback=None, limitation_text="PayPal was still updating this dispute; wait a minute and try again")
    rows = store.list_actions("PP-R-TST-0000001")
    assert [r["id"] for r in rows] == [first["id"], second["id"]]
    assert rows[0]["settled"] == 1 and rows[1]["settled"] == 0
    assert json.loads(rows[0]["request_json"]) == BODY
    assert rows[1]["limitation_text"].startswith("PayPal was still updating")
    assert store.list_actions("PP-R-TST-0000009") == []


def test_one_audit_row_per_attempt_with_hashes_and_status_only():
    _save()
    audit = [a for a in repo.list_audit(config.db_path(), "PP-R-TST-0000001") if a["action"] == "resolution_message"]
    assert len(audit) == 1 and audit[0]["actor"] == "vendor"
    assert set(audit[0]["detail"]) == {"request_sha256", "approval_sha256", "status"}


@pytest.mark.parametrize("field,value", [("action", "escalate"), ("status", "done")])
def test_unknown_action_or_status_is_refused(field, value):
    with pytest.raises(ValueError):
        _save(**{field: value})


def test_the_pending_status_exists_and_a_row_can_be_updated_to_its_final_status():
    row = _save(status="sent_pending_settle", readback=None, settled=False)
    assert store.STATUS_PENDING == "sent_pending_settle" and "sent_pending_settle" in store.STATUSES
    store.update_action_status(row["id"], "sent_confirmed", {"stage": "INQUIRY", "status": "X", "links": [], "outcome": None}, True)
    got = store.list_actions("PP-R-TST-0000001")[0]
    assert got["status"] == "sent_confirmed" and got["settled"] == 1 and json.loads(got["readback_json"])["stage"] == "INQUIRY"
    store.update_action_status(row["id"], "sent_unconfirmed", None, False, "text")
    assert store.list_actions("PP-R-TST-0000001")[0]["limitation_text"] == "text"
    with pytest.raises(ValueError):
        store.update_action_status(row["id"], "done", None, False)
