"""bilingual.model: the local model drafts the Spanish version of ONE English statement; code checks it; the seller reviews.
Tests always pass a fake call: no model is reached here."""
import json

import pytest

from copilot import config
from copilot.db import repo
from copilot.features.bilingual import model, service, store

from .conftest import DISPUTE_ID, EN_1, EN_2, ES_1, ES_2, FakeModel, claim_ids, make_package


def test_the_prompt_holds_the_english_statement_only():
    call = FakeModel({EN_1: ES_1})
    result = model.call_spanish(EN_1, call=call)
    assert (result.es, result.error) == (ES_1, None)
    messages, schema, seed = call.calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert EN_1 in messages[1]["content"] and EN_2 not in messages[1]["content"]
    assert schema == model.SCHEMA and schema["required"] == ["es"] and schema["additionalProperties"] is False
    assert result.raw_sha256 and len(result.raw_sha256) == 64 and result.model == config.model_name()


@pytest.mark.parametrize("raw", ["not json", json.dumps(["es"]), json.dumps({"es": 3}), json.dumps({"es": "x", "en": "y"})])
def test_an_unreadable_answer_is_an_error_not_text(raw):
    result = model.call_spanish(EN_1, call=FakeModel(raw=raw))
    assert result.es is None and result.error == model.MALFORMED


@pytest.mark.parametrize("exc", [RuntimeError("GPU busy"), OSError("refused")])
def test_an_unavailable_model_is_an_error(exc):
    result = model.call_spanish(EN_1, call=FakeModel(raises=exc))
    assert result.es is None and result.error == model.UNAVAILABLE


def test_the_hosted_demo_refuses_before_any_network_attempt(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    with pytest.raises(RuntimeError, match=config.HOSTED_MODEL_OFF_MESSAGE):
        model.default_call([], model.SCHEMA, 7)
    assert model.call_spanish(EN_1).error == model.UNAVAILABLE


def _ai_rows():
    import sqlite3
    with sqlite3.connect(str(config.db_path())) as conn:
        try:
            return conn.execute(f"SELECT claim_id, es, source, model FROM {store.TABLE} ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return []


def test_draft_with_model_fills_every_missing_row_as_ai(mx_on):
    db = config.db_path()
    package_id = make_package()
    first, second = claim_ids(package_id)
    summary = service.draft_with_model(db, DISPUTE_ID, package_id, call=FakeModel({EN_1: ES_1, EN_2: ES_2}))
    assert (summary["made"], summary["refused"], summary["error"]) == (2, [], None)
    view = service.view(db, package_id)
    assert [(r["es"], r["es_state"]) for r in view["rows"]] == [(ES_1, "ai"), (ES_2, "ai")]
    assert view["complete"] is True
    assert [r[0] for r in _ai_rows()] == [first, second] and {r[3] for r in _ai_rows()} == {config.model_name()}


def test_a_version_that_changes_a_fact_is_refused_and_not_stored(mx_on):
    db = config.db_path()
    package_id = make_package()
    summary = service.draft_with_model(db, DISPUTE_ID, package_id,
                                       call=FakeModel({EN_1: ES_1.replace("7 oct", "9 oct"), EN_2: ES_2}))
    assert summary["made"] == 1 and summary["refused"] == [1] and summary["error"] is None
    assert [r["es_state"] for r in service.view(db, package_id)["rows"]] == ["missing", "ai"]


def test_the_seller_s_own_spanish_is_never_replaced(mx_on):
    db = config.db_path()
    package_id = make_package()
    first = claim_ids(package_id)[0]
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, "Mi propia versión, 7 oct 2026, 1Z999AA10123456784.")
    call = FakeModel({EN_1: ES_1, EN_2: ES_2})
    summary = service.draft_with_model(db, DISPUTE_ID, package_id, call=call)
    assert summary["made"] == 1 and len(call.calls) == 1 and EN_2 in call.calls[0][0][1]["content"]
    assert [r["es_state"] for r in service.view(db, package_id)["rows"]] == ["seller", "ai"]


def test_a_seller_edit_of_an_ai_version_makes_it_the_seller_s(mx_on):
    db = config.db_path()
    package_id = make_package()
    service.draft_with_model(db, DISPUTE_ID, package_id, call=FakeModel({EN_1: ES_1, EN_2: ES_2}))
    service.save_seller_spanish(db, DISPUTE_ID, package_id, claim_ids(package_id)[0], ES_1)
    assert [r["es_state"] for r in service.view(db, package_id)["rows"]] == ["seller", "ai"]


def test_an_unavailable_model_stores_nothing_and_stops_after_the_first_call(mx_on):
    db = config.db_path()
    package_id = make_package()
    call = FakeModel(raises=RuntimeError("busy"))
    summary = service.draft_with_model(db, DISPUTE_ID, package_id, call=call)
    assert summary["made"] == 0 and summary["error"] == model.UNAVAILABLE and len(call.calls) == 1
    assert _ai_rows() == []


def test_nothing_missing_means_no_model_call(mx_on):
    db = config.db_path()
    package_id = make_package()
    for cid, es in zip(claim_ids(package_id), (ES_1, ES_2)):
        service.save_seller_spanish(db, DISPUTE_ID, package_id, cid, es)
    call = FakeModel({EN_1: ES_1})
    assert service.draft_with_model(db, DISPUTE_ID, package_id, call=call)["made"] == 0 and call.calls == []


def test_every_model_call_is_audited_without_text(mx_on):
    db = config.db_path()
    package_id = make_package()
    service.draft_with_model(db, DISPUTE_ID, package_id, call=FakeModel({EN_1: ES_1, EN_2: "Hola 99"}))
    rows = [a for a in repo.list_audit(db, DISPUTE_ID) if a["action"] == "draft_spanish_statement"]
    assert len(rows) == 2 and {a["actor"] for a in rows} == {"system"}
    text = json.dumps(rows, ensure_ascii=False)
    assert "entregó" not in text and "Hola" not in text and EN_1 not in text
    assert '"validation_ok": true' in text and '"validation_ok": false' in text


def test_the_model_path_needs_the_feature_on(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_BILINGUAL", "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    with pytest.raises(PermissionError):
        service.draft_with_model(config.db_path(), DISPUTE_ID, make_package(), call=FakeModel())
