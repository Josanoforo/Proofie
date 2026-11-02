"""Missing PayPal credentials: the 503 keeps its status, and the page lists what is missing and what still opens."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hosted import authenticated_client, configure_hosted  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.web.routes_disputes import CredentialsMissing, get_client  # noqa: E402

ROW = "PayPal sandbox credentials not configured: dispute data unavailable"
FLAGS = ("PRESENTATION_MODE", "OFFLINE_MARKET_MODE", "BUYER_NOTE", "LOST_PACKAGE_PATHS")


def _missing():
    raise CredentialsMissing("x")


def _get(monkeypatch, path, flags_on):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    configure_hosted(monkeypatch)
    for name in FLAGS:
        monkeypatch.setenv("COPILOT_FEATURE_" + name, "true" if flags_on else "false")
    app = create_app()
    app.dependency_overrides[get_client] = _missing
    return authenticated_client(app, client=("127.0.0.1", 12345), raise_server_exceptions=False).get(path)


@pytest.mark.parametrize("path", ["/disputes", "/disputes/PP-D-12345"])
def test_missing_credentials_keep_503_and_show_the_row_and_credential_free_links(monkeypatch, path):
    r = _get(monkeypatch, path, True)
    assert r.status_code == 503 and ROW in r.text
    for href in ("/presentation-mode", "/offline-market-mode/status", "/disputes/PP-D-12345/buyer-note",
                 "/disputes/PP-D-12345/lost-package-paths"):
        assert f'href="{href}"' in r.text


def test_links_follow_their_flags(monkeypatch):
    r = _get(monkeypatch, "/disputes", False)
    assert r.status_code == 503 and ROW in r.text
    assert "/presentation-mode" not in r.text and "buyer-note" not in r.text
