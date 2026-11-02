"""A judge's first run, no dead ends. One test per item; each names the assertion that fails
without its fix. Offline: fake PayPal client, no model, no network."""
from _hosted import authenticated_client, reset_csrf
import json
import re
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pilot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dom import parse, state  # noqa: E402
from copilot import config  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.core import disputes  # noqa: E402
from copilot.db import repo  # noqa: E402
from copilot.web import routes_disputes  # noqa: E402
from copilot.web.routes_disputes import CredentialsMissing, get_client  # noqa: E402
from copilot.web.security import CSRF_COOKIE_NAME  # noqa: E402

REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"


class FakeClient:
    def __init__(self, items=None):
        self.items = [] if items is None else items

    def call(self, method, path, op, **kw):
        if op == "show_dispute":
            return WAITING
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        if op == "list_disputes":
            return {"items": self.items}
        raise AssertionError(f"unexpected op {op}")


def _client(fake=None, **kw):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake or FakeClient()
    c = authenticated_client(app, client=("127.0.0.1", 12345), **kw)
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return c, token


def _manual_package(c, token):
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


# ---- root redirect ---------------------------------------------------------------------------------------------------------
def test_a1_root_redirects_to_the_inbox():
    c, _ = _client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (303, 307) and r.headers["location"] == "/disputes"   # fails without the fix: 404


# ---- env example placeholders ---------------------------------------------------------------------------------------------------------
def test_a2_env_example_placeholders_count_as_missing(monkeypatch):
    monkeypatch.setattr(routes_disputes, "SandboxClient", lambda name: object())
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "your-sandbox-client-id-here")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "your-sandbox-client-secret-here")
    with pytest.raises(CredentialsMissing):   # fails without the fix: a client is built and OAuth fails with 401
        routes_disputes.get_client()


def test_a2_the_credentials_page_appears_for_placeholders(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "your-sandbox-client-id-here")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "your-sandbox-client-secret-here")
    monkeypatch.setattr(disputes.SandboxClient, "__init__", lambda self, *a, **k: None)
    app = create_app()
    c = authenticated_client(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)
    r = c.get("/disputes")
    assert r.status_code == 503 and state(parse(r.text), "hosted-no-credentials" if config.hosted_demo() else "credentials-missing") is not None
    assert "your-sandbox" not in r.text


def test_a2_a_real_looking_value_is_not_treated_as_a_placeholder(monkeypatch):
    class Built:
        def __init__(self, name):
            pass
    monkeypatch.setattr(routes_disputes, "SandboxClient", Built)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "AbCdEf123")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "your-secret-is-real")   # prefix only: not the placeholder shape
    assert isinstance(routes_disputes.get_client(), Built)


# ---- a complete manual package needs no override -------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c2_a_manual_package_with_content_has_no_empty_flag_and_approves_without_override():
    c, token = _client()
    pkg_id = _manual_package(c, token)
    db_path = config.db_path()
    c.post(f"/disputes/{DID}/packages/{pkg_id}/claims",
           data={"csrf_token": token, "text": "The parcel was shipped with Estafeta."}, follow_redirects=False)
    c.post(f"/disputes/{DID}/manual-fields",
           data={"csrf_token": token, "package_id": pkg_id, "tracking_number": "DEMO123456", "carrier": "Estafeta"},
           follow_redirects=False)
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "nothing has been entered yet" not in page.text            # fails without the fix: the flag never clears
    assert state(parse(page.text), "review-flags") is None
    fresh = page.cookies.get(CSRF_COOKIE_NAME)                         # the page rotated the CSRF token
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": fresh}, follow_redirects=False)
    assert r.status_code == 303, r.text                                # fails without the fix: 400, needs an override
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None


def test_c2_an_empty_manual_package_still_shows_the_flag():
    c, token = _client()
    pkg_id = _manual_package(c, token)
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "nothing has been entered yet" in page.text


# ---- send form ---------------------------------------------------------------------------------------------------------
def _send_form(page_text, pkg_id):
    return re.search(rf'<form[^>]*action="/disputes/{DID}/packages/{pkg_id}/send"', page_text)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c4_send_is_not_offered_without_tracking_and_says_why():
    c, token = _client()
    pkg_id = _manual_package(c, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/claims",
           data={"csrf_token": token, "text": "The parcel was shipped with Estafeta."}, follow_redirects=False)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303, r.text
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert _send_form(page.text, pkg_id) is None                       # fails without the fix: the Send form is there
    assert state(parse(page.text), "send-needs-tracking") is not None
    assert "tracking number" in page.text


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c4_send_is_offered_when_tracking_exists():
    c, token = _client()
    pkg_id = _manual_package(c, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/claims",
           data={"csrf_token": token, "text": "The parcel was shipped with Estafeta."}, follow_redirects=False)
    c.post(f"/disputes/{DID}/manual-fields",
           data={"csrf_token": token, "package_id": pkg_id, "tracking_number": "DEMO123456", "carrier": "Estafeta"},
           follow_redirects=False)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token}, follow_redirects=False)
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert _send_form(page.text, pkg_id) is not None
    assert state(parse(page.text), "send-needs-tracking") is None


# ---- refusal and expired-session pages -----------------------------------------------------------------------------------------------
def _is_error_page(r):
    dom = parse(r.text)
    return dom.find("h1") is not None and 'class="site-header"' in r.text and 'href="/disputes"' in r.text


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c6_approve_refusal_for_notices_renders_the_error_page_with_a_way_back():
    c, token = _client()
    pkg_id = repo.insert_package(config.db_path(), DID, source="ai", validation_ok=True, review_flags=["Check X"])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token})
    assert r.status_code == 400 and "open notices that need an explicit decision" in r.text and "Check X" in r.text
    assert _is_error_page(r)                                           # fails without the fix: a bare <p>
    assert f'href="/disputes/{DID}/packages/{pkg_id}"' in r.text


def test_c6_upload_refusal_renders_the_error_page_with_a_way_back():
    c, token = _client()
    r = c.post(f"/disputes/{DID}/documents", data={"csrf_token": token},
               files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400 and "Could not upload this file" in r.text
    assert _is_error_page(r)                                           # fails without the fix: a bare <p>
    assert f'href="/disputes/{DID}/workspace/prepare#documents"' in r.text               # Back to the documents


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.parametrize("path", [
    f"/disputes/{DID}/documents",
    f"/disputes/{DID}/packages",
    f"/disputes/{DID}/packages/1/approve",
    f"/disputes/{DID}/packages/1/send",
    f"/disputes/{DID}/manual-fields",
])
def test_c6_csrf_session_expired_is_the_styled_page_and_names_cookies(path):
    c, _ = _client()
    kwargs = {"files": {"file": ("a.pdf", b"x", "application/pdf")}} if path.endswith("/documents") else {}
    r = c.post(path, data={"csrf_token": "forged"}, **kwargs)
    assert r.status_code == 403 and "session expired" in r.text.lower()
    assert _is_error_page(r)                                           # fails without the fix: a bare <p>
    assert "cookies" in r.text.lower()


# ---- first-run details ---------------------------------------------------------------------------------------
def test_c7_generate_button_says_it_can_take_a_few_minutes():
    c, _ = _client()
    repo.insert_document(config.db_path(), DID, "a.pdf", "application/pdf", 10, "sha-a", "Carrier: FedEx", True)   # the Generate step needs a document
    r = c.get(f"/disputes/{DID}")
    expected = ("Preparing the package. Keep this page open." if config.hosted_demo()
                else "Preparing with the assistant. This can take a few minutes. Keep this page open.")
    assert expected in r.text


def _waiting_item():
    return WAITING


def test_c8_inbox_never_reloads_by_itself_and_offers_a_visible_reload_while_a_row_is_preparing(monkeypatch):
    """no http-equiv in either state; a Reload link while preparing."""
    c, _ = _client()
    monkeypatch.setattr(routes_disputes, "fetch_all_disputes", lambda client: ([WAITING], 1))
    monkeypatch.setattr(routes_disputes.auto_prep, "should_prepare", lambda norm: True)
    monkeypatch.setattr(routes_disputes.repo, "claim_auto_prep", lambda *a, **k: "tok")
    monkeypatch.setattr(routes_disputes, "_run_auto_prep", lambda *a, **k: None)
    r = c.get("/disputes")
    assert "http-equiv" not in r.text and "<meta" in r.text                         # other meta tags exist; no refresh one
    note = parse(r.text).find("p", data_state="preparing")
    assert note is not None and note.find("a").text() == "Reload to see progress" and note.find("a").attrs["href"] == "/disputes"
    monkeypatch.setattr(routes_disputes.auto_prep, "should_prepare", lambda norm: False)
    r2 = c.get("/disputes")
    assert "http-equiv" not in r2.text and parse(r2.text).find("p", data_state="preparing") is None


def test_c11_the_dispute_page_has_no_raw_dict_dump():
    c, _ = _client()
    r = c.get(f"/disputes/{DID}")
    assert "{'" not in r.text and "&#39;dispute_id&#39;" not in r.text    # fails without the fix: the dict repr
    assert 'data-state="summary"' in r.text                          # the readable details stay


def test_c12_pending_send_wording_only_when_a_send_is_pending():
    c, _ = _client()
    r = c.get(f"/disputes/{DID}")
    assert "including any pending send" not in r.text                  # fails without the fix
    assert "Check again" in r.text


# ---- unknown status ---------------------------------------------------------------------------------------------------------
def test_p3_unknown_status_shows_no_repo_path():
    text = disputes.humanize_status("BRAND_NEW_STATUS")
    assert "copilot/" not in text and "disputes.py" not in text        # fails without the fix
    assert text == "Status not recognised: BRAND_NEW_STATUS"


# ---- legacy approval ----------------------------------------------------------------------------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_fix2_n1_approving_an_empty_manual_package_is_refused_without_an_override_even_with_a_blank_one():
    c, token = _client()
    pkg_id = _manual_package(c, token)                                  # no claims, no manual fields
    path = f"/disputes/{DID}/packages/{pkg_id}/approve"
    absent = c.post(path, data={"csrf_token": token})                   # the override field is not submitted at all
    assert absent.status_code == 400 and "open notices that need an explicit decision" in absent.text
    blank = c.post(path, data={"csrf_token": token, "sufficiency_override": "   "})   # a blank value is no override
    assert blank.status_code == 400 and "nothing has been entered yet" in blank.text
    assert repo.get_latest_valid_approval(config.db_path(), pkg_id) is None


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf
