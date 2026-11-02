"""Check page, sent page, honest confirmation, refusals that keep the text,
the hub, the history and the follow-ups. Offline: the fake PayPal of resolution_fakes and a temp database.
Behaviour items were written red first."""
import html as htmllib
import json
import re
import time

import httpx
import pytest
from _dom import parse
from _hosted import authenticated_client, reset_csrf, set_test_csrf
import _ux_lint as lint

from copilot import config
from copilot.app import create_app
from copilot.core import write_guard
from copilot.features.resolution import actions as act
from copilot.features.resolution import service, store
from copilot.web.routes_disputes import get_client
from resolution_fakes import BASE, DISPUTE_ID, FakePayPal, paypal_error

FLAG, WRITES = "COPILOT_FEATURE_RESOLUTION", "COPILOT_FEATURE_RESOLUTION_WRITES"
TOKEN = "test-csrf-token"
PAGE = f"/disputes/{DISPUTE_ID}/resolution"
TEXT = "Synthetic seller message about the shipment, written for this test."
OFFER = {"action": "offer", "amount": "10.00", "note": "Partial refund while we check the shipment"}


def _web(monkeypatch, fake=None, writes="true", **flags):
    for name, value in ((FLAG, "true"), (WRITES, writes)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    for name in ("COPILOT_FEATURE_ACCOUNT_CLIFF", "COPILOT_FEATURE_DELIVERY_FACTS", "COPILOT_FEATURE_FULFILMENT",
                 "COPILOT_FEATURE_DRAFT", "COPILOT_FEATURE_OUTCOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in flags.items():
        monkeypatch.setenv(name, value)
    fake = fake or FakePayPal(settling_reads=0)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, TOKEN)
    return web, fake


def _post(web, path, data=None, **kw):
    set_test_csrf(web, TOKEN)
    return web.post(path, data={"csrf_token": TOKEN, **(data or {})}, **kw)


def _get(web, path, **kw):
    response = web.get(path, **kw)
    set_test_csrf(web, TOKEN)
    return response


def _check_url(response):
    assert response.status_code == 303, response.text[:300]
    return response.headers["location"]


def _prepare(web, action="message", text=TEXT, **extra):
    data = {"action": action, **({"text": text} if action == "message" else {k: v for k, v in OFFER.items() if k != "action"})}
    return _check_url(_post(web, PAGE + "/preview", {**data, **extra}, follow_redirects=False))


def _confirm(page_html):
    form = re.search(r'<form[^>]*data-state="confirm-form"[^>]*>(.*?)</form>', page_html, re.S)
    assert form, "no confirm form on this page"
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(1)))
    fields.pop("csrf_token", None)
    return {k: htmllib.unescape(v) for k, v in fields.items()}


def _send(web, message=True, text=TEXT):
    """Preview and register; returns (fake-visible register response, check page html)."""
    url = _prepare(web, "message" if message else "offer", text)
    page = _get(web, url).text
    return _post(web, PAGE + "/register", _confirm(page), follow_redirects=False), page


def _text(node):
    return " ".join(node.text().split())


# ==== preview post answers 303 to the check page and no text travels ===
def test_preview_post_answers_303_to_the_check_page_and_no_text_travels_in_the_url(monkeypatch):
    web, fake = _web(monkeypatch)
    for action in ("message", "offer"):
        url = _prepare(web, action)
        assert re.fullmatch(rf"/disputes/{DISPUTE_ID}/resolution/check/[A-Za-z0-9_-]{{20,}}#preview", url), url
        assert "Synthetic" not in url and "Partial" not in url and "10.00" not in url
    assert not fake.posts()


def test_the_check_page_for_a_message_has_the_preview_before_one_primary_send_and_no_textarea(monkeypatch):
    web, fake = _web(monkeypatch)
    page = _get(web, _prepare(web)).text
    dom = parse(page)
    assert dom.find("h1").text().strip() == "Review the message before it is sent"
    preview = dom.find("section", id="preview")
    assert preview is not None and preview.attrs.get("data-state") == "preview"
    assert page.index('id="preview"') < page.index('data-state="confirm-form"')
    assert "Not sent yet. This is the exact text PayPal will show the buyer." in page
    assert "To: The buyer, through PayPal" in _text(dom)
    primaries = lint.primaries(page)
    assert [n.text().strip() for n in primaries] == ["Send message to buyer"]
    assert dom.find_all("textarea") == []
    exact = [n for n in dom.walk() if n.attrs.get("translate") == "no" and TEXT in n.text()]
    assert exact, "the message must sit inside a translate=no block"
    assert "Send message to buyer sends it: there is no separate send step." in page
    assert "Proofie will not send it again on its own." in page
    assert dom.find("pre", **{"data-state": "request-body"}) is not None
    assert re.search(r'data-state="request-sha256">[0-9a-f]{64}<', page)
    assert page.index('data-state="request-body"') > page.index("Send message to buyer")        # Technical details after the button
    assert not fake.posts()


def test_the_check_page_for_an_offer_states_the_amount_from_cents(monkeypatch):
    web, fake = _web(monkeypatch)
    page = _get(web, _prepare(web, "offer")).text
    assert parse(page).find("h1").text().strip() == "Review the offer before it is sent"
    assert "Not sent yet. This is the exact offer PayPal will show the buyer." in page
    assert "You offer 10.00 USD (of 40.00 USD disputed)." in page
    assert "Partial refund while we check the shipment" in page
    assert [n.text().strip() for n in lint.primaries(page)] == ["Send offer to PayPal"]
    assert "Change the offer" in page and "Change the message" not in page


def test_the_check_page_form_carries_only_the_token_the_action_and_the_hash(monkeypatch):
    web, fake = _web(monkeypatch)
    page = _get(web, _prepare(web)).text
    form = re.search(r'<form[^>]*data-state="confirm-form"[^>]*>', page).group(0)
    assert "data-sends" in form and f'action="{PAGE}/register"' in form
    fields = _confirm(page)
    assert set(fields) == {"token", "action", "approval_sha256"}
    assert fields["action"] == "message" and re.fullmatch(r"[0-9a-f]{64}", fields["approval_sha256"])
    assert TEXT not in re.search(r'<form[^>]*data-state="confirm-form".*?</form>', page, re.S).group(0)


def test_the_check_page_is_never_cached_and_the_hub_never_holds_a_preview(monkeypatch):
    web, fake = _web(monkeypatch)
    response = _get(web, _prepare(web))
    assert response.headers["cache-control"] == "no-store"
    hub = _get(web, PAGE).text
    assert 'data-state="preview"' not in hub and 'data-state="confirm-form"' not in hub


def test_the_hub_forms_post_to_the_preview_anchor_and_carry_the_nothing_is_sent_note(monkeypatch):
    web, fake = _web(monkeypatch)
    hub = _get(web, PAGE).text
    forms = re.findall(r'<form[^>]*action="([^"]*/resolution/preview[^"]*)"', hub)
    assert forms == [f"{PAGE}/preview#preview"] * 2
    assert hub.count("Nothing is sent yet. You see the exact text on the next page.") == 2


def test_register_refuses_a_token_whose_stored_body_does_not_hash_to_the_approval_and_posts_nothing(monkeypatch):
    web, fake = _web(monkeypatch)
    page = _get(web, _prepare(web)).text
    fields = _confirm(page)
    r = _post(web, PAGE + "/register", {**fields, "approval_sha256": "0" * 64})
    assert r.status_code == 409 and not fake.posts() and store.list_actions(DISPUTE_ID) == []
    assert TEXT in r.text and "Nothing was sent" in r.text            # the stored record is shown again, never the submitted body


def test_register_ignores_a_body_in_the_request_and_sends_the_stored_one(monkeypatch):
    web, fake = _web(monkeypatch)
    fields = _confirm(_get(web, _prepare(web)).text)
    r = _post(web, PAGE + "/register", {**fields, "body_json": json.dumps({"message": "Injected text"})}, follow_redirects=False)
    assert r.status_code == 303
    assert store.list_actions(DISPUTE_ID)[0]["request_json"] == json.dumps({"message": TEXT}, sort_keys=True, ensure_ascii=False)


def test_an_expired_or_foreign_token_says_so_and_offers_the_way_back(monkeypatch):
    web, fake = _web(monkeypatch)
    token = store.save_check(DISPUTE_ID, "message", act.message_body(TEXT), act.request_sha256(act.message_body(TEXT)),
                             "seller", now=time.time() - 61 * 60)
    for url in (f"{PAGE}/check/{token}", f"{PAGE}/check/not-a-real-token", f"/disputes/PP-R-TST-0000002/resolution/check/{token}"):
        r = _get(web, url)
        dom = parse(r.text)
        assert "This check has expired. Nothing was sent." in r.text, url
        back = [a for a in dom.find_all("a") if a.text().strip() == "Back to resolution options"]
        assert back and "btn-primary" in back[0].attrs["class"] and back[0].attrs["href"] == url.split("/resolution/")[0] + "/resolution"
        assert 'data-state="confirm-form"' not in r.text
    expired_register = _post(web, PAGE + "/register", {"token": token, "action": "message", "approval_sha256": "0" * 64})
    assert "This check has expired. Nothing was sent." in expired_register.text and not fake.posts()


def test_a_token_is_bound_to_its_action(monkeypatch):
    web, fake = _web(monkeypatch)
    fields = _confirm(_get(web, _prepare(web)).text)
    r = _post(web, PAGE + "/register", {**fields, "action": "offer"})
    assert r.status_code in (409, 410) and not fake.posts()


def test_the_draft_handoff_post_also_gets_the_303(monkeypatch):
    web, fake = _web(monkeypatch)
    r = _post(web, PAGE + "/preview", {"action": "message", "text": TEXT}, follow_redirects=False)
    assert r.status_code == 303 and "/resolution/check/" in r.headers["location"]


# ==== fresh hub one primary review the offer the message review is secondary ===
def test_fresh_hub_one_primary_review_the_offer_the_message_review_is_secondary_large(monkeypatch):
    web, fake = _web(monkeypatch)
    hub = _get(web, PAGE).text
    dom = parse(hub)
    assert [n.text().strip() for n in lint.primaries(hub)] == ["Review the offer"]
    message = [n for n in dom.find_all("button") if n.text().strip() == "Review the message"][0]
    assert {"btn-secondary", "btn-large"} <= set(message.attrs["class"].split())
    defend = dom.find("a", **{"data-state": "defend-link"})
    assert defend.text().strip() == "Defend with your evidence file" and {"btn-secondary", "btn-large"} <= set(defend.attrs["class"].split())


def test_hub_without_the_offer_link_makes_the_message_review_the_primary(monkeypatch):
    fake = FakePayPal(phase="after_offer", status="WAITING_FOR_BUYER_RESPONSE")
    web, _ = _web(monkeypatch, fake)
    hub = _get(web, PAGE).text
    assert [n.text().strip() for n in lint.primaries(hub)] == ["Review the message"]
    assert "Review the offer" not in hub


# ==== register success redirects to the sent page of the stored row ===
def test_register_success_redirects_to_the_sent_page_of_the_stored_row(monkeypatch):
    web, fake = _web(monkeypatch)
    r, _ = _send(web)
    row = store.list_actions(DISPUTE_ID)[0]
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}/sent/{row['id']}#sent"
    assert len(fake.posts()) == 1


def test_the_sent_page_shows_the_status_headline_the_record_and_one_primary(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=99))
    r, _ = _send(web)
    page = _get(web, r.headers["location"].split("#")[0]).text
    dom = parse(page)
    assert dom.find("h1").text().strip() == "PayPal answered OK; not listed yet"
    assert "What you sent (Proofie's record)" in page
    assert any(n.attrs.get("translate") == "no" and TEXT in n.text() for n in dom.walk())
    assert [n.text().strip() for n in lint.primaries(page)] == [f"Return to dispute {DISPUTE_ID}"]
    assert not re.search(r'<form[^>]*action="[^"]*/register"', page)
    assert "What happens next: the buyer can reply in PayPal. Proofie sends nothing else." in page
    assert "Check PayPal again" in page                                    # not confirmed: the read-only check is offered
    assert fake.posts() and len(fake.posts()) == 1


def test_the_sent_page_for_a_confirmed_message_and_a_confirmed_offer(monkeypatch):
    web, fake = _web(monkeypatch)
    r, _ = _send(web)
    web.get(PAGE)                                                           # settles the pending row
    page = _get(web, r.headers["location"].split("#")[0]).text
    assert parse(page).find("h1").text().strip() == "Message sent to the buyer"
    assert "PayPal lists your message (read at" in page and "Check PayPal again" not in page
    web2, fake2 = _web(monkeypatch)
    r2, _ = _send(web2, message=False)
    web2.get(PAGE)
    page2 = _get(web2, r2.headers["location"].split("#")[0]).text
    assert parse(page2).find("h1").text().strip() == "Refund offer sent to PayPal"
    assert "PayPal no longer shows the refund-offer option for this dispute (read at" in page2


def test_the_sent_page_of_an_unconfirmed_send(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(post_raises=httpx.ConnectError("boom")))
    r, _ = _send(web)
    page = _get(web, r.headers["location"].split("#")[0]).text
    assert parse(page).find("h1").text().strip() == "Send attempted - not confirmed by PayPal"
    assert "Proofie will not send it again on its own." in page
    assert len(fake.posts()) == 1


def test_a_sent_page_of_another_dispute_or_a_missing_row_is_404(monkeypatch):
    web, fake = _web(monkeypatch)
    r, _ = _send(web)
    row_id = store.list_actions(DISPUTE_ID)[0]["id"]
    assert _get(web, f"/disputes/PP-R-TST-0000002/resolution/sent/{row_id}").status_code == 404
    assert _get(web, f"{PAGE}/sent/9999").status_code == 404


def test_a_repeat_register_goes_to_the_sent_page_of_the_stored_row(monkeypatch):
    web, fake = _web(monkeypatch)
    r, page = _send(web)
    again = _post(web, PAGE + "/register", _confirm(page), follow_redirects=False)
    row = store.list_actions(DISPUTE_ID)[0]
    assert again.status_code == 303 and again.headers["location"] == f"{PAGE}/sent/{row['id']}#sent"
    assert len(fake.posts()) == 1


def test_the_hub_after_a_message_says_so_and_warns_about_a_second_message(monkeypatch):
    web, fake = _web(monkeypatch)
    _send(web)
    web.get(PAGE)
    hub = _get(web, PAGE).text
    assert "You sent a message at" in hub and "See what you sent." in hub
    assert "If you write again, the buyer receives a second message." in hub
    assert 'data-state="outcome"' not in hub


# ==== a fake PayPal holding given messages ==============================================
def _raw_with(messages, *, status="WAITING_FOR_SELLER_RESPONSE"):
    fake = FakePayPal(settling_reads=0, lists_messages=False)
    fake.messages = messages
    return fake


def _register_message(fake, text=TEXT):
    body = act.message_body(text)
    out = service.register(fake, DISPUTE_ID, "message", body, act.request_sha256(body))
    assert out.status == "sent_pending_settle"
    return body


def test_a_message_is_not_confirmed_when_paypal_does_not_list_it():
    fake = FakePayPal(settling_reads=0, lists_messages=False)
    _register_message(fake)
    assert service.settle_pending(fake, DISPUTE_ID) == 1
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_unconfirmed"
    assert json.loads(row["readback_json"])["message_listed"] is False


def test_a_message_is_confirmed_when_one_seller_message_equals_the_body():
    fake = FakePayPal(settling_reads=0)
    _register_message(fake, "  " + TEXT + "  ")
    service.settle_pending(fake, DISPUTE_ID)
    row = store.list_actions(DISPUTE_ID)[0]
    readback = json.loads(row["readback_json"])
    assert row["status"] == "sent_confirmed" and readback["message_count"] == 1 and readback["message_listed"] is True


def test_a_buyer_message_with_the_same_text_does_not_confirm():
    fake = FakePayPal(settling_reads=0, lists_messages=False)
    _register_message(fake)
    fake.messages = [{"posted_by": "BUYER", "content": TEXT, "time_posted": "2999-01-01T00:00:00.000Z"}]
    service.settle_pending(fake, DISPUTE_ID)
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"


def test_two_equal_seller_messages_confirm_and_the_count_is_kept_and_said(monkeypatch):
    web, fake = _web(monkeypatch)
    r, _ = _send(web)
    fake.messages.append(dict(fake.messages[0]))                           # PayPal shows it twice
    page = _get(web, PAGE).text                                             # the hub GET settles the pending row
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_confirmed" and json.loads(row["readback_json"])["message_count"] == 2
    sent = _get(web, f"{PAGE}/sent/{row['id']}").text
    assert "PayPal shows 2 messages from you with this text." in sent


def test_the_readback_never_stores_message_contents():
    fake = FakePayPal(settling_reads=0)
    _register_message(fake)
    service.settle_pending(fake, DISPUTE_ID)
    row = store.list_actions(DISPUTE_ID)[0]
    assert TEXT not in row["readback_json"] and set(json.loads(row["readback_json"])) >= {"message_listed", "message_count"}
    assert all(TEXT not in str(v) for v in json.loads(row["readback_json"]).values())


def test_a_transport_failed_message_never_becomes_confirmed_because_paypal_answered_a_read():
    fake = FakePayPal(post_raises=httpx.ConnectError("boom"), lists_messages=False)
    body = act.message_body(TEXT)
    assert service.register(fake, DISPUTE_ID, "message", body, act.request_sha256(body)).status == "sent_unconfirmed"
    assert service.settle_pending(fake, DISPUTE_ID) == 0
    out = service.check_rows(fake, DISPUTE_ID)
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed" and out.absent == 1
    assert len(fake.posts()) == 1


def test_an_older_seller_message_with_the_same_text_is_not_evidence_for_this_send():
    fake = FakePayPal(settling_reads=0, lists_messages=False)
    _register_message(fake)
    fake.messages = [{"posted_by": "SELLER", "content": TEXT, "time_posted": "2001-01-01T00:00:00.000Z"}]
    service.settle_pending(fake, DISPUTE_ID)
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"


def test_offers_keep_the_link_gone_rule():
    fake = FakePayPal(settling_reads=0)
    body = act.offer_body(1000, "USD", "Partial refund while we check the shipment", max_cents=4000)
    service.register(fake, DISPUTE_ID, "offer", body, act.request_sha256(body))
    service.settle_pending(fake, DISPUTE_ID)
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_confirmed"

# ======================================================================================== the shared page shell on the resolution pages
def _templates():
    from pathlib import Path
    return sorted((Path(__file__).resolve().parents[3] / "copilot" / "web" / "templates" / "features" / "resolution").glob("*.html"))


def test_the_resolution_templates_pass_the_shell_lint():
    assert lint.template_lint(_templates()) == []


def test_crumbs_no_back_links_no_maxlength_novalidate_and_titles(monkeypatch):
    web, fake = _web(monkeypatch)
    hub = _get(web, PAGE).text
    check = _get(web, _prepare(web)).text
    r, _ = _send(web)
    sent = _get(web, r.headers["location"].split("#")[0]).text
    crumbs = [("All disputes", "/disputes"), (f"Dispute {DISPUTE_ID}", f"/disputes/{DISPUTE_ID}")]
    assert lint.breadcrumb_items(hub) == crumbs + [("Resolution options and buyer messages", None)]
    assert lint.breadcrumb_items(check)[:3] == crumbs + [("Resolution options and buyer messages", PAGE)]
    assert [label for label, _ in lint.breadcrumb_items(check)][-1] == "Review the message"
    assert [label for label, _ in lint.breadcrumb_items(sent)][-1] == "Message sent"
    for page in (hub, check, sent):
        assert lint.back_links(page) == [] and "maxlength" not in page and lint.bare_buttons(page) == []
    assert "Back to the dispute" not in hub and "Review the current case and its next task" not in hub
    assert len(re.findall(r"<form[^>]*novalidate", hub)) == 2 and "novalidate" in re.search(r"<form[^>]*confirm-form[^>]*>", check).group(0)
    assert "<title>Resolution options and buyer messages: " in hub and f"{DISPUTE_ID} - Proofie</title>" in hub
    assert "<title>Review the message: " in check


def test_a_single_primary_on_every_state(monkeypatch):
    web, fake = _web(monkeypatch)
    lint.assert_single_primary(_get(web, PAGE).text)                                              # fresh
    lint.assert_single_primary(_get(web, _prepare(web)).text)                                     # check page
    bad = _post(web, PAGE + "/preview", {"action": "offer", "amount": "", "note": ""})
    assert bad.status_code == 400
    lint.assert_single_primary(bad.text)                                                          # error state
    r, _ = _send(web)
    lint.assert_single_primary(_get(web, r.headers["location"].split("#")[0]).text)               # sent
    gone, _ = _web(monkeypatch, FakePayPal(phase="after_offer", status="WAITING_FOR_BUYER_RESPONSE"))
    lint.assert_single_primary(_get(gone, PAGE).text)                                             # offer gone
    off, _ = _web(monkeypatch, writes=None)
    lint.assert_single_primary(_get(off, PAGE).text)                                              # writes off: Defend leads
    settling = FakePayPal(settling_reads=99)
    settling.pending, settling.left = "after_offer", 99
    quiet, _ = _web(monkeypatch, settling)
    lint.assert_single_primary(_get(quiet, PAGE).text, allow_zero=True)                           # settling: nothing to press


# ==== a stale reserved row blocks register with zero posts and the history ===
def test_a_stale_reserved_row_blocks_register_with_zero_posts_and_the_history_keeps_the_trace(monkeypatch):
    fake = FakePayPal(settling_reads=0)
    body = act.message_body(TEXT)
    sha = act.request_sha256(body)
    assert write_guard.reserve(DISPUTE_ID, "message", sha).reserved                    # the process died before finish()
    out = service.register(fake, DISPUTE_ID, "message", body, sha)
    assert out.status == "already_sent" and out.existing is None and not fake.posts()
    web, _ = _web(monkeypatch, fake)
    hub = _get(web, PAGE).text
    assert 'data-status="sending_started"' in hub and "Sending started - not confirmed yet" in hub
    assert store.list_actions(DISPUTE_ID) == [] and not fake.posts()


# ==== an unconfirmed send helper ===========================================
def _unconfirmed(monkeypatch, **fake_kw):
    fake = FakePayPal(post_raises=httpx.ConnectError("boom"), **fake_kw)
    web, _ = _web(monkeypatch, fake)
    r, _page = _send(web)
    assert r.status_code == 303 and store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"
    return web, fake, r.headers["location"].split("#")[0]


def test_check_paypal_again_confirms_only_when_paypal_lists_the_message(monkeypatch):
    web, fake, url = _unconfirmed(monkeypatch)
    fake.post_raises = None
    fake.messages = [{"posted_by": "SELLER", "content": TEXT, "time_posted": "2999-01-01T00:00:00.000Z"}]
    row = store.list_actions(DISPUTE_ID)[0]
    r = _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}/sent/{row['id']}#sent"
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_confirmed" and len(fake.posts()) == 1
    assert "Message sent to the buyer" in _get(web, url).text


BUYER_MESSAGE = {"posted_by": "BUYER", "content": "Synthetic buyer message", "time_posted": "2026-10-01T15:00:00.000Z"}


def _age_row(row_id, seconds):
    """Pretend the POST happened `seconds` ago: the stored send time and the send-once reservation (the clock the release
    rule reads) are both moved."""
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    conn = store._connect()
    try:
        conn.execute("UPDATE resolution_actions SET created_at = ? WHERE id = ?", (stamp, row_id))
        conn.commit()
    finally:
        conn.close()
    import sqlite3
    raw = sqlite3.connect(str(config.db_path()))
    try:
        raw.execute("UPDATE write_reservations SET created_at = ? WHERE subject_id = ?", (time.time() - seconds, DISPUTE_ID))
        raw.commit()
    finally:
        raw.close()


YET = "PayPal does not list this message yet. Proofie will not send it again; check again later."


def test_a_too_young_absence_keeps_the_row_blocked_and_says_yet(monkeypatch):
    web, fake, url = _unconfirmed(monkeypatch)
    fake.messages = [BUYER_MESSAGE]                                   # the read is normal: it lists the dispute's messages
    row = store.list_actions(DISPUTE_ID)[0]
    _age_row(row["id"], 5 * 60)                                       # older than nothing, younger than 10 minutes
    posts_before = len(fake.posts())
    r = _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)
    page = _get(web, r.headers["location"].split("#")[0]).text
    assert YET in page and "Prepare it again" not in page and "can prepare it again" not in page
    assert write_guard.lookup(DISPUTE_ID, "message", row["request_sha256"])["state"] == write_guard.UNKNOWN
    again = _get(web, _prepare(web)).text                              # a new check page still shows the block
    assert 'data-state="already-sent"' in again and 'data-state="confirm-form"' not in again
    out = service.register(fake, DISPUTE_ID, "message", act.message_body(TEXT), row["request_sha256"])
    assert out.status == "already_sent" and len(fake.posts()) == posts_before


def test_a_partial_read_never_releases_even_when_the_row_is_old(monkeypatch):
    web, fake, url = _unconfirmed(monkeypatch)                         # no "messages" list in PayPal's answer: not a normal read
    row = store.list_actions(DISPUTE_ID)[0]
    _age_row(row["id"], 3 * 3600)
    r = _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)
    page = _get(web, r.headers["location"].split("#")[0]).text
    assert YET in page and "Prepare it again" not in page
    assert write_guard.lookup(DISPUTE_ID, "message", row["request_sha256"])["state"] == write_guard.UNKNOWN


def test_an_old_absence_releases_and_a_repeat_still_needs_a_new_check_page_and_approval(monkeypatch):
    web, fake, url = _unconfirmed(monkeypatch)
    fake.messages = [BUYER_MESSAGE]
    row = store.list_actions(DISPUTE_ID)[0]
    _age_row(row["id"], 11 * 60 + 10)
    posts_before = len(fake.posts())
    r = _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)
    page = _get(web, r.headers["location"].split("#")[0]).text
    assert "PayPal does not list this message 11 minutes after it was sent. You can prepare it again; nothing is sent automatically." in page
    assert len(fake.posts()) == posts_before and store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"
    assert "Prepare it again" in page and f'action="{PAGE}/preview"' in page
    # nothing was sent by the release; the same register request (old token page) is still refused until a fresh approval
    fake.post_raises = None
    again = _post(web, PAGE + "/preview", {"action": "message", "text": TEXT}, follow_redirects=False)
    check_page = _get(web, _check_url(again)).text
    assert 'data-state="confirm-form"' in check_page and 'data-state="already-sent"' not in check_page
    assert len(fake.posts()) == posts_before
    sent = _post(web, PAGE + "/register", _confirm(check_page), follow_redirects=False)
    assert sent.status_code == 303 and len(fake.posts()) == posts_before + 1
    # and the release is single use: the new send is blocked again like any other
    blocked = service.register(fake, DISPUTE_ID, "message", act.message_body(TEXT), act.request_sha256(act.message_body(TEXT)))
    assert blocked.status == "already_sent" and len(fake.posts()) == posts_before + 1


def test_the_already_sent_notice_has_a_real_check_and_no_link_that_only_hides_it(monkeypatch):
    web, fake, url = _unconfirmed(monkeypatch)
    page = _get(web, _prepare(web)).text
    dom = parse(page)
    assert 'data-state="already-sent"' in page
    assert [a for a in dom.find_all("a") if a.text().strip() == "Check again"] == []
    assert f"{PAGE}/check" in re.findall(r'<form[^>]*action="([^"]*)"', page)


def test_the_hub_check_reads_once_per_unconfirmed_row_and_never_posts(monkeypatch):
    web, fake, url = _unconfirmed(monkeypatch)
    gets_before = len(fake.disputes_gets())
    r = _post(web, PAGE + "/check", {}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#recorded")
    assert len(fake.disputes_gets()) - gets_before == 1 and len(fake.posts()) == 1


def test_a_failed_read_during_the_check_changes_nothing_and_says_so(monkeypatch):
    web, fake, url = _unconfirmed(monkeypatch)
    row = store.list_actions(DISPUTE_ID)[0]
    fake.get_raises = httpx.ConnectError("down")
    r = _post(web, PAGE + "/check", {"row_id": str(row["id"])}, follow_redirects=False)
    fake.get_raises = None
    after = _get(web, r.headers["location"].split("#")[0]).text
    assert "Proofie could not read the dispute from PayPal, so nothing changed." in after
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"


# ==== unavailable actions are plain text once per section and never red ===
def test_unavailable_actions_are_plain_text_once_per_section_and_never_red(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(phase="after_offer", status="WAITING_FOR_BUYER_RESPONSE"))
    hub = _get(web, PAGE).text
    offer = re.search(r'<div data-state="action-offer".*?</div>', hub, re.S).group(0)
    assert offer.count("Not offered by PayPal any more for this dispute.") == 1 and "tag--bad" not in hub
    settling = FakePayPal(settling_reads=99)
    settling.pending, settling.left = "after_offer", 99
    quiet, _ = _web(monkeypatch, settling)
    page = _get(quiet, PAGE).text
    for state in ("action-offer", "action-message", "action-defend"):
        section = re.search(rf'<div data-state="{state}".*?</div>', page, re.S).group(0)
        assert section.count("PayPal is updating this dispute. Options return when it finishes.") == 1
    assert "tag--bad" not in page


def test_the_hub_heading_sections_and_order_sentence(monkeypatch):
    web, fake = _web(monkeypatch)
    hub = _get(web, PAGE).text
    dom = parse(hub)
    assert dom.find("h1").text().strip().startswith("Resolution options and buyer messages")
    assert 'class="h1-id"' in hub and f"Dispute {DISPUTE_ID}" in dom.find("h1").text()
    for text in ("Actions, in this order", "1. Offer a refund (optional)", "2. Send a message to the buyer (optional)",
                 "3. Defend with your evidence", "Each action is optional.",
                 "Proofie has seen PayPal remove the refund option after a seller message, so make an offer first if you want one."):
        assert text in hub, text
    for state in ("action-offer", "action-message", "action-defend"):
        assert f'data-state="{state}"' in hub


# ==== the offer form labels hint and the currency rules =====
def test_the_offer_form_labels_hint_and_the_currency_rules(monkeypatch):
    web, fake = _web(monkeypatch)
    hub = _get(web, PAGE).text
    assert "Offer amount" in hub and "In USD, up to 40.00 USD (the disputed amount). For example 10.00" in hub
    assert "The buyer reads this with your offer. Up to 2000 characters." in hub and "Note to the buyer" in hub
    ok = _post(web, PAGE + "/preview", {**OFFER, "amount": "10 USD"}, follow_redirects=False)
    assert ok.status_code == 303
    cases = {"$10": "Enter only the number, without $", "10 EUR": "This dispute is in USD. Enter the amount in USD.",
             "abc": "Enter the offer amount as a number, for example 10.00", "0": "The offer must be more than 0.00 USD",
             "50": "The offer must be 40.00 USD or less, the disputed amount",
             "1,000": "Enter the offer amount as a number, for example 10.00"}
    for amount, message in cases.items():
        r = _post(web, PAGE + "/preview", {**OFFER, "amount": amount})
        assert r.status_code == 400 and message in r.text, amount


def test_empty_amount_and_empty_note_give_two_summary_entries_in_one_response(monkeypatch):
    web, fake = _web(monkeypatch)
    r = _post(web, PAGE + "/preview", {"action": "offer", "amount": "", "note": ""})
    summary = parse(r.text).find("section", **{"class": "error-summary"})
    assert [li.text().strip() for li in summary.find_all("li")] == ["Enter the offer amount", "Enter a note to the buyer"]
    assert not fake.posts()


def test_the_message_hint_the_quoted_marker_and_the_kept_text(monkeypatch):
    web, fake = _web(monkeypatch)
    hub = _get(web, PAGE).text
    assert "The buyer reads this in PayPal. Up to 2000 characters. Do not include links or email addresses." in hub
    typed = "Please see www.example.invalid for the tracking."
    r = _post(web, PAGE + "/preview", {"action": "message", "text": typed})
    assert r.status_code == 400
    assert ("Your message contains “www.”. Messages sent through Proofie cannot include links or email addresses. "
            "Remove “www.” and review again.") in r.text
    assert typed in re.search(r'<textarea[^>]*name="text"[^>]*>(.*?)</textarea>', r.text, re.S).group(1)
    assert "Enter the message you want to send to the buyer" in _post(web, PAGE + "/preview", {"action": "message", "text": "  "}).text
    assert "Message must be 2000 characters or fewer" in _post(web, PAGE + "/preview", {"action": "message", "text": "x" * 2001}).text


def test_the_fixed_template_has_no_tracking_page_phrase_and_still_validates():
    prepared = service.prepare_message(FakePayPal(), DISPUTE_ID)
    assert prepared.allowed and "tracking page" not in prepared.body["message"]
    assert "28 September 2026" in prepared.body["message"] and service.message_text_problem(prepared.body["message"]) is None


# ==== a read failure at send time keeps the text on the check ===
def test_a_read_failure_at_send_time_keeps_the_text_on_the_check_page(monkeypatch):
    web, fake = _web(monkeypatch)
    fields = _confirm(_get(web, _prepare(web)).text)
    fake.get_raises = httpx.ConnectError("down")
    r = _post(web, PAGE + "/register", fields)
    assert r.status_code == 502 and TEXT in r.text
    assert "Nothing was sent. Proofie could not read the dispute from PayPal. Your text is kept. Press Send when PayPal answers again." in r.text
    assert not fake.posts() and store.list_actions(DISPUTE_ID) == []


def test_a_link_that_disappeared_settling_or_not_gives_different_sentences_and_keeps_the_text(monkeypatch):
    web, fake = _web(monkeypatch)
    fields = _confirm(_get(web, _prepare(web, "offer")).text)
    fake.phase, fake.status = "after_offer", "WAITING_FOR_BUYER_RESPONSE"
    r = _post(web, PAGE + "/register", fields)
    assert r.status_code == 409 and "Partial refund while we check the shipment" in r.text
    assert "Nothing was sent. PayPal no longer offers the refund option on this dispute (checked " in r.text
    assert "Your text is kept below." in r.text and not fake.posts()
    web2, fake2 = _web(monkeypatch)
    fields2 = _confirm(_get(web2, _prepare(web2)).text)
    fake2.pending, fake2.left = "after_message", 99
    r2 = _post(web2, PAGE + "/register", fields2)
    assert "Nothing was sent. PayPal is updating this dispute, so it does not offer this option right now. Try again in a minute; your text is kept." in r2.text
    assert TEXT in r2.text and not fake2.posts()


# ==== the origin of a message is computed by the server =====
def test_the_origin_of_a_message_is_computed_by_the_server(monkeypatch):
    import dataclasses
    from copilot.features.draft import store as draft_store

    @dataclasses.dataclass
    class Sheet:
        x: int = 1

    web, fake = _web(monkeypatch, COPILOT_FEATURE_DRAFT="true")
    template = service.page_state(fake, DISPUTE_ID).template_text
    seller = _get(web, _prepare(web, text="A message written by the seller for this test.")).text
    assert "Where this text came from:</strong> Written by you" in seller
    fixed = _get(web, _prepare(web, text=template)).text
    assert "Proofie's fixed template, not edited" in htmllib.unescape(fixed)
    ai_text = "A valid draft text from the local model for this test."
    draft_store.save_draft(DISPUTE_ID, None, Sheet(), ai_text, "Texto", "qwen-test", 1, "c" * 64, True, [], "model")
    unedited = _get(web, _prepare(web, text=ai_text)).text
    assert "AI text (local model), not edited" in unedited
    edited = _get(web, _prepare(web, text=ai_text + " Edited by the seller.")).text
    assert "May include AI text (local model), edited by you" in edited
    token = re.search(r'name="token" value="([^"]*)"', unedited).group(1)
    stored = store.get_check(token, DISPUTE_ID)
    assert stored["origin"] == "ai_unedited" and stored["request_sha256"] == act.request_sha256({"message": ai_text})
    assert not fake.posts()


# ==== the history table is a stack with a caption and labelled cells ===
def test_the_history_table_is_a_stack_with_a_caption_and_labelled_cells(monkeypatch):
    web, fake = _web(monkeypatch)
    _send(web)
    hub = _get(web, PAGE).text
    table = parse(hub).find("table", class_="resolution-history")
    assert {"resolution-history", "stack"} <= set(table.attrs["class"].split())
    assert table.find("caption").text().strip() == "Offers and messages sent from Proofie, newest last"
    assert all(td.attrs.get("data-label") for td in table.find_all("td"))
    headers = [th.text().strip() for th in table.find_all("th")]
    assert headers == ["When (Mexico City time)", "Action", "Result", "Details"] and not any("sha256" in h for h in headers)
    assert "Proofie keeps what you sent and what PayPal showed afterwards." in hub


# ==== delivery facts links only when a local tracking file exists ===
def test_delivery_facts_links_only_when_a_local_tracking_file_exists(monkeypatch):
    from copilot.features.fulfilment import store as fulfilment_store
    from resolution_fakes import CAPTURE
    web, fake = _web(monkeypatch, COPILOT_FEATURE_DELIVERY_FACTS="true")
    before = _get(web, PAGE).text
    assert 'href="/delivery-facts/' not in before and "Local delivery facts: available after a local tracking file has been saved." in before
    fulfilment_store.save_file(CAPTURE, {"tracking_number": "DEMO-1"}, "a" * 64, "a" * 64, None, None, "saved_without_registration")
    after = _get(web, PAGE).text
    assert f">Local delivery facts for transaction {CAPTURE}</a>" in after
    assert "Add tracking in PayPal" not in after                          # only with the fulfilment flag
    with_flag, _ = _web(monkeypatch, COPILOT_FEATURE_DELIVERY_FACTS="true", COPILOT_FEATURE_FULFILMENT="true")
    assert f'href="/fulfilment/{CAPTURE}">Add tracking in PayPal</a>' in _get(with_flag, PAGE).text


def test_the_offer_first_sentence_is_the_observed_behaviour_wording(monkeypatch):
    from copilot.features.resolution import order
    assert order.TEXT_OFFER_FIRST == ("Proofie has seen PayPal remove the refund option after a seller message. "
                                      "If you want to offer a refund, do it first.")
    web, fake = _web(monkeypatch)
    assert htmllib.unescape(_get(web, PAGE).text).count(order.TEXT_OFFER_FIRST) == 1


def test_a_pending_row_shows_when_paypal_was_last_checked_and_the_page_has_no_meta_refresh(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=99))
    r, _ = _send(web)
    web.get(PAGE)                                                  # one read: PayPal is still updating the dispute
    hub = _get(web, PAGE).text
    assert "Last checked with PayPal at" in hub and "http-equiv" not in hub
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_pending_settle" and len(fake.posts()) == 1
