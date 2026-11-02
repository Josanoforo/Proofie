import pytest
from copilot import config
from copilot.features.webhooks import service, store
from webhooks_fakes import FakeClient, HEADERS, raw

@pytest.mark.parametrize("value", [None, "false", "1", "yes", ""])
def test_verification_off_stores_unverified_without_any_client_call(monkeypatch, value):
    name = "COPILOT_FEATURE_WEBHOOKS_VERIFY"
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    fake = FakeClient()
    handled = service.handle_event(fake, config.db_path(), HEADERS, raw(), None)
    assert (handled.http_status, handled.verification, handled.readback_status) == (202, "skipped", None)
    assert fake.calls == []
    row = store.get_event("WH-TEST-1")
    assert row["verification"] == "unverified"
    assert row["readback_status"] is None and row["readback_sha256"] is None

def test_verification_on_preserves_verify_then_readback(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", "true")
    fake = FakeClient()
    result = service.handle_event(fake, config.db_path(), HEADERS, raw(), "WH-REGISTERED-1")
    assert result.http_status == 200 and result.verification == "verified"
    assert [call[0] for call in fake.calls] == ["POST", "GET"]

@pytest.mark.parametrize("hosted", [False, True])
def test_receiver_off_never_constructs_client(monkeypatch, hosted):
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client
    from _hosted import authenticated_client
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS", "true")
    monkeypatch.delenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", raising=False)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true" if hosted else "false")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-hosted-test-code")
    app = create_app()
    calls = []
    def forbidden_client():
        calls.append("constructed")
        raise AssertionError("client must not be constructed")
    app.dependency_overrides[get_client] = forbidden_client
    web = authenticated_client(app)
    response = web.post("/webhooks/paypal", content=raw(), headers=HEADERS)
    assert response.status_code == (401 if hosted else 202)
    assert calls == []
    if hosted:
        assert store.list_events() == []
    else:
        assert "unverified" in web.get("/webhooks/events").text


def test_unverified_retry_can_verify_without_overwriting_verified_history(monkeypatch):
    fake = FakeClient()
    monkeypatch.delenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", raising=False)
    skipped = service.handle_event(fake, config.db_path(), HEADERS, raw(), None)
    assert skipped.verification == "skipped" and fake.calls == []
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", "true")
    assert service.handle_event(fake, config.db_path(), HEADERS, raw(), "WH-REGISTERED-1").verification == "verified"
    row = store.get_event("WH-TEST-1")
    count = len(fake.calls)
    monkeypatch.delenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", raising=False)
    assert service.handle_event(fake, config.db_path(), HEADERS, raw(), None).verification == "skipped"
    assert len(fake.calls) == count and store.get_event("WH-TEST-1") == row


def test_hosted_both_flags_on_verifies_with_fake_only(monkeypatch):
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client
    from _hosted import authenticated_client
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-hosted-test-code")
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS", "true")
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", "true")
    monkeypatch.setenv("COPILOT_WEBHOOK_ID", "WH-REGISTERED-1")
    fake = FakeClient()
    app = create_app(); app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app)
    response = web.post("/webhooks/paypal", content=raw(), headers=HEADERS)
    assert response.status_code == 200 and response.json()["verification"] == "verified"
    assert [c[0] for c in fake.calls] == ["POST", "GET"]


def test_hosted_feature_off_keeps_unauthenticated_receiver_refusal(monkeypatch):
    from copilot.app import create_app
    from fastapi.testclient import TestClient
    from copilot.web.routes_disputes import get_client
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-hosted-test-code")
    monkeypatch.delenv("COPILOT_FEATURE_WEBHOOKS", raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", "true")
    calls = []
    app = create_app(); app.dependency_overrides[get_client] = lambda: calls.append("constructed")
    web = TestClient(app)
    assert web.post("/webhooks/paypal", content=raw(), headers=HEADERS).status_code == 401
    assert calls == []
