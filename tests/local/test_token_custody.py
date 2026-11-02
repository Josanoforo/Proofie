"""The OAuth access token lives in memory only: not in the evidence log, not in the database, not in any file the app
writes, and not in a page it serves. The app is driven through its own routes and the real client, over a mock transport
that plays PayPal."""
import json
import secrets
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilot"))
from _hosted import authenticated_client, set_test_csrf  # noqa: E402
from copilot import config  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.db import repo  # noqa: E402
from copilot.web import routes_disputes  # noqa: E402
from test_routes_send import DID, WAITING, _approved_package_with_tracking  # noqa: E402

ISSUED = "synthetic-access-token-0123456789abcdef"
PROVIDE_EVIDENCE_REFUSED = {"name": "UNPROCESSABLE_ENTITY", "debug_id": "synthetic-debug-1",
                            "details": [{"issue": "DISPUTE_ALREADY_RESOLVED"}]}


class Paypal:
    """A mock transport that plays PayPal: it hands out ISSUED (or refuses to), and answers the reads and the one write."""

    def __init__(self, token_status=200):
        self.token_status = token_status
        self.authorizations, self.paths = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append((request.method, path))
        if path == "/v1/oauth2/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client",
                                                               "error_description": "Client Authentication failed"})
            return httpx.Response(200, json={"access_token": ISSUED, "expires_in": 3000, "token_type": "Bearer",
                                             "scope": "https://uri.paypal.com/services/disputes/read-seller"})
        self.authorizations.append(request.headers.get("authorization"))
        if request.method == "POST" and path.endswith("/provide-evidence"):
            return httpx.Response(422, json=PROVIDE_EVIDENCE_REFUSED)
        if path.startswith("/v1/customer/disputes/"):
            return httpx.Response(200, json=WAITING)
        if path.startswith("/v1/reporting/transactions"):
            return httpx.Response(200, json={"transaction_details": []})
        if path.startswith("/v1/shipping/trackers"):
            return httpx.Response(200, json={"trackers": []})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})


@pytest.fixture
def app_over(monkeypatch, tmp_path):
    """The real app and the real get_client; only the transport is a mock. Every place the app or the client can write
    is under tmp_path."""
    import client as pilot_client
    for name, value in (("PAYPAL_CLIENT_ID", "synthetic-client-id-0001"), ("PAYPAL_CLIENT_SECRET", "synthetic-secret-0001"),
                        ("PROOFIE_EVIDENCE_DIR", str(tmp_path / "evidence")), ("COPILOT_DATA_DIR", str(tmp_path / "data"))):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(pilot_client, "EVIDENCE", tmp_path / "evidence")
    real_get_client = routes_disputes.get_client

    def build(token_status=200):
        paypal = Paypal(token_status)

        def get_client():
            sandbox = real_get_client()
            sandbox._http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(paypal))
            return sandbox
        app = create_app()
        app.dependency_overrides[routes_disputes.get_client] = get_client
        web = authenticated_client(app, client=("127.0.0.1", 12345))
        token = secrets.token_urlsafe(32)
        set_test_csrf(web, token)
        return web, token, paypal
    return build


def _holders(tmp_path, *pages):
    """Every file under tmp_path, the database dumped as text, and the pages the app served, that hold ISSUED."""
    needle = ISSUED.encode()
    found = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file() and needle in p.read_bytes()]
    with sqlite3.connect(config.db_path()) as db:
        if ISSUED in "\n".join(db.iterdump()):
            found.append("database dump")
    return found + [f"page {i}" for i, page in enumerate(pages) if ISSUED in page]


def _evidence_lines(tmp_path):
    return [json.loads(line) for path in (tmp_path / "evidence").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_dispute_read_and_a_refused_send_leave_the_token_nowhere_on_disk_or_in_a_page(app_over, tmp_path):
    web, token, paypal = app_over()
    page = web.get(f"/disputes/{DID}")
    assert page.status_code == 200
    set_test_csrf(web, token)                              # the page load rotated the cookie
    package = _approved_package_with_tracking(web, config.db_path(), token)
    sent = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token})
    assert sent.status_code >= 400 or "not" in sent.text.lower()
    assert ("POST", f"/v1/customer/disputes/{DID}/provide-evidence") in paypal.paths    # the write really reached PayPal
    assert paypal.authorizations and set(paypal.authorizations) == {f"Bearer {ISSUED}"}    # and used the token every time
    entries = repo.list_audit(config.db_path(), DID)
    assert entries and any(e["action"].startswith("send") for e in entries)            # the copilot layer wrote its rows
    lines = _evidence_lines(tmp_path)
    assert any(line.get("op") == "oauth_token" and line.get("status") == 200 for line in lines)
    assert any(line.get("status") == 422 for line in lines)
    assert _holders(tmp_path, page.text, sent.text) == []


def test_a_refused_token_request_leaves_nothing_either(app_over, tmp_path):
    web, _token, paypal = app_over(token_status=401)
    page = web.get(f"/disputes/{DID}")
    assert ("POST", "/v1/oauth2/token") in paypal.paths and paypal.authorizations == []
    lines = _evidence_lines(tmp_path)
    assert any(line.get("op") == "oauth_token" and line.get("status") == 401 for line in lines)
    assert _holders(tmp_path, page.text) == []
