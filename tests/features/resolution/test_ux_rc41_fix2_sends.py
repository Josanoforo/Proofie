"""A spent check never reaches service.register,
legacy "failed" rows that PayPal may have received read as not confirmed, the history link of a failed row, the test gaps
for supersede_checks_for_request and the fulfilment release age, and the released sent page without a Check button.
Offline: the fake PayPal of resolution_fakes and a temp database. Behaviour items were written red first."""
import sqlite3
import time

import httpx
import pytest
from _dom import parse

from copilot.core import write_guard
from copilot.features.resolution import actions as act
from copilot.features.resolution import routes, service, store
from resolution_fakes import DISPUTE_ID, FakePayPal, paypal_error
from test_ux_rc41_fix_sends import (BODY, SHA, _age, _fields_and_page, _failed_message, _post, _get, _released,
                                    _unknown_message, _web)
from test_ux_rc41_resolution import BUYER_MESSAGE, PAGE


# ==== a spent check never reaches service register even when a release lands ===
def test_a_spent_check_never_reaches_service_register_even_when_a_release_lands_between_the_two_reads(monkeypatch):
    """The window: the route reads "still blocked", a Check PayPal again in a second tab releases the key, and
    the spent token then reaches register. A spent check must be answered from the record, with no call to register."""
    web, fake, fields, row = _unknown_message(monkeypatch)
    fake.messages = [BUYER_MESSAGE]                                  # a normal read that does not list the message
    _age(row["id"], 11 * 60 + 10)
    real_view = routes.already_sent_view
    interleaved = []

    def view_then_release(dispute_id, action, sha):
        seen = real_view(dispute_id, action, sha)
        if seen is not None and not interleaved:                    # the second tab's check lands right after the read
            interleaved.append(service.check_rows(fake, DISPUTE_ID, row["id"]))
        return seen
    registered = []
    real_register = service.register
    monkeypatch.setattr(routes, "already_sent_view", view_then_release)
    monkeypatch.setattr(service, "register", lambda *a, **k: registered.append(a) or real_register(*a, **k))
    posts_before = len(fake.posts())
    again = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert interleaved and interleaved[0].released_row_ids == (row["id"],)      # the release did land in the window
    assert not registered, "a spent check reached service.register"
    assert len(fake.posts()) == posts_before                         # PayPal got nothing
    assert again.status_code in (303, 409)
    if again.status_code == 303:
        assert again.headers["location"].startswith((f"{PAGE}/sent/{row['id']}", f"{PAGE}?already=message"))
    assert len(store.list_actions(DISPUTE_ID)) == 1


def test_a_spent_check_re_posted_while_the_send_is_blocked_lands_on_the_stored_row_without_register(monkeypatch):
    web, fake = _web(monkeypatch)
    fields, _page = _fields_and_page(web)
    assert _post(web, PAGE + "/register", fields, follow_redirects=False).status_code == 303
    registered = []
    monkeypatch.setattr(service, "register", lambda *a, **k: registered.append(a) or pytest.fail("register called"))
    again = _post(web, PAGE + "/register", fields, follow_redirects=False)
    row = store.list_actions(DISPUTE_ID)[0]
    assert again.status_code == 303 and again.headers["location"] == f"{PAGE}/sent/{row['id']}#sent"
    assert not registered and len(fake.posts()) == 1


def test_a_spent_check_whose_send_has_only_a_reservation_goes_to_the_hub_notice(monkeypatch):
    web, fake = _web(monkeypatch)
    fields, _page = _fields_and_page(web)
    store.spend_check(fields["token"], DISPUTE_ID)
    assert write_guard.reserve(DISPUTE_ID, "message", SHA).reserved          # the end of that send was never recorded
    monkeypatch.setattr(service, "register", lambda *a, **k: pytest.fail("register called"))
    again = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"].startswith(f"{PAGE}?already=message&sha={SHA}")
    assert not fake.posts()


# ==== legacy failed =========================================
def _legacy_failed(status_text, action="message"):
    """A row the earlier release stored as "failed" for any HTTP status (and for a transport failure), with its limitation text."""
    body = BODY if action == "message" else act.offer_body(1000, "USD", "Partial refund while we check the shipment", max_cents=4000)
    sha = act.request_sha256(body)
    return store.save_action(DISPUTE_ID, action, body, sha, sha, None, False, "failed", status_text), body, sha


@pytest.mark.parametrize("text", ["PayPal could not send the message (error 503).",
                                  "PayPal could not send the message (error 429).",
                                  "PayPal did not answer in time when trying to send the message."])
def test_a_legacy_failed_row_that_paypal_may_have_received_reads_as_not_confirmed(monkeypatch, text):
    web, fake = _web(monkeypatch)
    saved, body, sha = _legacy_failed(text)
    row = store.get_action(DISPUTE_ID, saved["id"])
    assert row["status"] == "sent_unconfirmed"                                    # the same rule as fulfilment's legacy unknown
    page = _get(web, f"{PAGE}/sent/{saved['id']}").text
    assert "PayPal refused it" not in page and "nothing was sent" not in page.lower()
    assert "Check PayPal again" in page and "Sent from Proofie at" in page and "Send result" not in page
    assert store.find_sent(DISPUTE_ID, "message", sha) is not None                 # a repeat is blocked
    blocked = service.register(fake, DISPUTE_ID, "message", body, sha)
    assert blocked.status == "already_sent" and not fake.posts()
    hub = _get(web, PAGE).text
    assert 'data-status="sent_unconfirmed"' in hub and "Send attempted - not confirmed by PayPal" in hub


def test_a_legacy_failed_row_with_a_real_refusal_keeps_the_refusal_wording(monkeypatch):
    web, fake = _web(monkeypatch)
    saved, body, sha = _legacy_failed("PayPal could not send the message (error 400).")
    assert store.get_action(DISPUTE_ID, saved["id"])["status"] == "failed"
    page = _get(web, f"{PAGE}/sent/{saved['id']}").text
    assert "PayPal refused it" in page and "Check PayPal again" not in page
    assert store.find_sent(DISPUTE_ID, "message", sha) is None


def test_checking_a_legacy_failed_503_row_reads_paypal_and_confirms_only_from_its_answer(monkeypatch):
    web, fake = _web(monkeypatch)
    saved, body, sha = _legacy_failed("PayPal could not send the message (error 503).")
    fake.messages = [{"posted_by": "SELLER", "content": BODY["message"], "time_posted": "2999-01-01T00:00:00.000Z"}]
    done = _post(web, PAGE + "/check", {"row_id": str(saved["id"])}, follow_redirects=False)
    assert done.status_code == 303 and not fake.posts()
    row = store.get_action(DISPUTE_ID, saved["id"])
    assert row["status"] == "sent_confirmed" and store.readback_of(row)["message_listed"] is True


def test_a_legacy_failed_503_offer_row_blocks_a_repeat_and_keeps_the_check_button(monkeypatch):
    web, fake = _web(monkeypatch)
    saved, body, sha = _legacy_failed("PayPal could not send the offer (error 503).", action="offer")
    page = _get(web, f"{PAGE}/sent/{saved['id']}").text
    assert "PayPal refused it" not in page and "Check PayPal again" in page
    assert service.register(fake, DISPUTE_ID, "offer", body, sha).status == "already_sent" and not fake.posts()


# ==== the history link of a failed row says what proofie tried ===
def test_the_history_link_of_a_failed_row_says_what_proofie_tried_to_send(monkeypatch):
    web, fake, row = _failed_message(monkeypatch)
    hub = _get(web, PAGE).text
    links = [a.text().strip() for a in parse(hub).find_all("a") if a.attrs.get("href", "").endswith(f"/sent/{row['id']}#sent")]
    assert "See what Proofie tried to send" in links and "See what you sent" not in links


def test_the_history_link_of_a_refused_422_row_says_what_proofie_tried_to_send(monkeypatch):
    fake = FakePayPal(post_raises=paypal_error(422))
    web, _ = _web(monkeypatch, fake)
    fields, _page = _fields_and_page(web)
    _post(web, PAGE + "/register", fields, follow_redirects=False)
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "refused_422"
    hub = _get(web, PAGE).text
    texts = [a.text().strip() for a in parse(hub).find_all("a") if a.attrs.get("href", "").endswith(f"/sent/{row['id']}#sent")]
    assert "See what Proofie tried to send" in texts


def test_the_history_link_of_a_sent_row_keeps_see_what_you_sent(monkeypatch):
    web, fake, fields, row = _unknown_message(monkeypatch)
    hub = _get(web, PAGE).text
    texts = [a.text().strip() for a in parse(hub).find_all("a") if a.attrs.get("href", "").endswith(f"/sent/{row['id']}#sent")]
    assert "See what you sent" in texts and "See what Proofie tried to send" not in texts


# ==== an unspent check page prepared before a release is superseded ===
def test_an_unspent_check_page_prepared_before_a_release_is_superseded_by_the_release_alone(monkeypatch):
    """Only supersede_checks_for_request can spend this token: it was never used for a send, so spend_check never touched
    it. Turning that function off must fail this test (the mutation check)."""
    web, fake = _web(monkeypatch)
    fake.post_raises = httpx.ConnectError("boom")
    first, _ = _fields_and_page(web)
    second, _ = _fields_and_page(web)                                   # a second check page of the same text, never sent
    assert first["token"] != second["token"]
    assert _post(web, PAGE + "/register", first, follow_redirects=False).status_code == 303
    row = store.list_actions(DISPUTE_ID)[0]
    fake.post_raises = None
    fake.messages = [BUYER_MESSAGE]
    _age(row["id"], 11 * 60 + 10)
    assert service.check_rows(fake, DISPUTE_ID, row["id"]).released_row_ids == (row["id"],)
    assert write_guard.lookup(DISPUTE_ID, "message", SHA)["state"] == write_guard.REFUSED
    posts = len(fake.posts())
    stale = _post(web, PAGE + "/register", second, follow_redirects=False)           # the unused page of before the release
    assert stale.status_code == 409 and len(fake.posts()) == posts
    assert 'data-state="confirm-form"' not in stale.text
    assert store.get_check(second["token"], DISPUTE_ID) is None
    assert store.get_check(second["token"], DISPUTE_ID, include_spent=True)["spent"] is True


# ==== the sent page of a released row has no check button ===
def test_the_sent_page_of_a_released_row_has_no_check_button(monkeypatch):
    web, fake, fields, row = _released(monkeypatch)
    page = _get(web, f"{PAGE}/sent/{row['id']}").text
    assert "PayPal does not list this message" in page and "Prepare it again" in page
    assert "Check PayPal again" not in page and "Reads the dispute from PayPal" not in page


def test_the_sent_page_of_an_absent_but_not_yet_released_row_keeps_the_check_button(monkeypatch):
    web, fake, fields, row = _unknown_message(monkeypatch)
    fake.messages = [BUYER_MESSAGE]
    service.check_rows(fake, DISPUTE_ID, row["id"])                     # absent, but younger than the release floor
    stored = store.get_action(DISPUTE_ID, row["id"])
    assert store.readback_of(stored).get("message_absent") and not store.readback_of(stored).get("released")
    page = _get(web, f"{PAGE}/sent/{row['id']}").text
    assert "Check PayPal again" in page and "Prepare it again" not in page


# ======================================================================================== integration
RECEIVED_NOTE = "Shown exactly as PayPal received it. Your browser will not translate this part."


def _history_note(hub, row_id):
    row = next(tr for tr in parse(hub).find_all("tr") if any(
        a.attrs.get("href", "").endswith(f"/sent/{row_id}#sent") for a in tr.find_all("a")))
    return row.find("p", class_="exact-note").text()


def test_integration_the_sent_page_and_history_of_a_message_paypal_took_say_received(monkeypatch):
    """Integration: a message PayPal accepted and lists is shown with exact_content(sent=true) on its sent page and in
    the hub's history row."""
    web, fake = _web(monkeypatch)
    _post(web, PAGE + "/register", _fields_and_page(web)[0], follow_redirects=False)
    _get(web, PAGE)                                                      # settles the row
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_confirmed"
    record = parse(_get(web, f"{PAGE}/sent/{row['id']}").text).find(None, data_state="sent-record")
    assert record.find("p", class_="exact-note").text() == RECEIVED_NOTE
    assert _history_note(_get(web, PAGE).text, row["id"]) == RECEIVED_NOTE


def test_integration_an_unconfirmed_message_never_says_paypal_received_it(monkeypatch):
    """An unconfirmed send may not be in PayPal: its note does not claim PayPal received it."""
    web, fake, fields, row = _unknown_message(monkeypatch)
    assert "as PayPal received it" not in _get(web, f"{PAGE}/sent/{row['id']}").text
    assert _history_note(_get(web, PAGE).text, row["id"]) != RECEIVED_NOTE


def test_integration_a_refused_message_never_says_paypal_received_it(monkeypatch):
    """A refused send was not sent: its note does not claim PayPal received it."""
    web, fake, row = _failed_message(monkeypatch)
    assert "as PayPal received it" not in _get(web, f"{PAGE}/sent/{row['id']}").text
    assert _history_note(_get(web, PAGE).text, row["id"]) != RECEIVED_NOTE
