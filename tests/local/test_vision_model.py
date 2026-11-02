"""Offline tests for copilot/core/vision_model.py (vision
suggestions for an image document). The real model/GPU are never touched here except in the one test
marked live_model - ai.preflight()/httpx.post are monkeypatched everywhere else.
"""
from _hosted import authenticated_client, reset_csrf
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core import vision_model as vm
from copilot.db import repo

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
DID = "PP-D-TEST-0001"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _obs(text, field=None, value=None):
    return {"text": text, "field": field, "value": value}


def _ok_response(observations):
    import json
    return _FakeResponse({"message": {"content": json.dumps({"observations": observations})}})


@pytest.mark.local_spec
def test_unknown_content_type_returns_nothing(monkeypatch):
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    assert vm.propose_image_claims(b"x", "image/bmp", "1") == []


@pytest.mark.local_spec
def test_preflight_failure_returns_nothing_not_a_crash(monkeypatch):
    monkeypatch.setattr(vm.ai, "preflight", lambda: (_ for _ in ()).throw(RuntimeError("GPU busy")))
    assert vm.propose_image_claims(PNG_BYTES, "image/png", "1") == []


@pytest.mark.local_spec
def test_a_transport_error_returns_nothing_not_a_crash(monkeypatch):
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("boom")))
    assert vm.propose_image_claims(PNG_BYTES, "image/png", "1") == []


@pytest.mark.local_spec
def test_a_malformed_response_returns_nothing_not_a_crash(monkeypatch):
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post", lambda *a, **kw: _FakeResponse({"message": {"content": "not json"}}))
    assert vm.propose_image_claims(PNG_BYTES, "image/png", "1") == []


@pytest.mark.local_spec
def test_clean_observations_become_model_interpretation_claims(monkeypatch):
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post",
                        lambda *a, **kw: _ok_response([_obs("Tracking number DEMO123 is visible."),
                                                       _obs("Carrier appears to be FedEx.")]))
    claims = vm.propose_image_claims(PNG_BYTES, "image/png", "42")
    assert len(claims) == 2
    assert all(c["basis"] == "model_interpretation" for c in claims)
    assert all(c["source_ids"] == ["doc_42"] for c in claims)
    assert claims[0]["claim_index"] == 0 and claims[1]["claim_index"] == 1


@pytest.mark.local_spec
@pytest.mark.parametrize("observation", [
    "The package cost $45.00.", "Total: 450.00 MXN", "Amount due is 25 USD",
    "Precio: 100 pesos", "El importe es de 500",
])
def test_any_claim_mentioning_money_is_dropped_even_if_the_model_states_it(monkeypatch, observation):
    """Defense in depth beyond the prompt: numbers/amounts are computed by code from PayPal's own data,
    never by a model - this must hold even if the model ignores its own system instruction."""
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post",
                        lambda *a, **kw: _ok_response([_obs(observation), _obs("Carrier appears to be FedEx.")]))
    claims = vm.propose_image_claims(PNG_BYTES, "image/png", "1")
    assert len(claims) == 1 and claims[0]["text"] == "Carrier appears to be FedEx."


@pytest.mark.local_spec
def test_a_money_mentioning_suggested_value_is_also_dropped_even_if_the_text_looks_clean(monkeypatch):
    """The text sentence might read innocently while the structured value itself leaks an amount - both
    are checked independently."""
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post",
                        lambda *a, **kw: _ok_response([
                            _obs("A value is visible.", field="delivery_status", value="$45.00")]))
    assert vm.propose_image_claims(PNG_BYTES, "image/png", "1") == []


@pytest.mark.local_spec
def test_an_observation_mapping_to_a_known_field_carries_the_suggestion_through(monkeypatch):
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post",
                        lambda *a, **kw: _ok_response([
                            _obs("The status reads Entregado.", field="delivery_status", value="Entregado")]))
    claims = vm.propose_image_claims(PNG_BYTES, "image/png", "1")
    assert claims[0]["suggested_field"] == "delivery_status"
    assert claims[0]["suggested_value"] == "Entregado"


@pytest.mark.local_spec
def test_an_observation_with_an_unrecognised_field_name_drops_the_suggestion_not_the_claim(monkeypatch):
    """Defense in depth: a field name outside checklist.py's own vocabulary is never trusted, even though
    the schema is meant to constrain the model to it - the free-text observation still survives."""
    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post",
                        lambda *a, **kw: _ok_response([
                            _obs("Something about a service tier is visible.", field="service_tier",
                                value="gold")]))
    claims = vm.propose_image_claims(PNG_BYTES, "image/png", "1")
    assert len(claims) == 1
    assert claims[0]["suggested_field"] is None and claims[0]["suggested_value"] is None


@pytest.mark.local_spec
def test_the_request_body_never_sends_the_model_an_amount_free_system_prompt_without_images_key_missing(monkeypatch):
    """Confirms the actual request shape: model/keep_alive from config/pilot, an images field with the
    base64 payload, and the money-forbidding instruction present in the system message."""
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"], captured["json"] = url, json
        return _ok_response([])

    monkeypatch.setattr(vm.ai, "preflight", lambda: {})
    monkeypatch.setattr(vm.httpx, "post", fake_post)
    vm.propose_image_claims(PNG_BYTES, "image/png", "1")
    assert captured["url"].endswith("/api/chat")
    body = captured["json"]
    assert body["model"] == vm.config.model_name()
    assert "NEVER" in body["messages"][0]["content"] and "money" in body["messages"][0]["content"].lower()
    assert "images" in body["messages"][1]


# ---- claims_for_dispute_images: shared by routes_package.py and auto_prep.py --------------------------
def test_claims_for_dispute_images_skips_non_image_and_excluded_documents(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha_pdf", "text", True)
    repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha_png", None, False)
    excluded_id = repo.insert_document(db_path, DID, "excluded.png", "image/png", 10, "sha_excl", None, False)
    repo.set_document_excluded(db_path, excluded_id, True)
    from copilot.core.raw_storage import write_raw
    write_raw(tmp_path / "uploads", "sha_png", PNG_BYTES)

    called = []
    monkeypatch.setattr(vm, "propose_image_claims", lambda *a, **kw: called.append(a) or [])
    monkeypatch.setattr(vm.config, "uploads_dir", lambda: tmp_path / "uploads")
    vm.claims_for_dispute_images(db_path, DID)
    assert len(called) == 1   # only the one non-excluded image


@pytest.mark.local_spec
@pytest.mark.live_model
def test_a_real_vision_call_against_the_demo_illegible_image_never_mentions_money():
    """End to end against the REAL qwen3.5:9b (confirmed 2026-10-05 via /api/show: declares the
    'vision' capability), GPU/model preflight included - no PayPal call anywhere in this test, so it
    needs no PayPal-write authorization. Marked live_model so conftest.py's network block does not
    intercept the real, deliberate local Ollama call."""
    demo_png = (Path(__file__).resolve().parents[2] / "demo" / "documents" / "05_illegible_scan.png").read_bytes()
    claims = vm.propose_image_claims(demo_png, "image/png", "1")
    for c in claims:
        assert not vm._REJECT_IF_MENTIONS_MONEY.search(c["text"]), c["text"]
        assert c["basis"] == "model_interpretation"


def test_claims_for_dispute_images_sets_the_given_evidence_index(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha_png", None, False)
    from copilot.core.raw_storage import write_raw
    write_raw(tmp_path / "uploads", "sha_png", PNG_BYTES)

    monkeypatch.setattr(vm, "propose_image_claims",
                        lambda *a, **kw: [{"evidence_index": 0, "claim_index": 0, "text": "x",
                                          "source_ids": ["doc_1"], "basis": "model_interpretation"}])
    monkeypatch.setattr(vm.config, "uploads_dir", lambda: tmp_path / "uploads")
    claims = vm.claims_for_dispute_images(db_path, DID, evidence_index=3)
    assert claims[0]["evidence_index"] == 3


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


@pytest.mark.hosted_spec
@pytest.mark.parametrize("content_type", ["image/png", "image/jpeg", "image/gif"])
def test_hosted_vision_refuses_all_supported_inputs_before_preflight_or_transport(monkeypatch, content_type):
    calls = []
    monkeypatch.setattr(vm.ai, "preflight", lambda: calls.append("preflight"))
    monkeypatch.setattr(vm.httpx, "post", lambda *a, **kw: calls.append("transport"))
    assert vm.propose_image_claims(PNG_BYTES, content_type, "1") == []
    assert calls == []
