"""Hosted access, bounded uploads and the unchanged send flow through offline fakes."""
import asyncio
import secrets
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _hosted import TEST_ACCESS_CODE, authenticated_client, hosted_web  # noqa: F401
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web import security

DID = "PP-R-DEMO-0000001"
UPLOAD = f"/disputes/{DID}/documents"


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    for name in ("fulfilment_register", "resolution_writes", "outcome_appeal", "webhooks"):
        monkeypatch.delenv("COPILOT_FEATURE_" + name.upper(), raising=False)
    app = create_app()
    calls = []
    @app.get("/probe")
    def probe():
        return {"ok": True}
    @app.post("/probe")
    def write():
        calls.append("handler")
        return {"ok": True}
    return app, TestClient(app, base_url="https://localhost", client=("10.0.0.1", 1)), calls


def access_post(web, code=TEST_ACCESS_CODE, next_path="/probe"):
    web.get("/access")
    return web.post("/access", data={"code": code, "csrf_token": web.cookies.get("csrf_token"), "next": next_path},
                    follow_redirects=False)


def test_dr1b_wrong_code_has_visible_red_message_without_changing_limiter(gate):
    app, web, calls = gate
    response = access_post(web, code="synthetic-wrong")
    assert response.status_code == 401
    assert "The access code is not correct. Check it and enter it again." in response.text
    assert "There is a problem" in response.text and 'class="error-summary"' in response.text
    assert len(app.state.access_limiter.failures) == 1 and calls == []


def test_dr1b_rate_limit_and_expired_form_have_layout_and_field_link(gate):
    app, web, calls = gate
    for _ in range(5): app.state.access_limiter.record_failure("10.0.0.1")
    response = access_post(web)
    assert response.status_code == 429 and "Too many attempts" in response.text
    assert 'href="#field-code"' in response.text and 'class="error-summary"' in response.text
    response = web.post("/access",data={"csrf_token":"invalid","code":TEST_ACCESS_CODE})
    assert response.status_code == 403 and "<main id=\"main\" tabindex=\"-1\">" in response.text
    assert "Your session expired" in response.text and calls == []


def test_no_cookie_get_and_head_redirect_and_post_never_calls_handler(gate):
    _, web, calls = gate
    for method in ("GET", "HEAD"):
        response = web.request(method, "/probe", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/access?next=%2Fprobe"
    assert web.post("/probe").status_code == 401 and calls == []


def test_hosted_access_is_outermost_with_active_approval(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    for name in ("fulfilment_register", "resolution_writes", "outcome_appeal"):
        monkeypatch.delenv("COPILOT_FEATURE_" + name.upper(), raising=False)
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
    assert app.user_middleware[0].cls is security.HostedAccessMiddleware
    target = f"/disputes/{DID}/packages/1/approve"
    with TestClient(app, base_url="https://localhost", client=("10.0.0.1", 1)) as web:
        assert web.post(target).status_code == 401
        assert access_post(web).status_code == 303
        assert web.post(target).status_code == 409


def test_right_code_secure_cookie_remote_peer_and_hosted_csrf(gate):
    _, web, _ = gate
    response = access_post(web)
    header = response.headers["set-cookie"]
    assert response.status_code == 303 and response.headers["location"] == "/probe"
    assert "proofie_access=" in header and "HttpOnly" in header and "Secure" in header
    assert "SameSite=lax" in header and "Path=/" in header and "Max-Age=604800" in header
    assert web.get("/probe").status_code == 200
    assert "Secure" in web.get("/access").headers["set-cookie"]


def test_access_requires_csrf_and_the_cookie_is_not_issued(gate):
    _, web, _ = gate
    for token in ("", "forged"):
        response = web.post("/access", data={"code": TEST_ACCESS_CODE, "csrf_token": token})
        assert response.status_code == 403 and "proofie_access" not in web.cookies


def test_wrong_code_has_fixed_async_delay_and_no_hint(gate, monkeypatch):
    _, web, _ = gate
    delays = []
    async def delay(seconds):
        delays.append(seconds)
    monkeypatch.setattr(security.asyncio, "sleep", delay)
    response = access_post(web, "wrong")
    assert response.status_code == 401 and delays == [1]
    assert "proofie_access" not in web.cookies and "wrong" not in response.text
    assert 'data-state="hosted-demo"' in response.text and 'name="csrf_token"' in response.text


def test_sixth_failure_per_address_is_limited_then_window_expires(gate, monkeypatch):
    app, web, _ = gate
    now = [100.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: now[0])
    async def no_delay(seconds):
        pass
    monkeypatch.setattr(security.asyncio, "sleep", no_delay)
    assert [access_post(web, "bad").status_code for _ in range(6)] == [401] * 5 + [429]
    now[0] += 601
    assert access_post(web, "bad").status_code == 401


def test_global_failure_limit_across_addresses(gate, monkeypatch):
    app, _, _ = gate
    async def no_delay(seconds):
        pass
    monkeypatch.setattr(security.asyncio, "sleep", no_delay)
    for number in range(50):
        web = TestClient(app, base_url="https://localhost", client=(f"10.0.0.{number + 1}", 1))
        assert access_post(web, "bad").status_code == 401
    web = TestClient(app, base_url="https://localhost", client=("10.0.1.1", 1))
    assert access_post(web, "bad").status_code == 429


def test_changing_code_invalidates_old_cookie_immediately(gate, monkeypatch):
    _, web, _ = gate
    access_post(web)
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "test-rotated-access-code")
    assert web.get("/probe", follow_redirects=False).status_code == 303
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "")
    assert web.get("/probe", follow_redirects=False).status_code == 303


def test_health_static_and_documentation_method_restrictions(gate):
    app, web, _ = gate
    assert web.get("/healthz").json() == {"status": "ok"}
    assert web.head("/healthz").status_code == 200
    assert web.get("/static/app.css").status_code == 200
    assert web.post("/healthz").status_code == web.post("/static/app.css").status_code == 401
    access_post(web)
    for target in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert web.get(target).status_code == 404


def test_webhook_exemption_only_when_flag_on_and_only_post(gate, monkeypatch):
    app, web, _ = gate
    @app.post("/webhooks/paypal")
    def receiver():
        return {"fake": True}
    assert web.post("/webhooks/paypal").status_code == 401
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS", "true")
    assert web.post("/webhooks/paypal").json() == {"fake": True}
    assert web.get("/webhooks/paypal", follow_redirects=False).status_code == 303
    assert web.get("/webhooks/events", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("value", ["", "short", "x" * 11])
def test_startup_requires_at_least_twelve_characters(monkeypatch, value):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", value)
    with pytest.raises(RuntimeError, match="COPILOT_ACCESS_CODE"):
        create_app()


@pytest.mark.parametrize("name", ["outcome_appeal"])
def test_startup_accepts_the_appeal_flag_but_still_needs_the_access_code(monkeypatch, name):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    monkeypatch.setenv("COPILOT_FEATURE_" + name.upper(), "true")
    assert create_app() is not None
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "short")
    with pytest.raises(RuntimeError, match="COPILOT_ACCESS_CODE"):
        create_app()


@pytest.mark.parametrize("target", ["//evil.example", "/\\evil.example", "https://evil.example", "/%2fevil.example", "/\nLocation:bad"])
def test_next_rejects_external_or_ambiguous_destinations(gate, target):
    _, web, _ = gate
    response = access_post(web, next_path=target)
    assert response.status_code == 303 and response.headers["location"] == "/disputes"


def test_access_code_is_read_per_call_and_unicode_compares_safely(gate, monkeypatch):
    _, web, _ = gate
    code = "test-code-with-unicode-é"
    monkeypatch.setenv("COPILOT_ACCESS_CODE", code)
    assert config.access_code() == code
    assert access_post(web, code).status_code == 303
    assert web.get("/probe").status_code == 200


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_rw_upload_approve_send_has_one_fake_post_followed_by_get(hosted_web, monkeypatch):
    import test_routes_send as existing
    fake = existing.FakeClient(post_send_get_bodies=[existing._confirmed_body()])
    web = hosted_web(lambda: fake)
    token = secrets.token_urlsafe(32)
    web.cookies.set("csrf_token", token, domain="localhost.local", path="/")
    package_id = existing._approved_package_with_tracking(web, config.db_path(), token)
    page = web.get(f"/disputes/{DID}/packages/{package_id}")
    assert page.status_code == 200
    assert "Anything you send here goes to the shared sandbox dispute; the next visitor will see it." in page.text
    assert f'action="/disputes/{DID}/packages/{package_id}/send"' in page.text
    response = web.post(f"/disputes/{DID}/packages/{package_id}/send", data={"csrf_token": page.cookies.get("csrf_token")})
    assert response.status_code == 200 and "Evidence sent to PayPal" in response.text
    assert len(fake.provide_evidence_calls) == 1
    assert fake.calls_log[-3:] == ["pre_send_get", "provide_evidence", "post_send_get"]


def test_rw_still_requires_approval_and_csrf_before_any_fake_call(hosted_web):
    import test_routes_send as existing
    fake = existing.FakeClient()
    web = hosted_web(lambda: fake)
    package_id = repo.insert_package(config.db_path(), DID, source="human_edit", validation_ok=False, review_flags=[])
    assert web.post(f"/disputes/{DID}/packages/{package_id}/send", data={"csrf_token": "wrong"}).status_code == 403
    web.cookies.set("csrf_token", "synthetic")
    assert web.post(f"/disputes/{DID}/packages/{package_id}/send", data={"csrf_token": "synthetic"}).status_code == 400
    assert fake.calls_log == []


def test_upload_budget_refuses_before_storage_and_allows_small_upload(hosted_web, monkeypatch):
    web = hosted_web()
    monkeypatch.setattr(config, "HOSTED_UPLOAD_BUDGET_BYTES", 1024)
    uploads = config.uploads_dir()
    uploads.mkdir()
    (uploads / "existing").write_bytes(b"x" * 900)
    web.cookies.set("csrf_token", "synthetic")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    response = web.post(UPLOAD, files={"file": ("a.png", png, "image/png")}, data={"csrf_token": "synthetic"}, follow_redirects=False)
    assert response.status_code == 413 and "upload limit" in response.text
    assert len(list(uploads.iterdir())) == 1 and repo.list_documents(config.db_path(), DID) == []
    monkeypatch.setattr(config, "HOSTED_UPLOAD_BUDGET_BYTES", 2048)
    response = web.post(UPLOAD, files={"file": ("a.png", png, "image/png")}, data={"csrf_token": "synthetic"}, follow_redirects=False)
    assert response.status_code == 303 and len(repo.list_documents(config.db_path(), DID)) == 1


@pytest.mark.parametrize("length_headers", [[], [(b"content-length", b"1")]])
def test_stream_cap_ignores_length_and_serializes_across_instances(monkeypatch, tmp_path, length_headers):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    monkeypatch.setattr(config, "HOSTED_UPLOAD_BUDGET_BYTES", 100)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "method": "POST", "path": UPLOAD,
             "raw_path": UPLOAD.encode(), "query_string": b"", "scheme": "https", "server": ("localhost", 443),
             "client": ("10.0.0.1", 1), "http_version": "1.1",
             "headers": [(b"cookie", ("proofie_access=" + security.access_cookie_value()).encode()), *length_headers]}
    calls = []
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(scope, receive, send):
            message = await receive()
            calls.append(message["body"])
            entered.set()
            await release.wait()
            config.uploads_dir().mkdir(exist_ok=True)
            (config.uploads_dir() / "stored").write_bytes(message["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        async def request(middleware, chunks):
            queue = deque({"type": "http.request", "body": body, "more_body": i < len(chunks)-1} for i, body in enumerate(chunks))
            async def receive():
                return queue.popleft() if queue else {"type": "http.disconnect"}
            messages = []
            async def send(message):
                messages.append(message)
            await middleware(scope, receive, send)
            return next(m["status"] for m in messages if m["type"] == "http.response.start")
        first, second = security.HostedAccessMiddleware(handler), security.HostedAccessMiddleware(handler)
        assert await request(first, [b"x" * 60, b"x" * 60]) == 413
        assert calls == []
        task = asyncio.create_task(request(first, [b"a" * 60]))
        await entered.wait()
        assert await request(second, [b"b" * 60]) == 503
        release.set()
        assert await task == 200
        assert await request(second, [b"b" * 60]) == 413
        assert calls == [b"a" * 60]
    asyncio.run(run())


def test_disconnected_upload_releases_own_lock_and_never_calls_handler(monkeypatch):
    monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    called = []
    async def handler(scope, receive, send):
        called.append(True)
    scope = {"type": "http", "method": "POST", "path": UPLOAD, "query_string": b"", "scheme": "https",
             "server": ("localhost", 443), "client": ("10.0.0.1", 1),
             "headers": [(b"cookie", ("proofie_access=" + security.access_cookie_value()).encode())]}
    async def run():
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            pass
        await security.HostedAccessMiddleware(handler)(scope, receive, send)
    asyncio.run(run())
    assert called == []
    assert not (config.uploads_dir().parent / ".proofie-upload.lock").exists()


def test_stale_upload_lock_is_preserved_and_refuses_safely(hosted_web):
    web = hosted_web()
    lock = config.uploads_dir().parent / ".proofie-upload.lock"
    lock.write_text("another worker", encoding="utf-8")
    response = web.post(UPLOAD, content=b"untrusted")
    assert response.status_code == 503 and lock.read_text(encoding="utf-8") == "another worker"
    assert repo.list_documents(config.db_path(), DID) == []


def test_access_body_is_bounded_before_form_parsing(gate):
    _, web, _ = gate
    response = web.post("/access", content=b"x" * 4097, headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413 and "proofie_access" not in web.cookies


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_local_review_render_matches_original_template_bytes(monkeypatch):
    from jinja2 import ChoiceLoader, DictLoader
    import test_routes_send as existing
    from copilot.web.routes_disputes import get_client, templates
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    fake = existing.FakeClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = TestClient(app, client=("127.0.0.1", 1))
    token = "synthetic"
    web.cookies.set("csrf_token", token)
    package_id = existing._approved_package_with_tracking(web, config.db_path(), token)
    response = web.get(f"/disputes/{DID}/packages/{package_id}")
    assert response.status_code == 200 and "hosted-shared-send" not in response.text
    root = Path(__file__).resolve().parents[2]
    fixtures = root / "tests/features/active_approval/fixtures"
    import hashlib
    baseline = (fixtures / "package_review-baseline-0.html").read_text(encoding="utf-8")   # the frozen baseline
    # The frozen baseline stays immutable; the page was deliberately changed later, in both modes.
    assert hashlib.sha256(baseline.encode()).hexdigest()=="09614b908184f1ad09310ff2cce18e0baca45bce7c41df735bd9fd43cad8695b"
    for fixture, digest in (("package_review-historic-1.html", "d4b2570088ab73c193b8eb90bd6e7afaa0b0385672b5e184e5f9c9ccb156f991"),
                            ("package_review-historic-2.html", "b11cf2c942c2bc6ff80d2e33baff55a7703f6d4e9ed19f393beeee9df420ae40")):
        frozen = (fixtures / fixture).read_text(encoding="utf-8")
        assert hashlib.sha256(frozen.encode()).hexdigest() == digest      # the historic fixtures stay asserted
    # Re-baseline, frozen from the merged template (one Check again per state);
    # any later edit of the template must come with a new fixture and hash.
    baseline = (fixtures / "package_review-frozen-3.html").read_text(encoding="utf-8")
    assert hashlib.sha256(baseline.encode()).hexdigest() == "83e85d5c4c0ad8f2a7b208884b08315cc4144d01e075edcb52c796562d9ba620"
    # An explicit, counted list of approved edits on top of that fixture (tests/local/_template_delta.py)
    import _template_delta as delta
    assert len(delta.edits()) == 107 and delta.apply(baseline) == delta.current_template()
    env = templates.env.overlay(loader=ChoiceLoader([DictLoader({"package_review.html": delta.apply(baseline)}),
                                                     templates.env.loader]))
    assert response.content == env.get_template("package_review.html").render(response.context).encode("utf-8")


# The browser's implicit favicon request must not invalidate an open access form.
def _first_access_token(response):
    import re
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_access_first_form_survives_favicon_request(gate, method):
    _, web, _ = gate
    first = web.get("/access")
    token = _first_access_token(first)
    assert web.cookies.get("csrf_token") == token
    favicon = web.request(method, "/favicon.ico", follow_redirects=False)
    assert favicon.status_code == 404
    assert "location" not in favicon.headers and "set-cookie" not in favicon.headers
    assert web.cookies.get("csrf_token") == token
    response = web.post("/access", data={"code": TEST_ACCESS_CODE, "csrf_token": token, "next": "/probe"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/probe"
    assert "proofie_access" in web.cookies


def test_access_second_get_preserves_original_form(gate):
    _, web, _ = gate
    first = web.get("/access")
    token = _first_access_token(first)
    second = web.get("/access")
    assert web.cookies.get("csrf_token") == token == _first_access_token(second)
    response = web.post("/access", data={"code": TEST_ACCESS_CODE, "csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303 and "proofie_access" in web.cookies


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_favicon_before_access_has_no_cookie_or_gate(gate, method):
    _, web, _ = gate
    response = web.request(method, "/favicon.ico", follow_redirects=False)
    assert response.status_code == 404 and response.content == b""
    assert "location" not in response.headers and "set-cookie" not in response.headers
    assert "csrf_token" not in web.cookies and "proofie_access" not in web.cookies


@pytest.mark.parametrize("bad", ["short", "a" * 42, "a" * 44, "!" + "a" * 42, "a" * 42 + "=", "a" * 21 + "." + "a" * 21])
def test_access_replaces_malformed_csrf_cookie(gate, bad):
    import re
    _, web, _ = gate
    web.cookies.set("csrf_token", bad, domain="localhost.local", path="/")
    response = web.get("/access")
    token = _first_access_token(response)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token) and token != bad
    assert web.cookies.get("csrf_token") == token
    assert web.post("/access", data={"code": TEST_ACCESS_CODE, "csrf_token": token}, follow_redirects=False).status_code == 303


def test_access_reuses_present_wellformed_token(gate):
    _, web, _ = gate
    token = secrets.token_urlsafe(32)
    web.cookies.set("csrf_token", token, domain="localhost.local", path="/")
    page = web.get("/access")
    assert web.cookies.get("csrf_token") == token == _first_access_token(page)


def test_favicon_exemption_is_exact_get_head_only(gate):
    _, web, _ = gate
    assert web.post("/favicon.ico").status_code == 401
    for path in ("/favicon.ico/extra", "/FAVICON.ICO"):
        assert web.get(path, follow_redirects=False).status_code == 303
    assert "csrf_token" not in web.cookies


def test_base_has_the_brand_favicon_and_no_empty_data_favicon():
    text = (Path(__file__).resolve().parents[2] / "copilot/web/templates/_base.html").read_text(encoding="utf-8")
    assert text.count('<link rel="icon" type="image/svg+xml" href="/static/brand/proofie-mark.svg">') == 1
    assert 'href="data:,"' not in text


def test_browser_followed_favicon_does_not_expire_first_form(gate):
    _, web, _ = gate
    original = _first_access_token(web.get("/access"))
    # A real browser follows the icon redirect automatically; TestClient needs this explicit request.
    web.get("/favicon.ico", follow_redirects=True)
    response = web.post("/access", data={"code": TEST_ACCESS_CODE, "csrf_token": original}, follow_redirects=False)
    assert response.status_code == 303 and "proofie_access" in web.cookies
