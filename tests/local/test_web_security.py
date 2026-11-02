"""Offline tests for copilot/web/security.py."""
import sys
from pathlib import Path
import pytest

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.web.security import CSRF_COOKIE_NAME, LocalhostOnlyMiddleware, issue_csrf_token, verify_csrf
from copilot import config
from copilot.web.security import HostedAccessMiddleware, access_cookie_value

pytestmark = pytest.mark.usefixtures("web_mode")


def _app():
    app = FastAPI()
    app.add_middleware(HostedAccessMiddleware if config.hosted_demo() else LocalhostOnlyMiddleware)

    @app.get("/ping")
    def ping():
        return PlainTextResponse("pong")

    @app.get("/form")
    def form():
        resp = PlainTextResponse("ok")
        issue_csrf_token(resp)
        return resp

    @app.post("/submit")
    async def submit(request: Request):
        body = await request.form()
        if not verify_csrf(request, body.get("csrf_token")):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse("accepted")

    return app


# ---- LocalhostOnlyMiddleware ---------------------------------------------------------------------
def test_request_from_loopback_is_allowed():
    client = _local_client()
    assert client.get("/ping").status_code == 200


def test_localhost_guard_is_outermost_with_active_approval(monkeypatch):
    from copilot.app import create_app
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    # Synthetic registration keeps this core ordering proof runnable in the public tree.
    # The independent probes exercise the real private active-approval guard.
    import sys
    from types import ModuleType
    from starlette.middleware.base import BaseHTTPMiddleware
    from fastapi.responses import HTMLResponse
    class DemoApprovalGuard(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.method == "POST" and request.url.path.endswith("/approve"):
                return HTMLResponse("DEMO passive approval guard", status_code=409)
            return await call_next(request)
    for name in ("copilot.features", "copilot.features.active_approval"):
        if name not in sys.modules:
            package = ModuleType(name)
            package.__path__ = []
            monkeypatch.setitem(sys.modules, name, package)
    module = ModuleType("copilot.features.active_approval.routes")
    module.register = lambda app: app.add_middleware(DemoApprovalGuard)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    app = create_app()
    assert app.user_middleware[0].cls is LocalhostOnlyMiddleware
    target = "/disputes/PP-R-DEMO-0000001/packages/1/approve"
    with TestClient(app, client=("203.0.113.5", 1)) as remote:
        assert remote.post(target).status_code == 403
    with TestClient(app, client=("127.0.0.1", 1)) as local:
        assert local.post(target).status_code == 409


def test_request_from_a_non_loopback_address_is_rejected():
    client = TestClient(_app(), base_url="https://localhost", client=("203.0.113.5", 12345))
    r = client.get("/ping", follow_redirects=False)
    if config.hosted_demo():
        assert r.status_code == 303 and r.headers["location"].startswith("/access")
        client.cookies.set("proofie_access", access_cookie_value(), domain="localhost.local", path="/")
        assert client.get("/ping").status_code == 200
    else:
        assert r.status_code == 403


# ---- CSRF: double-submit cookie -------------------------------------------------------------------
def _local_client():
    client = TestClient(_app(), base_url="https://localhost" if config.hosted_demo() else "http://localhost",
                        client=("127.0.0.1", 12345))
    if config.hosted_demo():
        client.cookies.set("proofie_access", access_cookie_value(), domain="localhost.local", path="/")
    return client


def test_post_without_any_csrf_token_is_rejected():
    client = _local_client()
    r = client.post("/submit", data={})
    assert r.status_code == 403


def test_post_with_matching_cookie_and_field_is_accepted():
    client = _local_client()
    client.get("/form")   # sets the cookie
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    r = client.post("/submit", data={"csrf_token": token})
    assert r.status_code == 200 and r.text == "accepted"


def test_post_with_wrong_field_value_is_rejected_even_with_a_real_cookie():
    client = _local_client()
    client.get("/form")
    r = client.post("/submit", data={"csrf_token": "not-the-real-token"})
    assert r.status_code == 403


def test_post_with_a_token_but_no_cookie_is_rejected():
    client = _local_client()
    # a token value with no cookie ever set (e.g. forged from another session) must not pass
    r = client.post("/submit", data={"csrf_token": "some-token-nobody-issued"})
    assert r.status_code == 403


def test_verify_csrf_uses_constant_time_comparison_not_equality(monkeypatch):
    """Sanity that we call secrets.compare_digest rather than ==, so this file doesn't silently regress
    to a plain string comparison later."""
    import copilot.web.security as security_module
    calls = []
    real = security_module.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(security_module.secrets, "compare_digest", spy)
    client = _local_client()
    client.get("/form")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    client.post("/submit", data={"csrf_token": token})
    assert calls.count((token, token)) == 1
