"""The same approved message or offer is sent once. A repeat click after a confirmed, pending or
unconfirmed send makes no PayPal call at all; two simultaneous submits make one POST; a definite refusal can be
approved again; different text is a different request. Synthetic fakes only, no network."""
import re
import threading
import time

import httpx
import pytest
from _hosted import authenticated_client, reset_csrf
from fastapi.testclient import TestClient

from copilot import config
from copilot.core import write_guard
from copilot.app import create_app
from copilot.features.resolution import service, store
from copilot.web.routes_disputes import get_client
from resolution_fakes import BASE, DISPUTE_ID, FakePayPal, paypal_error

PAGE = f"/disputes/{DISPUTE_ID}/resolution"
TOKEN = "test-csrf-token"


def _message(fake, text=None):
    p = service.prepare_message(fake, DISPUTE_ID, text)
    assert p.allowed, p.reason
    return p


def _offer(fake, cents=1000, note="Partial refund while we check the shipment"):
    p = service.prepare_offer(fake, DISPUTE_ID, cents, note)
    assert p.allowed, p.reason
    return p


def _reopen(fake):
    """PayPal offers the write links again (as in a new round with the buyer): only the app can refuse a repeat."""
    fake.phase, fake.pending, fake.status = "fresh", None, "WAITING_FOR_SELLER_RESPONSE"


def _posts(fake):
    return [c for c in fake.calls if c[0] == "POST"]


# ---- service level: no second POST, no PayPal call at all --------------------------------------------------------
def test_repeat_message_after_confirmed_makes_no_call_and_says_already_sent():
    fake = FakePayPal(settling_reads=0)
    m = _message(fake)
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "sent_pending_settle"
    assert service.settle_pending(fake, DISPUTE_ID) == 1
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_confirmed"
    # PayPal still shows the send-message link (e.g. a second round with the buyer): the app alone must refuse
    _reopen(fake)
    calls_before = len(fake.calls)
    out = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert out.status == "already_sent"
    assert out.existing is not None and out.existing["status"] == "sent_confirmed"
    assert len(fake.calls) == calls_before, "a blocked repeat must not call PayPal (not even a GET)"
    assert len(_posts(fake)) == 1 and len(store.list_actions(DISPUTE_ID)) == 1


def test_repeat_message_after_pending_makes_no_call():
    fake = FakePayPal(settling_reads=99)
    m = _message(fake)
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "sent_pending_settle"
    _reopen(fake)
    calls_before = len(fake.calls)
    out = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert out.status == "already_sent" and out.existing["status"] == "sent_pending_settle"
    assert len(fake.calls) == calls_before and len(_posts(fake)) == 1


def test_repeat_message_after_unconfirmed_makes_no_call():
    fake = FakePayPal(post_raises=httpx.ConnectError("boom"))
    m = _message(fake)
    first = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert first.status == "sent_unconfirmed"
    calls_before = len(fake.calls)
    out = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert out.status == "already_sent" and out.existing["status"] == "sent_unconfirmed"
    assert len(fake.calls) == calls_before and len(_posts(fake)) == 1


def test_repeat_offer_after_confirmed_is_blocked_too():
    fake = FakePayPal(settling_reads=0)
    o = _offer(fake)
    service.register(fake, DISPUTE_ID, "offer", o.body, o.sha256)
    service.settle_pending(fake, DISPUTE_ID)
    _reopen(fake)
    calls_before = len(fake.calls)
    assert service.register(fake, DISPUTE_ID, "offer", o.body, o.sha256).status == "already_sent"
    assert len(fake.calls) == calls_before and len(_posts(fake)) == 1


def test_a_row_stored_before_this_release_also_blocks_the_same_body():
    """A database from before the guard, with two stored rows for one body, must not allow a third."""
    fake = FakePayPal()
    m = _message(fake)
    store.save_action(DISPUTE_ID, "message", m.body, m.sha256, m.sha256, {}, True, "sent_confirmed")
    calls_before = len(fake.calls)
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "already_sent"
    assert len(fake.calls) == calls_before and not _posts(fake)


def test_two_threads_registering_at_once_make_exactly_one_post():
    class SlowPost(FakePayPal):
        def _post(self, path, kw):
            time.sleep(0.4)                       # hold the window open so both threads are past the early checks
            return super()._post(path, kw)

    fake = SlowPost(settling_reads=99)
    m = _message(fake)
    barrier, results = threading.Barrier(2), []

    def go():
        barrier.wait()
        results.append(service.register(fake, DISPUTE_ID, "message", m.body, m.sha256))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    assert sorted(r.status for r in results) == ["already_sent", "sent_pending_settle"]
    assert len(_posts(fake)) == 1 and len(store.list_actions(DISPUTE_ID)) == 1


def test_refused_422_then_a_new_approval_is_allowed():
    fake = FakePayPal(post_raises=paypal_error(422))
    m = _message(fake)
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "refused_422"
    fake.post_raises = None
    out = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert out.status == "sent_pending_settle"
    assert len(_posts(fake)) == 2


def test_a_paypal_error_with_a_status_is_a_definite_refusal_and_can_be_approved_again():
    fake = FakePayPal(post_raises=paypal_error(400, "INVALID_REQUEST"))
    m = _message(fake)
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "failed"
    fake.post_raises = None
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "sent_pending_settle"
    assert len(_posts(fake)) == 2


def test_different_text_is_a_different_request_and_is_allowed():
    fake = FakePayPal(settling_reads=0)
    first = _message(fake, "First message to the buyer about the shipment.")
    service.register(fake, DISPUTE_ID, "message", first.body, first.sha256)
    service.settle_pending(fake, DISPUTE_ID)
    _reopen(fake)
    second = _message(fake, "A different message with other words for the buyer.")
    assert second.sha256 != first.sha256
    assert service.register(fake, DISPUTE_ID, "message", second.body, second.sha256).status == "sent_pending_settle"
    assert len(_posts(fake)) == 2


def test_an_unexpected_exception_during_the_post_leaves_the_outcome_unknown_and_blocks_a_repeat():
    class Crash(FakePayPal):
        def _post(self, path, kw):
            raise RuntimeError("programming error inside the client")

    fake = Crash()
    m = _message(fake)
    with pytest.raises(RuntimeError):
        service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    fake.post_raises = None
    again = service.register(FakePayPal(), DISPUTE_ID, "message", m.body, m.sha256)
    assert again.status == "already_sent"


# ---- web level: PRG, the page after it, the repeat submit --------------------------------------------------------
def _web(monkeypatch, fake):
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    _csrf(web)
    return web


def _csrf(web):
    reset_csrf(web)
    web.cookies.set("csrf_token", TOKEN)


def _confirm_form(web, text="Hello, a message for the buyer about the tracking."):
    """Review the text (POST preview, 303, then the check page); the confirm form carries token, action and fingerprint."""
    html = web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "message", "text": text}).text
    _csrf(web)
    sha = re.search(r'data-state="request-sha256">([0-9a-f]{64})<', html).group(1)
    token = re.search(r'name="token" value="([^"]*)"', html).group(1)
    return {"csrf_token": TOKEN, "action": "message", "token": token, "approval_sha256": sha}, html


def _sent_url(row_id):
    return f"{PAGE}/sent/{row_id}#sent"


def test_register_redirects_303_to_the_sent_page_and_a_re_preview_of_the_same_text_offers_no_send_button(monkeypatch):
    """The page after the redirect is the sent page, and re-previewing the same text after it must not offer a send
    control. The assertion fails when the already-sent handling is removed (the check page would offer Send again)."""
    fake = FakePayPal(settling_reads=0)
    web = _web(monkeypatch, fake)
    text = "Same words twice should not send twice, written for this test."
    form, _ = _confirm_form(web, text)
    r = web.post(PAGE + "/register", data=form, follow_redirects=False)
    row = store.list_actions(DISPUTE_ID)[0]
    assert r.status_code == 303 and r.headers["location"] == _sent_url(row["id"])
    _csrf(web)
    page = web.get(PAGE + f"/sent/{row['id']}")
    assert page.status_code == 200 and 'data-state="confirm-form"' not in page.text and "Send message to buyer" not in page.text
    _reopen(fake)
    _csrf(web)
    again = web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "message", "text": text})
    assert again.status_code == 200 and 'data-state="already-sent"' in again.text
    assert 'data-state="confirm-form"' not in again.text and "Send message to buyer" not in again.text
    assert len(_posts(fake)) == 1


def test_a_repeat_submit_after_the_redirect_is_blocked_and_goes_to_the_sent_page_of_the_stored_row(monkeypatch):
    fake = FakePayPal(settling_reads=0)
    web = _web(monkeypatch, fake)
    form, _ = _confirm_form(web)
    first = web.post(PAGE + "/register", data=form, follow_redirects=False)
    assert first.status_code == 303
    _csrf(web)
    web.get(first.headers["location"].split("#")[0])        # settles the pending row: PayPal lists the message
    _reopen(fake)         # PayPal would offer the link again
    _csrf(web)
    calls_before = len(fake.calls)
    again = web.post(PAGE + "/register", data=form, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == first.headers["location"]   # post/redirect/get for the blocked path too
    assert len(fake.calls) == calls_before and len(_posts(fake)) == 1
    page = web.get(again.headers["location"].split("#")[0])
    assert page.status_code == 200 and "Message sent to the buyer" in page.text
    assert 'data-state="confirm-form"' not in page.text and "Send message to buyer" not in page.text
    assert len(_posts(fake)) == 1


def test_the_check_page_of_an_already_sent_text_shows_the_facts_of_the_stored_row_and_no_send_control(monkeypatch):
    """Only the Already sent notice (no 'exactly as Proofie will send it' header), the stored row's own
    state label and its formatted time, no send control."""
    from copilot.web import status, timefmt
    fake = FakePayPal(settling_reads=0)
    web = _web(monkeypatch, fake)
    text = "Same words twice should not send twice, written for this test."
    form, _ = _confirm_form(web, text)
    web.post(PAGE + "/register", data=form, follow_redirects=False)
    _csrf(web)
    web.get(PAGE)
    _reopen(fake)
    _csrf(web)
    html = web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "message", "text": text}).text
    row = store.find_sent(DISPUTE_ID, "message", form["approval_sha256"])
    section = re.search(r'<section[^>]*data-state="already-sent".*?</section>', html, re.S).group(0)
    confirmation = re.search(r'<p data-state="already-sent-confirmation">(.*?)</p>', section, re.S).group(1)
    assert f"PayPal's confirmation state: {status.message_status(row).label}." in " ".join(confirmation.split())
    assert timefmt.display(row["created_at"]) in re.sub(r"<[^>]+>", "", section)
    assert "This exact message was already sent on" in section
    assert "Not sent yet" not in html and "Nothing has been sent" not in html
    assert 'data-state="confirm-form"' not in html and "Send message to buyer" not in html
    assert 'data-state="preview"' not in html


def test_the_page_after_a_different_text_still_offers_to_send_it(monkeypatch):
    fake = FakePayPal(settling_reads=0)
    web = _web(monkeypatch, fake)
    form, _ = _confirm_form(web, "The first text for this dispute with the buyer.")
    web.post(PAGE + "/register", data=form, follow_redirects=False)
    _csrf(web)
    web.get(PAGE)
    _reopen(fake)
    _csrf(web)
    other, html = _confirm_form(web, "A second, different text for the buyer here.")
    assert 'data-state="confirm-form"' in html and other["approval_sha256"] != form["approval_sha256"]


@pytest.mark.parametrize("status_code", [500, 503, 429])
def test_5xx_and_429_are_unknown_and_block_a_repeat(status_code):
    """Only a 4xx other than 429 is a definite refusal. Anything else leaves the outcome unknown."""
    fake = FakePayPal(post_raises=paypal_error(status_code, "SERVER_ERROR"))
    m = _message(fake)
    first = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert first.status == "sent_unconfirmed"
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"
    assert write_guard.lookup(DISPUTE_ID, "message", m.sha256)["state"] == write_guard.UNKNOWN
    calls_before = len(fake.calls)
    again = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert again.status == "already_sent" and len(fake.calls) == calls_before and len(_posts(fake)) == 1
