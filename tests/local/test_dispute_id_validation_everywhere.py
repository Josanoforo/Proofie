"""A single parametrized sweep confirming EVERY web route with a {dispute_id} path
segment rejects a malformed one (400, "That dispute reference is not valid.") before any PayPal call or database write -
not route-by-route scattered across each router's own test file, so a new route added later without this
guard shows up here as a missing case, not as a silent gap."""
from _hosted import authenticated_client, reset_csrf
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.app import create_app
from copilot.web.routes_disputes import get_client
from copilot.web.security import CSRF_COOKIE_NAME

BAD_ID = "not-a-real-paypal-id"


class _NeverCalledClient:
    """Standing in for SandboxClient: validation must refuse before any network call, so .call() must
    never be invoked by any of the routes below when dispute_id is malformed."""
    def call(self, method, path, op, **kw):
        raise AssertionError(f"unexpected PayPal call ({op}) - dispute_id validation should have refused "
                             f"before this")


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_client] = lambda: _NeverCalledClient()
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return c, token


# (method, path template, extra form/file data beyond csrf_token)
ROUTES = [
    ("get", "/disputes/{did}", None),
    ("get", "/disputes/{did}/due.ics", None),
    ("post", "/disputes/{did}/documents", {"data": {}, "files": {"file": ("x.pdf", b"%PDF-1.7", "application/pdf")}}),
    ("post", "/disputes/{did}/documents/1/exclude", None),
    ("post", "/disputes/{did}/documents/1/include", None),
    ("post", "/disputes/{did}/packages", None),
    ("post", "/disputes/{did}/manual-fields", None),
    ("post", "/disputes/{did}/documents/1/confirm-reading",
     {"data": {"field_name": "tracking_number", "value": "x"}}),
    ("get", "/disputes/{did}/packages/1", None),
    ("post", "/disputes/{did}/packages/1/attach/1", None),
    ("post", "/disputes/{did}/packages/1/detach/1", None),
    ("post", "/disputes/{did}/packages/1/claims", {"data": {"text": "a claim"}}),
    ("post", "/disputes/{did}/packages/1/claims/1", {"data": {"text": "edited"}}),
    ("post", "/disputes/{did}/packages/1/approve", None),
    ("post", "/disputes/{did}/packages/1/send", None),
    ("post", "/disputes/{did}/packages/1/check", None),
    ("post", "/disputes/{did}/packages/1/confirm-not-received", {"data": {"confirmed": "on"}}),
]


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.parametrize("method, path_template, extra", ROUTES,
                         ids=[f"{m}:{p}" for m, p, _ in ROUTES])
def test_malformed_dispute_id_is_rejected_before_any_network_call(client, method, path_template, extra):
    c, token = client
    path = path_template.format(did=BAD_ID)
    data = dict((extra or {}).get("data", {}))
    if method == "post":
        data["csrf_token"] = token
    kwargs = {"data": data} if method == "post" else {}
    if extra and "files" in extra:
        kwargs["files"] = extra["files"]
    resp = getattr(c, method)(path, **kwargs)
    assert resp.status_code == 400, f"{method.upper()} {path} -> {resp.status_code}: {resp.text[:200]}"
    assert "That dispute reference is not valid." in resp.text


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf


# The id is matched whole (fullmatch), so a trailing line break after a valid id is refused like any other bad id.
@pytest.mark.parametrize("path", ["/disputes/PP-R-DEMO-0000001%0A", "/disputes/PP-R-DEMO-0000001%0A/due.ics",
                                  "/disputes/PP-R-DEMO-0000001%0A/packages/1"])
def test_a_valid_id_followed_by_a_line_break_is_refused_before_any_network_call(client, path):
    c, token = client
    response = c.get(path)
    assert response.status_code == 400 and "That dispute reference is not valid." in response.text


def test_the_validator_itself_refuses_a_trailing_line_break_and_names_no_jargon():
    from copilot.web.security import validate_dispute_id
    assert validate_dispute_id("PP-R-DEMO-0000001") is None
    assert validate_dispute_id("PP-R-DEMO-0000001\n") == "That dispute reference is not valid. Open the dispute from All disputes."
    words = set(__import__("re").findall(r"[a-z]+", validate_dispute_id("x").lower()))
    assert not words & {"invalid", "id", "http", "regex"}
