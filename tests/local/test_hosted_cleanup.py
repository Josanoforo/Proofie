"""Screenshot storage and hosted credentials banners, without sockets or a browser launch."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.core.disputes import PayPalError
from copilot.web.routes_disputes import CredentialsMissing, get_client
from _hosted import authenticated_client, configure_hosted


def test_ui_captures_follow_data_setting_and_keep_the_default(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[2] / "demo" / "ui_check.py"
    def output():
        spec = importlib.util.spec_from_file_location("ui_check_probe", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.OUT
    monkeypatch.delenv("COPILOT_DATA_DIR", raising=False)
    assert output() == config.DATA_DIR / "ui-captures" / "d2"
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "host-data"))
    assert output() == (tmp_path / "host-data" / "ui-captures" / "d2").resolve()


def test_hosted_rejected_and_missing_credentials_are_info(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    configure_hosted(monkeypatch)
    app = create_app()
    def missing():
        raise CredentialsMissing("synthetic")
    app.dependency_overrides[get_client] = missing
    web = authenticated_client(app)
    response = web.get("/disputes")
    assert response.status_code == 503 and 'class="banner banner-error"' in response.text
    def rejected():
        raise PayPalError(401, "synthetic", "synthetic", {})
    app.dependency_overrides[get_client] = rejected
    response = web.get("/disputes")
    assert response.status_code == 502 and 'class="banner banner-error"' in response.text
