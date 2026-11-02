"""Local security. Two independent pieces:

1. LocalhostOnlyMiddleware: defense in depth against the app ever being reachable from outside this
   machine. The PRIMARY control is launching uvicorn with host="127.0.0.1" (see copilot/app.py); this
   middleware rejects any request whose peer address is not loopback even if that primary control is ever
   misconfigured (e.g. someone changes the host to "0.0.0.0" without reading why it was 127.0.0.1).
2. CSRF: a double-submit cookie. Every GET that renders a page carrying a POST form gets a fresh random
   token in a cookie; the template embeds the same token in a hidden field; the POST handler requires both
   to be present and equal. No login/session system exists yet (single local vendor user), so there is no
   server-side session store to tie the token to - the cookie IS the session for this one purpose.
"""
from __future__ import annotations

import html
import asyncio
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import time
from collections import deque
from urllib.parse import quote, unquote

from copilot import config

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

CSRF_COOKIE_NAME = "csrf_token"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# the ONE dispute_id format both the web routes and the MCP server (copilot/mcp_server.py) validate
# an untrusted dispute_id against, so the two can never silently drift apart. PayPal's own ids look like
# PP-R-DEMO-0000001 - no slash, scheme or whitespace, so a valid id can never smuggle a path/URL/script
# into an f-string-built HTTP path or into HTML (the templates also escape every value; this check is a second line of defence).
DISPUTE_ID_RE = re.compile(r"^PP-[A-Z0-9]{1,4}(?:-[A-Z0-9]{1,24}){1,4}$")
MAX_DISPUTE_ID_LEN = 48


def validate_dispute_id(dispute_id: str) -> str | None:
    """None if `dispute_id` matches PayPal's id shape; otherwise a plain-language reason to refuse."""
    if not dispute_id or len(dispute_id) > MAX_DISPUTE_ID_LEN or not DISPUTE_ID_RE.fullmatch(dispute_id):
        return "That dispute reference is not valid. Open the dispute from All disputes."
    return None


def rejected_dispute_id(dispute_id: str) -> HTMLResponse | None:
    """One-line guard for every web route with a {dispute_id} path segment (GET, POST, and
    the ones that redirect) - call this FIRST, before any PayPal call, redirect target, or HTML built from
    dispute_id. None means proceed; otherwise return the HTMLResponse as-is."""
    err = validate_dispute_id(dispute_id)
    if not err:
        return None
    # Presentation only; validation above and every caller's early return remain unchanged.
    from copilot.web.routes_disputes import templates
    body = templates.env.get_template("error.html").render(
        title="Error: Check the dispute id", message=err, kind="error", state="refused",
        items=[], conflict_form=None, show_last_read=False, retry_url=None,
        back_url="/disputes", back_label="All disputes")
    return HTMLResponse(body, status_code=400)


class LocalhostOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client = request.client
        host = client.host if client else None
        if host not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "This app only accepts connections from this machine."},
                                status_code=403)
        return await call_next(request)


ACCESS_COOKIE_NAME = "proofie_access"
# Same shape as copilot.features.status_link.service.TOKEN_PATH, kept here so the gate imports no feature module.
STATUS_LINK_PATH = re.compile(r"^/s/[A-Za-z0-9_.-]{20,1500}$")
UPLOAD_LIMIT_MESSAGE = "This demo has reached its upload limit. Come back after the host clears the demo files."
UPLOAD_BUSY_MESSAGE = "Another upload is in progress. Try again in a moment."
UPLOAD_UNAVAILABLE_MESSAGE = "Uploads are unavailable right now. Come back later."
# Every route that accepts a file in the hosted demo: case documents, third-party statements (text or photo), and the
# shipment-moment photos. They share one streaming body cap and one budget.
UPLOAD_PATH = re.compile(r"^/(disputes/[^/]+/(documents|third-party-statements)|fulfilment/[^/]+/moment)/?$")
# Third-party photos are kept in this SQLite drawer under the data folder (not in uploads); its bytes count toward the budget.
BUDGET_DRAWER = "third_party_statements.sqlite3"
MAX_UPLOAD_BODY_BYTES = 10 * 1024 * 1024 + 64 * 1024


def access_cookie_value() -> str:
    code = config.access_code()
    if len(code) < 12:
        return ""
    return hmac.new(code.encode("utf-8"), b"proofie-access-v1", hashlib.sha256).hexdigest()


def safe_next(value: str | None) -> str:
    value = value or ""
    decoded = unquote(value)
    if (not value.startswith("/") or decoded.startswith("//") or "\\" in decoded
            or "%" in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded)):
        return "/disputes"
    return value


class AccessAttemptLimiter:
    """A bounded, process-local failure window. Record before the asynchronous delay."""
    def __init__(self):
        self.failures = deque()

    def blocked(self, address: str) -> bool:
        now = time.monotonic()
        while self.failures and now - self.failures[0][0] >= 600:
            self.failures.popleft()
        return len(self.failures) >= 50 or sum(a == address for _, a in self.failures) >= 5

    def record_failure(self, address: str) -> None:
        self.failures.append((time.monotonic(), address))


class HostedAccessMiddleware:
    """Hosted gate and pre-handler streaming upload budget; local mode does not install this class."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive=receive)
        method, path = request.method, request.url.path
        # Browser icon fetches must not redirect through /access and replace its CSRF cookie.
        if path == "/favicon.ico" and method in ("GET", "HEAD"):
            return await Response(status_code=404)(scope, receive, send)
        exempt = ((path == "/healthz" and method in ("GET", "HEAD"))
                  or (path == "/healthz/features" and method == "GET")
                  or (path == "/access" and method in ("GET", "POST"))
                  or (path.startswith("/static/") and method == "GET")
                  # the buyer's signed, read-only status page (B4): one path shape, GET/HEAD only, flag-gated
                  or (STATUS_LINK_PATH.fullmatch(path) and method in ("GET", "HEAD") and config.feature_enabled("status_link"))
                  or (path == "/webhooks/paypal" and method == "POST" and config.feature_enabled("webhooks")))
        expected = access_cookie_value()
        cookie = request.cookies.get(ACCESS_COOKIE_NAME, "")
        if not exempt and not (expected and secrets.compare_digest(cookie.encode("utf-8"), expected.encode("ascii"))):
            if method in ("GET", "HEAD"):
                response = RedirectResponse("/access?next=" + quote(safe_next(path), safe=""), status_code=303)
            else:
                response = HTMLResponse('<p>Enter the access code before continuing.</p>', status_code=401)
            return await response(scope, receive, send)
        if method == "POST" and path == "/access":
            return await self._bounded_call(scope, receive, send, 4096, "This request is too large.")
        if method == "POST" and UPLOAD_PATH.fullmatch(path):
            return await self._upload(scope, receive, send)
        return await self.app(scope, receive, send)

    async def _bounded_call(self, scope, receive, send, limit, message, directory=None):
        # Bound actual ASGI chunks, never the client's Content-Length. No handler/parser sees an over-budget body.
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024, dir=directory) as body:
            size = 0
            try:
                async with asyncio.timeout(30):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        chunk = event.get("body", b"")
                        size += len(chunk)
                        if size > limit:
                            return await HTMLResponse(f"<p>{message}</p>", status_code=413)(scope, receive, send)
                        body.write(chunk)
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                return await HTMLResponse("<p>The upload took too long. Try again.</p>", status_code=408)(scope, receive, send)
            body.seek(0)
            sent, finished = 0, False
            async def replay():
                nonlocal sent, finished
                if finished:
                    return await receive()
                chunk = body.read(64 * 1024)
                sent += len(chunk)
                finished = sent >= size
                return {"type": "http.request", "body": chunk, "more_body": not finished}
            return await self.app(scope, replay, send)

    async def _upload(self, scope, receive, send):
        uploads = config.uploads_dir()
        lock = uploads.parent / ".proofie-upload.lock"
        owned_lock = False
        try:
            uploads.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return await HTMLResponse(f"<p>{UPLOAD_BUSY_MESSAGE}</p>", status_code=503)(scope, receive, send)
            owned_lock = True
            os.close(fd)
            used = sum(p.stat().st_size for p in uploads.rglob("*") if p.is_file())
            used += sum(p.stat().st_size for p in config.data_dir().glob(BUDGET_DRAWER + "*") if p.is_file())
            available = config.HOSTED_UPLOAD_BUDGET_BYTES - used
            if available <= 0:
                return await HTMLResponse(f"<p>{UPLOAD_LIMIT_MESSAGE}</p>", status_code=413)(scope, receive, send)
            # The whole multipart body counts conservatively, including framing and form fields.
            return await self._bounded_call(scope, receive, send, min(available, MAX_UPLOAD_BODY_BYTES),
                                            UPLOAD_LIMIT_MESSAGE, directory=str(uploads.parent))
        except OSError:
            return await HTMLResponse(f"<p>{UPLOAD_UNAVAILABLE_MESSAGE}</p>", status_code=503)(scope, receive, send)
        finally:
            if owned_lock:
                lock.unlink(missing_ok=True)


class HostedPoolMiddleware:
    """Hosted demo only (installed just inside the access gate): a request whose path names a dispute outside the judge pool is
    refused here (403), before any route, form parser or PayPal call. The record case answers only a GET of its own page; every
    other path or method on it is refused too (403). Local mode does not install this class."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from copilot.core import pool_scope
        path = scope.get("path", "")
        if path in ("/judge", "/replay") or path.startswith(("/judge/", "/replay/")):
            # judge routes refuse a non-pool id themselves, before any PayPal read; the replay page reads only its committed
            # synthetic fixture (copilot/features/replay) and answers an unknown case with its own 404
            return await self.app(scope, receive, send)
        for segment in path.split("/"):
            if not (segment and len(segment) <= MAX_DISPUTE_ID_LEN and DISPUTE_ID_RE.fullmatch(segment)):
                continue
            if pool_scope.readable(segment) and not (pool_scope.is_record(segment) and not pool_scope.writable(segment)):
                continue
            refused = None
            if not pool_scope.readable(segment):
                refused = (403, pool_scope.REFUSAL_TITLE, pool_scope.REFUSAL_MESSAGE)
            elif scope.get("method") not in ("GET", "HEAD") or path.rstrip("/") != f"/disputes/{segment}":
                refused = (403, pool_scope.RECORD_TITLE, pool_scope.RECORD_MESSAGE)
            if refused:
                from copilot.web.routes_disputes import refusal_page
                request = Request(scope, receive=receive)
                response = refusal_page(request, *refused, back_url="/disputes", back_label="All disputes")
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def new_csrf_token() -> str:
    """Generates a fresh token value. Call this BEFORE rendering a template that embeds it as a hidden
    field (the template needs the value at render time); pair with set_csrf_cookie on the response you
    return, so the same value ends up in both the cookie and the form."""
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(CSRF_COOKIE_NAME, token, httponly=True, samesite="strict", secure=config.hosted_demo())


def issue_csrf_token(response: Response) -> str:
    """Convenience for the common case (no template needs the value): generates a token, sets it as a
    cookie on `response`, and returns it. If a template must also EMBED the token (a form on the same
    page), use new_csrf_token() first instead, so the value is available before the template renders."""
    token = new_csrf_token()
    set_csrf_cookie(response, token)
    return token


def verify_csrf(request: Request, submitted_token: str | None) -> bool:
    """True only if the cookie set by issue_csrf_token matches what the form submitted. Constant-time
    comparison so this check itself can't leak the token via timing."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token:
        return False
    return secrets.compare_digest(cookie_token, submitted_token)
