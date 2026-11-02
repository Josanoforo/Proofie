"""rc4.3 W1-1b: a buyer message that PayPal already lists with the same text is refused before the POST (lane RX).

EXPECTED (written before the first run; the first four tests are red on the code before the change):
  already listed : register("message") on a fresh read that lists a SELLER message with the same stripped text returns
                   status "already_listed", makes no POST, saves no action row and writes no reservation; the reason says
                   PayPal already lists a message from the seller with this exact text.
  whitespace     : the comparison strips the text, as message_evidence does.
  route          : the register route answers 409 with "Nothing was sent. PayPal already lists a message from you with this
                   exact text." and the check page stays; 0 POSTs.
  guards (green before and after): a BUYER message with the same text does not block; a different SELLER text does not
                   block; an offer is not affected by a listed message; a message nobody listed is sent once, as before.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _hosted import authenticated_client, reset_csrf

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from copilot.app import create_app
from copilot.features.resolution import service, store
from copilot.web.routes_disputes import get_client
from resolution_fakes import DISPUTE_ID, FakePayPal

PAGE = f"/disputes/{DISPUTE_ID}/resolution"
TOKEN = "test-csrf-token"
TEXT = "This message is already in PayPal, written for the listed-message test."


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _listed(fake, text=TEXT, who="SELLER"):
    fake.messages.append({"posted_by": who, "content": text, "time_posted": _stamp()})


def _message(fake, text=TEXT):
    p = service.prepare_message(fake, DISPUTE_ID, text)
    assert p.allowed, p.reason
    return p


def _posts(fake):
    return [c for c in fake.calls if c[0] == "POST"]


def test_a_message_paypal_already_lists_is_refused_without_a_post():
    fake = FakePayPal(settling_reads=0)
    m = _message(fake)
    _listed(fake)
    out = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert out.status == "already_listed"
    assert "already lists a message from the seller with this exact text" in out.reason
    assert _posts(fake) == []
    assert store.list_actions(DISPUTE_ID) == []


def test_the_comparison_strips_the_text():
    fake = FakePayPal(settling_reads=0)
    m = _message(fake)
    _listed(fake, "  " + TEXT + "\n")
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "already_listed"
    assert _posts(fake) == []


def test_a_refusal_for_a_listed_message_leaves_no_reservation_so_a_changed_text_can_go():
    fake = FakePayPal(settling_reads=0)
    m = _message(fake)
    _listed(fake)
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "already_listed"
    other = _message(fake, "A different message, with other words, for the same buyer.")
    assert service.register(fake, DISPUTE_ID, "message", other.body, other.sha256).status == "sent_pending_settle"
    assert len(_posts(fake)) == 1


def _web(monkeypatch, fake):
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    reset_csrf(web)
    web.cookies.set("csrf_token", TOKEN)
    return web


def test_the_register_route_answers_409_in_plain_words_and_posts_nothing(monkeypatch):
    fake = FakePayPal(settling_reads=0)
    web = _web(monkeypatch, fake)
    html = web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "message", "text": TEXT}).text
    reset_csrf(web)
    web.cookies.set("csrf_token", TOKEN)
    sha = re.search(r'data-state="request-sha256">([0-9a-f]{64})<', html).group(1)
    token = re.search(r'name="token" value="([^"]*)"', html).group(1)
    _listed(fake)                                           # PayPal lists the text between the review and the send
    r = web.post(PAGE + "/register", data={"csrf_token": TOKEN, "action": "message", "token": token,
                                           "approval_sha256": sha}, follow_redirects=False)
    assert r.status_code == 409
    assert "Nothing was sent. PayPal already lists a message from you with this exact text." in " ".join(r.text.split())
    assert _posts(fake) == [] and store.list_actions(DISPUTE_ID) == []


# ------------------------------------------------------------------------------------------------- guards
def test_guard_a_buyer_message_with_the_same_text_does_not_block():
    fake = FakePayPal(settling_reads=0)
    m = _message(fake)
    _listed(fake, who="BUYER")
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "sent_pending_settle"
    assert len(_posts(fake)) == 1


def test_guard_a_different_seller_text_does_not_block():
    fake = FakePayPal(settling_reads=0)
    m = _message(fake)
    _listed(fake, "An older message with different words that PayPal lists from the seller.")
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "sent_pending_settle"
    assert len(_posts(fake)) == 1


def test_guard_an_offer_is_not_affected_by_a_listed_message():
    fake = FakePayPal(settling_reads=0)
    p = service.prepare_offer(fake, DISPUTE_ID, 1000, "Partial refund while the shipment is checked")
    assert p.allowed, p.reason
    _listed(fake)
    assert service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256).status == "sent_pending_settle"


def test_guard_an_unlisted_message_is_sent_once():
    fake = FakePayPal(settling_reads=0)
    m = _message(fake)
    assert service.register(fake, DISPUTE_ID, "message", m.body, m.sha256).status == "sent_pending_settle"
    assert len(_posts(fake)) == 1 and len(store.list_actions(DISPUTE_ID)) == 1
