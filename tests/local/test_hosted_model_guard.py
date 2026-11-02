"""With COPILOT_HOSTED_DEMO=true the model call sites raise one constant RuntimeError before any network
attempt; with the flag off the call is attempted exactly as before. The draft feature's site has its own guard test
beside that feature's other tests."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.core import auto_prep, evidence_model, vision_model
from copilot.db import repo

DID = "PP-R-TEST-0001"
DISPUTE_NORM = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
                "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "update_time": "2026-10-05T00:00:00Z",
                "create_time": "2026-10-01T00:00:00Z", "seller_transaction_ids": ["TXN1"],
                "evidence_requested_from_seller": ["PROOF_OF_FULFILLMENT"], "evidence_submitted_by_seller": []}


@pytest.fixture
def net(monkeypatch):
    """Records every httpx call; none reaches a socket. Also records pilot's pipeline entry points."""
    import step4_evidence_ai as ai
    calls = []

    def fake(*a, **kw):
        calls.append(("httpx", a[:1]))
        raise httpx.ConnectError("recorded, not sent")
    monkeypatch.setattr(httpx, "get", fake)
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setattr(ai, "preflight", lambda: calls.append(("preflight",)) or {})
    monkeypatch.setattr(ai, "run_with_docs", lambda *a, **kw: calls.append(("run_with_docs",)) or {"_output": None})
    monkeypatch.setenv("COPILOT_OLLAMA_URL", "http://203.0.113.9:11434")        # a configured, non-default Ollama
    return calls


def test_refuse_model_if_hosted_raises_the_one_constant_message_only_when_hosted(monkeypatch):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    config.refuse_model_if_hosted()
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    with pytest.raises(RuntimeError, match=config.HOSTED_MODEL_OFF_MESSAGE):
        config.refuse_model_if_hosted()


# ---- site 1: evidence_model.propose_package -----------------------------------------------------------------
def test_propose_package_flag_off_attempts_the_call(monkeypatch, net):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    evidence_model.propose_package({"dispute_id": DID}, {}, {})
    assert ("run_with_docs",) in net


def test_propose_package_flag_on_raises_before_any_attempt(monkeypatch, net):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    with pytest.raises(RuntimeError, match=config.HOSTED_MODEL_OFF_MESSAGE):
        evidence_model.propose_package({"dispute_id": DID}, {}, {})
    assert net == []


# ---- site 2: vision_model.propose_image_claims (the one place a vision call starts; it never raises) -----------
def test_vision_flag_off_attempts_preflight_and_the_call(monkeypatch, net):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    monkeypatch.setattr(vision_model, "_log", lambda e: None)
    assert vision_model.propose_image_claims(b"\x89PNG", "image/png", "1") == []     # the stubbed call fails: []
    assert ("preflight",) in net and any(c[0] == "httpx" for c in net)


def test_vision_flag_on_returns_nothing_without_any_attempt_and_logs_the_reason(monkeypatch, net):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    logged = []
    monkeypatch.setattr(vision_model, "_log", logged.append)
    assert vision_model.propose_image_claims(b"\x89PNG", "image/png", "1") == []
    assert net == [] and len(logged) == 1
    assert logged[0]["reason"] == f"hosted guard: {config.HOSTED_MODEL_OFF_MESSAGE}"     # the guard's own prefix, not "preflight:"


# ---- the manual path renders when the guard fires (auto_prep, the same handler the missing-Ollama case uses) -----
class _Client:
    def call(self, method, path, op, **kw):
        return {"transaction_details": []} if op == "context_transaction_search" else {"trackers": []}


def _prepare(db_path):
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, _Client(), db_path=db_path, version_hash="v1", claim_token="tok1")
    return repo.list_packages(db_path, DID)[0]


def test_auto_prep_flag_on_takes_the_manual_path_with_the_hosted_text_and_no_attempt(monkeypatch, net):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    pkg = _prepare(config.db_path())
    assert pkg["source"] == "auto_manual"
    assert auto_prep.HOSTED_AI_OFF_FLAG in pkg["review_flags"] and auto_prep.AI_UNAVAILABLE_FLAG not in pkg["review_flags"]
    assert "switched off in this demo" in auto_prep.HOSTED_AI_OFF_FLAG
    assert net == []


# ---- the flag text is chosen at one point -------------------------------------------------------
def test_flag_text_is_try_again_when_not_hosted_and_switched_off_when_hosted(monkeypatch):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    assert auto_prep.ai_unavailable_flag() is auto_prep.AI_UNAVAILABLE_FLAG and "try again" in auto_prep.ai_unavailable_flag()
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    assert auto_prep.ai_unavailable_flag() == "The AI assistant is switched off in this demo. Type the shipment facts yourself."


# ---- the package route: hosted, with a readable document, the review page shows the hosted text and nothing was attempted
def test_package_route_hosted_shows_the_ai_off_text_and_attempts_nothing(monkeypatch, net):
    import json
    import secrets
    from fastapi.testclient import TestClient
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client
    from copilot.web.security import CSRF_COOKIE_NAME
    from _hosted import authenticated_client, configure_hosted

    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    configure_hosted(monkeypatch)
    rec = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
    body = json.loads((rec / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]

    class Fake:
        def call(self, method, path, op, **kw):
            return body if op == "show_dispute" else ({"transaction_details": []} if op == "context_transaction_search"
                                                      else {"trackers": []})
    did = "PP-R-DEMO-0000001"
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    c = authenticated_client(app)
    token = secrets.token_urlsafe(32)
    c.cookies.set(CSRF_COOKIE_NAME, token)
    repo.insert_document(config.db_path(), did, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    r = c.post(f"/disputes/{did}/packages", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    pkg = repo.list_packages(config.db_path(), did)[0]
    assert pkg["source"] == "human_edit" and pkg["review_flags"] == [auto_prep.HOSTED_AI_OFF_FLAG]
    assert "switched off in this demo" in c.get(f"/disputes/{did}/packages/{pkg['id']}").text
    assert net == []
