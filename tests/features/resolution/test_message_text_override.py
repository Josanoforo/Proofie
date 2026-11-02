"""The seller's reviewed text through the resolution feature's own message path: same body shape, same hash, one POST."""
from _hosted import authenticated_client, reset_csrf
import html as htmllib
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot.app import create_app
from copilot.features.resolution import actions as act
from copilot.features.resolution import service, store
from copilot.web.routes_disputes import get_client
from resolution_fakes import BASE, DISPUTE_ID, FakePayPal

TOKEN = "test-csrf-token"
PAGE = f"/disputes/{DISPUTE_ID}/resolution"
SELLER_TEXT = "Hello Ana, your parcel DEMO-FDX-000001 left on 2026-09-30. If it has not arrived, reply here and I will refund you."


@pytest.fixture(autouse=True)
def _no_httpx_calls(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no Ollama and no PayPal call is allowed in these tests")
    monkeypatch.setattr(httpx, "post", refuse)
    monkeypatch.setattr(httpx, "get", refuse)


def _web(monkeypatch, fake=None):
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    fake = fake or FakePayPal()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, TOKEN)
    return web, fake


def _preview(web, **form):
    return web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "message", **form})


def _confirm(html):
    form = re.search(r'<form[^>]*data-state="confirm-form"[^>]*>(.*?)</form>', html, re.S).group(1)
    fields = {k: htmllib.unescape(v) for k, v in re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form)}
    return {**fields, "csrf_token": TOKEN, "approval_sha256": re.search(r'data-state="request-sha256">([0-9a-f]{64})<', html).group(1)}


# ---- prepare_message ---------------------------------------------------------------------------------------------
def test_a_given_text_is_the_body_and_the_hash_is_over_that_body():
    p = service.prepare_message(FakePayPal(), DISPUTE_ID, SELLER_TEXT)
    assert p.allowed and p.body == {"message": SELLER_TEXT}
    assert p.sha256 == act.request_sha256({"message": SELLER_TEXT})
    template = service.prepare_message(FakePayPal(), DISPUTE_ID)
    assert p.sha256 != template.sha256 and p.body != template.body


def test_the_text_is_cleaned_and_bounded_by_the_same_helper_as_the_template_path():
    assert service.prepare_message(FakePayPal(), DISPUTE_ID, "   padded text   ").body == {"message": "padded text"}
    too_long = service.prepare_message(FakePayPal(), DISPUTE_ID, "x" * (act.MAX_TEXT_LEN + 1))
    assert not too_long.allowed and too_long.body is None and too_long.sha256 is None and "characters" in too_long.reason
    assert service.prepare_message(FakePayPal(), DISPUTE_ID, "x" * act.MAX_TEXT_LEN).allowed


@pytest.mark.parametrize("bad", ["see http://example.test", "SEE HTTPS://X.TEST", "go to www.example.test", "mail me@example.com"])
def test_a_link_or_an_email_in_the_text_is_refused_with_no_body(bad):
    p = service.prepare_message(FakePayPal(), DISPUTE_ID, bad)
    assert not p.allowed and p.body is None and p.sha256 is None
    marker = {"http": "http", "www": "www.", "@": "@"}[next(k for k in ("http", "www", "@") if k in bad.lower())]
    assert p.reason == (f"Your message contains \u201c{marker}\u201d. Messages sent through Proofie cannot include links or email "
                        f"addresses. Remove \u201c{marker}\u201d and review again.")


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t"])
def test_an_empty_text_keeps_the_template_exactly_as_before(empty):
    default = service.prepare_message(FakePayPal(), DISPUTE_ID)
    p = service.prepare_message(FakePayPal(), DISPUTE_ID, empty)
    assert p.allowed and p.body == default.body and p.sha256 == default.sha256
    assert p.body["message"].startswith("Hello, this is about your order of 28 September 2026 for 40.00 USD.")


def test_the_order_rule_still_applies_with_a_text():
    fake = FakePayPal(phase="after_offer", status="WAITING_FOR_BUYER_RESPONSE")
    fake.pending = None
    p = service.prepare_message(FakePayPal(phase="after_message"), DISPUTE_ID, SELLER_TEXT)
    assert not p.allowed and p.body is None                                 # no send-message link after a message
    assert service.prepare_message(fake, DISPUTE_ID, SELLER_TEXT).allowed    # the link is still there after an offer


def test_the_economics_and_next_step_are_unchanged_by_a_text():
    a = service.prepare_message(FakePayPal(), DISPUTE_ID)
    b = service.prepare_message(FakePayPal(), DISPUTE_ID, SELLER_TEXT)
    assert a.economics == b.economics and a.next_step_text == b.next_step_text


def test_page_state_exposes_the_template_text_for_the_textarea():
    state = service.page_state(FakePayPal(), DISPUTE_ID)
    assert state.template_text == service.prepare_message(FakePayPal(), DISPUTE_ID).body["message"]


# ---- the preview route and register --------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_the_page_has_a_textarea_prefilled_with_the_template(monkeypatch):
    web, _fake = _web(monkeypatch)
    page = web.get(PAGE).text
    assert '<textarea id="field-text" name="text"' in page
    assert "Hello, this is about your order of 28 September 2026 for 40.00 USD." in page


@pytest.mark.local_spec
def test_the_preview_with_a_text_shows_the_sellers_body_and_the_hash_of_it(monkeypatch):
    web, fake = _web(monkeypatch)
    r = _preview(web, text=SELLER_TEXT)
    assert r.status_code == 200 and "Hello Ana, your parcel DEMO-FDX-000001 left on 2026-09-30." in r.text
    form = _confirm(r.text)
    assert form["approval_sha256"] == act.request_sha256({"message": SELLER_TEXT})
    assert not fake.posts()


@pytest.mark.local_spec
def test_the_preview_without_a_text_is_the_template_as_today(monkeypatch):
    web, fake = _web(monkeypatch)
    r = _preview(web, text="")
    assert r.status_code == 400 and "There is a problem" in r.text and 'href="#field-text"' in r.text
    assert "Hello Ana" not in r.text and not fake.posts()
    assert 'data-state="confirm-form"' not in r.text
    # The service's existing template/body/hash default is unchanged; the actual form now requires reviewed text.
    prepared=service.prepare_message(FakePayPal(), DISPUTE_ID)
    assert prepared.sha256 == act.request_sha256(prepared.body)
    assert "Hello, this is about your order" in prepared.body["message"]


@pytest.mark.local_spec
def test_the_preview_with_a_link_is_refused_and_posts_nothing(monkeypatch):
    web, fake = _web(monkeypatch)
    r = _preview(web, text="see http://example.test")
    assert r.status_code == 400 and 'class="error-summary"' in r.text and "cannot include links or email addresses" in r.text
    assert 'href="#field-text"' in r.text and "see http://example.test" in r.text
    assert not fake.posts()


@pytest.mark.local_spec
def test_register_is_unchanged_one_post_with_the_sellers_text_pending_then_settled_by_get(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=0))
    form = _confirm(_preview(web, text=SELLER_TEXT).text)
    reset_csrf(web); set_test_csrf(web, TOKEN)
    r = web.post(PAGE + "/register", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert [c[1] for c in fake.posts()] == [f"{BASE}/send-message"] and len(fake.posts()) == 1
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["action"] == "message" and row["status"] == "sent_pending_settle"
    assert row["request_sha256"] == form["approval_sha256"] == act.request_sha256({"message": SELLER_TEXT})
    reset_csrf(web); set_test_csrf(web, TOKEN)
    assert web.get(PAGE).status_code == 200
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_confirmed"            # settled by the page's own GET


@pytest.mark.local_spec
def test_a_changed_text_with_the_old_hash_is_still_rejected_by_register(monkeypatch):
    web, fake = _web(monkeypatch)
    form = _confirm(_preview(web, text=SELLER_TEXT).text)
    form["approval_sha256"] = act.request_sha256({"message": "A different text"})      # a hash that is not the stored body's
    reset_csrf(web); set_test_csrf(web, TOKEN)
    assert web.post(PAGE + "/register", data=form).status_code == 409 and not fake.posts()


# ---- the same rules at write time -------------------------------------------------------
def _register_body(web, message):
    """A record stored behind the app's back (a body that never passed the preview), with its OWN matching hash: register
    must still apply the write-time rules."""
    body = {"message": message}
    token = store.save_check(DISPUTE_ID, "message", body, act.request_sha256(body), "seller")
    form = {"csrf_token": TOKEN, "action": "message", "token": token, "approval_sha256": act.request_sha256(body)}
    return web.post(PAGE + "/register", data=form, follow_redirects=False)


@pytest.mark.local_spec
@pytest.mark.parametrize("bad", ["see http://example.test", "visit www.example.test", "mail me@example.com"])
def test_a_hand_built_register_body_with_a_link_is_rejected_with_no_post(monkeypatch, bad):
    web, fake = _web(monkeypatch)
    r = _register_body(web, bad + " I will refund you.")
    assert r.status_code == 409 and "cannot include links or email addresses" in r.text
    assert not fake.posts() and store.list_actions(DISPUTE_ID) == []


@pytest.mark.local_spec
@pytest.mark.parametrize("message", ["x" * (act.MAX_TEXT_LEN + 1), "   "])
def test_a_hand_built_register_body_over_the_bound_or_blank_is_rejected_with_no_post(monkeypatch, message):
    web, fake = _web(monkeypatch)
    r = _register_body(web, message)
    assert r.status_code == 409 and not fake.posts() and store.list_actions(DISPUTE_ID) == []


@pytest.mark.local_spec
def test_a_clean_hand_built_text_still_registers_with_one_post(monkeypatch):
    web, fake = _web(monkeypatch)
    r = _register_body(web, SELLER_TEXT)
    assert r.status_code == 303 and [c[1] for c in fake.posts()] == [f"{BASE}/send-message"]
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_pending_settle"


def test_register_at_service_level_says_rejected_body_for_a_link_and_for_an_overlong_text():
    for message in ("go to http://example.test", "y" * (act.MAX_TEXT_LEN + 1)):
        body = {"message": message}
        out = service.register(FakePayPal(), DISPUTE_ID, "message", body, act.request_sha256(body))
        assert out.status == "rejected_body"


def test_prepare_message_and_body_problem_share_one_helper_so_they_cannot_drift():
    import inspect
    assert hasattr(service, "message_text_problem")
    assert "message_text_problem" in inspect.getsource(service.prepare_message)
    assert "message_text_problem" in inspect.getsource(service._body_problem)
    assert service.message_text_problem("fine text") is None
    assert "cannot include links or email addresses" in service.message_text_problem("see https://x.test")
    assert service.message_text_problem("   ") and service.message_text_problem("z" * (act.MAX_TEXT_LEN + 1))


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


def test_cd_write_configuration_keeps_access_csrf_and_approval_hash(web_mode, monkeypatch):
    from copilot import config
    fake = FakePayPal()
    web, observed = _web(monkeypatch, fake)
    assert web.app is not None and observed is fake and fake.calls == []
    target = PAGE + "/register"
    if config.hosted_demo():
        naked = TestClient(web.app, base_url="https://localhost")
        assert naked.post(target, data={}).status_code == 401
        assert fake.calls == []
    assert web.post(target, data={}).status_code == 403
    assert fake.calls == []
    set_test_csrf(web, TOKEN)
    rejected = web.post(target, data={"csrf_token": TOKEN, "action": "message", "token": "no-such-token",
                                     "approval_sha256": "0" * 64})
    assert rejected.status_code == 410 and "This check has expired. Nothing was sent." in rejected.text
    assert fake.calls == []
    set_test_csrf(web, TOKEN)
    preview = web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "message",
                                              "text": "Synthetic seller message"})
    assert preview.status_code == 200 and 'data-state="confirm-form"' in preview.text
    assert not fake.posts()


def test_safe_message_preview_in_both_modes(web_mode, monkeypatch):
    from copilot import config
    from copilot.app import create_app
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.delenv("COPILOT_FEATURE_RESOLUTION_WRITES", raising=False)
    fake = FakePayPal()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app)
    set_test_csrf(web, TOKEN)
    r = _preview(web, text=SELLER_TEXT)
    assert r.status_code == 403 and "Writes are switched off" in r.text
    assert fake.posts() == []

from _hosted import set_test_csrf
