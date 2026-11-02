"""draft_messages on a throwaway database (conftest isolates config.db_path() per test)."""
import json

import pytest

from copilot import config
from copilot.db import repo
from copilot.features.draft import store
from draft_fakes import DISPUTE_ID, EN_OK, ES_OK, sheet
from draft_fakes import _no_httpx_calls  # noqa: F401  (autouse: no httpx call in these tests)


def _save(**kw):
    args = dict(dispute_id=DISPUTE_ID, transaction_id="5S647034CC1979815", sheet=sheet(), en=EN_OK, es=ES_OK, model="qwen3.5:9b",
                seed=7, raw_sha256="a" * 64, validation_ok=True, problems=(), source="model")
    args.update(kw)
    return store.save_draft(**args)


def test_a_valid_draft_round_trips_with_the_sheet_the_model_saw():
    saved = _save()
    got = store.latest_draft(DISPUTE_ID)
    assert got == saved
    assert (got["en"], got["es"], got["model"], got["seed"], got["raw_sha256"], got["source"]) == (
        EN_OK, ES_OK, "qwen3.5:9b", 7, "a" * 64, "model")
    assert got["validation_ok"] is True and got["problems"] == [] and got["created_at"]
    assert got["sheet"]["tracking_number"] == "DEMO-FDX-000001" and got["sheet"]["literals"]
    assert got["transaction_id"] == "5S647034CC1979815"


def test_a_refused_draft_is_stored_with_its_problems():
    _save(validation_ok=False, problems=("English: the number 98765 is not in the facts",))
    got = store.latest_draft(DISPUTE_ID)
    assert got["validation_ok"] is False and got["problems"] == ["English: the number 98765 is not in the facts"]


def test_the_latest_draft_wins_and_other_disputes_are_separate():
    _save(en="first " + EN_OK)
    second = _save(en="second " + EN_OK)
    _save(dispute_id="PP-R-TEST-0000002")
    assert store.latest_draft(DISPUTE_ID)["id"] == second["id"] and store.latest_draft("PP-R-TEST-0000009") is None


def test_dispute_and_transaction_ids_may_be_null():
    _save(dispute_id=None, transaction_id=None)
    conn_rows = store._connect().execute("SELECT dispute_id, transaction_id FROM draft_messages").fetchall()
    assert [tuple(r) for r in conn_rows] == [(None, None)]


def test_one_audit_row_per_model_call_actor_system_with_hashes_and_flags_only():
    _save()
    audit = [a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "draft_message"]
    assert len(audit) == 1 and audit[0]["actor"] == "system"
    assert audit[0]["detail"] == {"model": "qwen3.5:9b", "seed": 7, "raw_sha256": "a" * 64, "validation_ok": True}
    assert EN_OK not in json.dumps(audit[0]) and "DEMO-FDX" not in json.dumps(audit[0])


def test_an_edited_draft_writes_no_audit_row_because_no_model_ran():
    _save(source="edited", raw_sha256=None)
    assert [a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "draft_message"] == []


def test_a_null_dispute_audits_under_the_dash():
    _save(dispute_id=None)
    assert [a["dispute_id"] for a in repo.list_audit(config.db_path(), "-") if a["action"] == "draft_message"] == ["-"]


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        _save(source="human")
