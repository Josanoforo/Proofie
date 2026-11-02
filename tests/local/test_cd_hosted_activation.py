"""C-D hosted contract: sixteen active flags, protected writes, appeal stays refused."""
import pytest
from fastapi.testclient import TestClient
from copilot import config
from copilot.app import create_app

CD_ON = {"account_cliff", "active_approval", "carrier_required", "claims",
         "deadlines", "delivery_facts", "draft", "events", "fulfilment",
         "fulfilment_register", "ledger", "outcome", "package_print",
         "resolution", "resolution_writes", "triage"}
CD_OFF = {"outcome_appeal", "webhooks", "webhooks_verify", "conformance"}

@pytest.fixture
def cd_hosted(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "test-hosted-access-code")
    for name in CD_ON | CD_OFF:
        monkeypatch.setenv("COPILOT_FEATURE_" + name.upper(),
                          "true" if name in CD_ON else "false")

def test_cd_hosted_starts_and_keeps_unauthenticated_write_refused(cd_hosted):
    assert all(config.feature_enabled(name) for name in CD_ON)
    assert all(not config.feature_enabled(name) for name in CD_OFF)
    app = create_app()
    with TestClient(app, base_url="https://localhost") as web:
        assert web.get("/healthz").status_code == 200
        assert web.post("/disputes/PP-R-DEMO-0000001/offer", data={}).status_code == 401

def test_cd_hosted_starts_with_outcome_appeal_on(cd_hosted, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "true")
    assert create_app() is not None
