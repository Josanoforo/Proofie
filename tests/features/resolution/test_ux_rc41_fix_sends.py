"""Spent check tokens, the release clock,
honest wording of the sent page, the legacy confirmation label and the test gaps a mutation check found.
Offline: the fake PayPal of resolution_fakes and a temp database. Behaviour items were written red first."""
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

import httpx
import pytest
from _dom import parse

from copilot import config
from copilot.core import write_guard
from copilot.features.resolution import actions as act
from copilot.features.resolution import service, store
from resolution_fakes import DISPUTE_ID, FakePayPal, paypal_error
from test_ux_rc41_resolution import (BUYER_MESSAGE, PAGE, TEXT, _check_url, _confirm, _get, _post, _prepare, _web)

BODY = act.message_body(TEXT)
SHA = act.request_sha256(BODY)


def _fields_and_page(web, text=TEXT, action="message"):
    """Prepare a check page and return (confirm-form fields, html)."""
    page = _get(web, _prepare(web, action, text)).text
    return _confirm(page), page


def _set_send_time(row_id, epoch):
    """Pretend the POST of this row (and its reservation) happened at `epoch` (whole seconds)."""
    stamp = datetime.fromtimestamp(epoch, timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    conn = store._connect()
    try:
        conn.execute("UPDATE resolution_actions SET created_at = ? WHERE id = ?", (stamp, row_id))
        conn.commit()
        sha = conn.execute("SELECT request_sha256 FROM resolution_actions WHERE id = ?", (row_id,)).fetchone()[0]
    finally:
        conn.close()
    raw = sqlite3.connect(str(config.db_path()))
    try:
        raw.execute("UPDATE write_reservations SET created_at = ? WHERE subject_id = ? AND request_sha256 = ?",
                    (float(epoch), DISPUTE_ID, sha))
        raw.commit()
    finally:
        raw.close()


def _age(row_id, seconds):
    _set_send_time(row_id, int(time.time()) - seconds)


def _unknown_message(monkeypatch, **fake_kw):
    """A message whose POST ended unknown (transport failure); returns (web, fake, confirm fields of its check page, row)."""
    fake = FakePayPal(post_raises=httpx.ConnectError("boom"), settling_reads=0, **fake_kw)
    web, _ = _web(monkeypatch, fake)
    fields, _page = _fields_and_page(web)
    sent = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert sent.status_code == 303 and len(fake.posts()) == 1
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_unconfirmed"
    fake.post_raises = None
    return web, fake, fields, row


def _released(monkeypatch):
    web, fake, fields, row = _unknown_message(monkeypatch)
    fake.messages = [BUYER_MESSAGE]                     # a normal read that does not list the message
    _age(row["id"], 11 * 60 + 10)
    _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)
    assert write_guard.lookup(DISPUTE_ID, "message", row["request_sha256"])["state"] == write_guard.REFUSED
    return web, fake, fields, row


# ==== an old check token re posted after a release answers 409 ===
def test_an_old_check_token_re_posted_after_a_release_answers_409_and_posts_nothing(monkeypatch):
    web, fake, fields, row = _released(monkeypatch)
    posts = len(fake.posts())
    again = _post(web, PAGE + "/register", fields, follow_redirects=False)       # the ORIGINAL check page, posted again
    assert again.status_code == 409 and len(fake.posts()) == posts
    assert 'data-state="confirm-form"' not in again.text and len(store.list_actions(DISPUTE_ID)) == 1
    assert write_guard.lookup(DISPUTE_ID, "message", row["request_sha256"])["state"] == write_guard.REFUSED


def test_the_old_check_page_after_a_release_shows_no_send_form(monkeypatch):
    web, fake, fields, row = _released(monkeypatch)
    page = _get(web, f"{PAGE}/check/{fields['token']}")
    assert page.status_code == 409 and 'data-state="confirm-form"' not in page.text
    assert "Send message to buyer" not in page.text and not [p for p in fake.posts()][1:]


def test_a_fresh_check_page_after_the_release_still_sends_once(monkeypatch):
    web, fake, fields, row = _released(monkeypatch)
    new_fields, page = _fields_and_page(web)
    assert new_fields["token"] != fields["token"]
    sent = _post(web, PAGE + "/register", new_fields, follow_redirects=False)
    assert sent.status_code == 303 and len(fake.posts()) == 2


def test_a_token_is_spent_by_a_send_and_a_repeat_click_still_lands_on_the_stored_row(monkeypatch):
    web, fake = _web(monkeypatch)
    fields, _ = _fields_and_page(web)
    first = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert first.status_code == 303
    assert store.get_check(fields["token"], DISPUTE_ID) is None                     # spent
    assert store.get_check(fields["token"], DISPUTE_ID, include_spent=True) is not None
    again = _post(web, PAGE + "/register", fields, follow_redirects=False)
    row = store.list_actions(DISPUTE_ID)[0]
    assert again.status_code == 303 and again.headers["location"] == f"{PAGE}/sent/{row['id']}#sent"
    assert len(fake.posts()) == 1
    shown = _get(web, f"{PAGE}/check/{fields['token']}").text                       # Back: the notice, never a send form
    assert 'data-state="already-sent"' in shown and 'data-state="confirm-form"' not in shown


def test_a_definite_refusal_does_not_spend_the_token_so_the_text_can_be_sent_again(monkeypatch):
    fake = FakePayPal(post_raises=paypal_error(422))
    web, _ = _web(monkeypatch, fake)
    fields, _ = _fields_and_page(web)
    refused = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert refused.status_code == 409 and "Nothing was sent" in refused.text and len(fake.posts()) == 1
    fake.post_raises = None
    sent = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert sent.status_code == 303 and len(fake.posts()) == 2


# ==== checking a released row never releases the key of a newer resend ===
def test_checking_a_released_row_never_releases_the_key_of_a_newer_resend(monkeypatch, tmp_path):
    web, fake, fields, row = _released(monkeypatch)
    fake.lists_messages = False                                  # PayPal's list lags: it does not show message #2 yet
    real_save = store.save_action
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("database write failed after the 2xx")
    monkeypatch.setattr(store, "save_action", boom)
    with pytest.raises(RuntimeError):
        service.register(fake, DISPUTE_ID, "message", BODY, SHA)
    monkeypatch.setattr(store, "save_action", real_save)
    assert len(fake.posts()) == 2 and calls and write_guard.lookup(DISPUTE_ID, "message", SHA)["state"] == write_guard.SENT
    fake.messages = [BUYER_MESSAGE]                              # PayPal does not list message #2 yet
    out = service.check_rows(fake, DISPUTE_ID, row["id"])        # the seller presses Check on the OLD sent page
    assert write_guard.lookup(DISPUTE_ID, "message", SHA)["state"] == write_guard.SENT
    assert out.released_row_ids == ()
    blocked = service.register(fake, DISPUTE_ID, "message", BODY, SHA)
    assert blocked.status == "already_sent" and len(fake.posts()) == 2
    assert store.readback_of(store.get_action(DISPUTE_ID, row["id"])).get("released") is True


def test_the_history_lists_a_resend_without_a_row_as_sending_started(monkeypatch):
    web, fake, fields, row = _released(monkeypatch)
    fake.lists_messages = False
    monkeypatch.setattr(store, "save_action", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("write failed")))
    with pytest.raises(RuntimeError):
        service.register(fake, DISPUTE_ID, "message", BODY, SHA)
    rows = service.history_rows(DISPUTE_ID)
    assert [r["status"] for r in rows] == ["sent_unconfirmed", service.SENDING_STARTED]
    hub = _get(web, PAGE).text
    assert hub.count('data-status="sending_started"') == 1 and "Sending started - not confirmed yet" in hub


def test_the_release_clock_is_the_reservation_not_the_old_row(monkeypatch):
    web, fake, fields, row = _unknown_message(monkeypatch)
    fake.messages = [BUYER_MESSAGE]
    now = int(time.time())
    _set_send_time(row["id"], now - 3 * 3600)                        # the stored row looks old ...
    raw = sqlite3.connect(str(config.db_path()))
    raw.execute("UPDATE write_reservations SET created_at = ?", (float(now - 5 * 60),))   # ... the reservation is 5 minutes old
    raw.commit()
    raw.close()
    out = service.check_rows(fake, DISPUTE_ID, row["id"])
    assert out.released_row_ids == () and write_guard.lookup(DISPUTE_ID, "message", SHA)["state"] == write_guard.UNKNOWN
    assert not store.readback_of(store.get_action(DISPUTE_ID, row["id"])).get("released")


def test_a_reservation_newer_than_the_checked_row_is_never_released(monkeypatch):
    web, fake, fields, row = _unknown_message(monkeypatch)
    fake.messages = [BUYER_MESSAGE]
    now = int(time.time())
    _set_send_time(row["id"], now - 3 * 3600)
    raw = sqlite3.connect(str(config.db_path()))
    raw.execute("UPDATE write_reservations SET created_at = ?", (float(now - 3 * 3600 + 3000),))    # taken long after the row
    raw.commit()
    raw.close()
    out = service.check_rows(fake, DISPUTE_ID, row["id"])
    assert out.released_row_ids == () and write_guard.lookup(DISPUTE_ID, "message", SHA)["state"] == write_guard.UNKNOWN


def test_release_after_absence_is_conditional_on_the_reservation_time():
    assert write_guard.reserve(DISPUTE_ID, "message", SHA).reserved
    write_guard.finish(DISPUTE_ID, "message", SHA, write_guard.SENT)
    created = write_guard.lookup(DISPUTE_ID, "message", SHA)["created_at"]
    assert write_guard.release_after_absence(DISPUTE_ID, "message", SHA, created_at_most=created - 5) is False
    assert write_guard.lookup(DISPUTE_ID, "message", SHA)["state"] == write_guard.SENT
    assert write_guard.release_after_absence(DISPUTE_ID, "message", SHA, created_at_most=created) is True
    assert write_guard.release_after_absence(DISPUTE_ID, "message", SHA) is False        # already released


# ==== unknown offer =========================================
def _unknown_offer(monkeypatch):
    fake = FakePayPal(post_raises=paypal_error(503, "SERVICE_UNAVAILABLE"), settling_reads=0)
    web, _ = _web(monkeypatch, fake)
    fields, _ = _fields_and_page(web, action="offer")
    sent = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert sent.status_code == 303 and len(fake.posts()) == 1
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_unconfirmed" and row["action"] == "offer"
    fake.post_raises = None
    return web, fake, row


def test_an_offer_paypal_still_offers_is_worded_from_paypals_answer(monkeypatch):
    web, fake, row = _unknown_offer(monkeypatch)
    r = _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)       # PayPal answers: make-offer still listed
    page = _get(web, r.headers["location"].split("#")[0]).text
    assert "PayPal is still updating" not in page
    note = parse(page).find("p", **{"data-state": "checked-note"})
    assert note is not None and "still offers the refund option" in note.text() and "does not show your offer" in note.text()
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed" and len(fake.posts()) == 1
    hub = _post(web, PAGE + "/check", {}, follow_redirects=False)
    assert "checked=offered" in hub.headers["location"]


def test_the_settling_note_stays_for_a_dispute_paypal_is_still_updating(monkeypatch):
    web, fake, row = _unknown_offer(monkeypatch)
    fake.pending, fake.left = "after_offer", 99                  # PayPal is updating: UNDER_REVIEW with only the self link
    r = _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)
    assert "checked=settling" in r.headers["location"]


# ==== failed message ========================================
def _failed_message(monkeypatch, issue_status=400):
    fake = FakePayPal(post_raises=paypal_error(issue_status, "INVALID_REQUEST"))
    web, _ = _web(monkeypatch, fake)
    fields, _ = _fields_and_page(web)
    r = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert r.status_code in (409, 502)
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "failed"
    return web, fake, row


def test_the_sent_page_of_a_failed_row_has_its_own_wording_no_check_button_and_a_send_result_crumb(monkeypatch):
    web, fake, row = _failed_message(monkeypatch)
    page = _get(web, f"{PAGE}/sent/{row['id']}").text
    dom = parse(page)
    assert dom.find("h1").text().strip() == "Not sent: PayPal returned an error"
    assert "Check PayPal again" not in page and "Sent from Proofie at" not in page
    assert "the buyer can reply in PayPal" not in page and "Proofie sends nothing else" not in page
    assert "Proofie tried to send this at" in page and "PayPal refused it" in page
    crumbs = [item.text().strip() for item in dom.find("nav", **{"aria-label": "Breadcrumb"}).find_all("li")] \
        if dom.find("nav", **{"aria-label": "Breadcrumb"}) else re.findall(r"<li[^>]*>(.*?)</li>", page, re.S)
    assert "Send result" in " ".join(crumbs) and "Message sent" not in " ".join(crumbs)
    assert len(fake.posts()) == 1


def test_the_sent_page_of_a_refused_422_row_has_the_same_honest_wording(monkeypatch):
    fake = FakePayPal(post_raises=paypal_error(422))
    web, _ = _web(monkeypatch, fake)
    fields, _ = _fields_and_page(web)
    _post(web, PAGE + "/register", fields, follow_redirects=False)
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "refused_422"
    page = _get(web, f"{PAGE}/sent/{row['id']}").text
    assert "Check PayPal again" not in page and "Sent from Proofie at" not in page and "PayPal refused it" in page
    assert "Send result" in page


def test_a_sent_row_keeps_the_message_sent_crumb_and_the_check_button_when_unconfirmed(monkeypatch):
    web, fake, fields, row = _unknown_message(monkeypatch)
    page = _get(web, f"{PAGE}/sent/{row['id']}").text
    assert "Check PayPal again" in page and "Sent from Proofie at" in page and "Send result" not in page


def test_the_hub_words_a_released_message_as_not_listed(monkeypatch):
    web, fake, fields, row = _released(monkeypatch)
    hub = _get(web, PAGE).text
    note = parse(hub).find("p", **{"data-state": "message-sent-note"})
    assert note is not None and "PayPal does not list it" in note.text()


# ==== legacy confirmed message ==============================
def _legacy_confirmed_message():
    saved = store.save_action(DISPUTE_ID, "message", BODY, SHA, SHA, {"stage": "INQUIRY", "status": "X", "links": [],
                                                                      "outcome": None}, True, "sent_confirmed")
    return saved["id"]


def test_a_legacy_confirmed_message_row_does_not_claim_paypal_lists_it(monkeypatch):
    web, fake = _web(monkeypatch)
    row_id = _legacy_confirmed_message()
    page = _get(web, f"{PAGE}/sent/{row_id}").text
    assert "PayPal lists your message" not in page
    assert page.count("Confirmed by reading the dispute back (message not checked)") >= 1
    hub = _get(web, PAGE).text
    assert "PayPal lists your message" not in hub and "Confirmed by reading the dispute back (message not checked)" in hub


def test_a_message_row_confirmed_with_message_listed_true_keeps_the_label(monkeypatch):
    web, fake = _web(monkeypatch)
    r = _post(web, PAGE + "/register", _fields_and_page(web)[0], follow_redirects=False)
    web.get(PAGE)                                                       # settles the row
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_confirmed" and store.readback_of(row)["message_listed"] is True
    page = _get(web, f"{PAGE}/sent/{row['id']}").text
    assert "PayPal lists your message" in page and "message not checked" not in page


# ==== a seller message with other text after the send does not confirm ===
def test_a_seller_message_with_other_text_after_the_send_does_not_confirm(monkeypatch):
    fake = FakePayPal(post_raises=httpx.ConnectError("boom"), lists_messages=False)
    out = service.register(fake, DISPUTE_ID, "message", BODY, SHA)
    assert out.status == "sent_unconfirmed"
    fake.post_raises = None
    fake.messages = [{"posted_by": "SELLER", "content": TEXT + " (a different text)",
                      "time_posted": "2999-01-01T00:00:00.000Z"}]
    result = service.check_rows(fake, DISPUTE_ID)
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_unconfirmed" and result.confirmed == 0 and result.absent == 1
    assert store.readback_of(row)["message_listed"] is False
    service.settle_pending(fake, DISPUTE_ID)
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"


def test_check_rows_while_paypal_is_settling_leaves_offer_and_message_rows_unchanged(monkeypatch):
    fake = FakePayPal(post_raises=paypal_error(503, "SERVICE_UNAVAILABLE"))
    offer = act.offer_body(1000, "USD", "Partial refund while we check the shipment", max_cents=4000)
    assert service.register(fake, DISPUTE_ID, "offer", offer, act.request_sha256(offer)).status == "sent_unconfirmed"
    assert service.register(fake, DISPUTE_ID, "message", BODY, SHA).status == "sent_unconfirmed"
    fake.post_raises = None
    _age_all = int(time.time()) - 3 * 3600
    for row in store.list_actions(DISPUTE_ID):
        _set_send_time(row["id"], _age_all)
    before = [(r["status"], r["readback_json"]) for r in store.list_actions(DISPUTE_ID)]
    fake.pending, fake.left = "after_message", 99                        # UNDER_REVIEW with only the self link
    out = service.check_rows(fake, DISPUTE_ID)
    assert out.checked == 2 and out.unchanged == 2 and out.confirmed == 0 and out.absent == 0 and out.released_row_ids == ()
    assert [(r["status"], r["readback_json"]) for r in store.list_actions(DISPUTE_ID)] == before
    assert write_guard.lookup(DISPUTE_ID, "message", SHA)["state"] == write_guard.UNKNOWN
    assert write_guard.lookup(DISPUTE_ID, "offer", act.request_sha256(offer))["state"] == write_guard.UNKNOWN


# ==== the release floor is ten minutes and the boundary is pinned ===
def test_the_release_floor_is_ten_minutes_and_the_boundary_is_pinned(monkeypatch):
    assert write_guard.release_min_age(80) == 600 and write_guard.release_min_age(0) == 600
    assert write_guard.release_min_age(900) == 900 and service.RELEASE_MIN_AGE_SECONDS == 600
    for seconds, released in ((570, False), (599, False), (600, True)):
        fake = FakePayPal(post_raises=httpx.ConnectError("boom"))
        service.register(fake, DISPUTE_ID, "message", BODY, SHA)
        fake.post_raises = None
        fake.messages = [BUYER_MESSAGE]
        row = store.list_actions(DISPUTE_ID)[-1]
        start = 1_800_000_000
        _set_send_time(row["id"], start)
        out = service.check_rows(fake, DISPUTE_ID, row["id"], now=datetime.fromtimestamp(start + seconds, timezone.utc))
        assert bool(out.released_row_ids) is released, seconds
        state = write_guard.lookup(DISPUTE_ID, "message", SHA)["state"]
        assert state == (write_guard.REFUSED if released else write_guard.UNKNOWN), seconds
        conn = sqlite3.connect(str(config.db_path()))              # a clean slate for the next boundary
        conn.execute("DELETE FROM write_reservations")
        conn.execute("DELETE FROM resolution_actions")
        conn.commit()
        conn.close()


# === polish
def test_the_recorded_link_on_the_check_page_points_to_the_hub_anchor(monkeypatch):
    web, fake = _web(monkeypatch)
    assert write_guard.reserve(DISPUTE_ID, "message", SHA).reserved          # a send of this text started and never ended
    page = _get(web, _prepare(web)).text
    links = [a for a in parse(page).find_all("a") if a.text().strip() == "See what this app recorded"]
    assert len(links) == 1 and links[0].attrs["href"] == f"{PAGE}#recorded"
    assert 'id="recorded"' not in page


def test_the_expired_check_page_makes_back_to_resolution_options_the_primary(monkeypatch):
    web, fake = _web(monkeypatch)
    page = _get(web, f"{PAGE}/check/not-a-real-token")
    back = [a for a in parse(page.text).find_all("a") if a.text().strip() == "Back to resolution options"]
    assert back and "btn-primary" in back[0].attrs["class"] and back[0].attrs["href"] == PAGE
    assert page.status_code == 410


# === follow-up
def _hub_with_due(monkeypatch, due):
    fake = FakePayPal(settling_reads=0)
    original = fake._raw
    fake._raw = lambda status, rels: {**original(status, rels), "seller_response_due_date": due}
    web, _ = _web(monkeypatch, fake)
    return _get(web, PAGE).text


def test_respond_by_is_shown_only_for_a_due_date_with_an_offset(monkeypatch):
    with_offset = _hub_with_due(monkeypatch, "2026-10-14T12:00:00.000Z")
    assert parse(with_offset).find("p", **{"data-state": "respond-by"}) is not None and "Respond by" in with_offset
    naive = _hub_with_due(monkeypatch, "2026-10-14T12:00:00")
    assert 'data-state="respond-by"' not in naive and "Respond by" not in naive and "Not available" not in naive
