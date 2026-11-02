"""D-101 seller item 3: the offer form is proposed from PayPal's own record (COPILOT_FEATURE_SELLER_SHORTCUTS).
The check page still shows the exact request and nothing is sent until the seller sends it. The message form was already
prefilled with the fixed template and stays so."""
import html as html_module
import re

import pytest
from _hosted import reset_csrf, set_test_csrf

from resolution_fakes import DISPUTE_ID, FakePayPal
from copilot.features.resolution import actions as act, store
from test_resolution_service_and_routes import PAGE, TOKEN, _confirm_form, _web

SHORTCUTS = "COPILOT_FEATURE_SELLER_SHORTCUTS"
LABEL = "Proposed from PayPal's record: the buyer's requested amount; you can change it."


class WithRequested(FakePayPal):
    """The dispute as PayPal reads it, plus offer.buyer_requested_amount."""
    def __init__(self, asked=("USD", "15.00"), **kw):
        super().__init__(**kw)
        self.asked = asked

    def _raw(self, status, rels):
        raw = super()._raw(status, rels)
        if self.asked:
            raw["offer"] = {"buyer_requested_amount": {"currency_code": self.asked[0], "value": self.asked[1]}}
        return raw


def _value(page, name):
    """The value of the form control called `name`, or None when the page has none."""
    area = re.search(rf'<textarea[^>]*name="{name}"[^>]*>(.*?)</textarea>', page, re.S)
    if area:
        return html_module.unescape(area.group(1).strip())
    tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    if not tag:
        return None
    value = re.search(r'value="([^"]*)"', tag.group(0))
    return html_module.unescape(value.group(1)) if value else ""


def test_with_the_setting_on_the_offer_form_comes_with_the_amount_the_buyer_asked_for(monkeypatch):
    monkeypatch.setenv(SHORTCUTS, "true")
    web, fake = _web(monkeypatch, WithRequested())
    page = web.get(PAGE).text
    assert _value(page, "amount") == "15.00"
    assert "15.00 USD" in (_value(page, "note") or "") and 'data-state="offer-proposed"' in page
    assert LABEL in page
    assert not fake.posts()


def test_with_the_setting_off_the_offer_form_is_empty_as_before(monkeypatch):
    monkeypatch.delenv(SHORTCUTS, raising=False)
    web, fake = _web(monkeypatch, WithRequested())
    page = web.get(PAGE).text
    assert (_value(page, "amount") or "") == "" and 'data-state="offer-proposed"' not in page


@pytest.mark.parametrize("asked", [("USD", "99.00"), ("MXN", "15.00"), ("USD", "0.00"), ("USD", "abc"), None])
def test_nothing_is_proposed_when_the_record_is_missing_foreign_or_above_the_disputed_amount(monkeypatch, asked):
    monkeypatch.setenv(SHORTCUTS, "true")
    web, fake = _web(monkeypatch, WithRequested(asked=asked, amount="40.00"))
    page = web.get(PAGE).text
    assert (_value(page, "amount") or "") == "" and 'data-state="offer-proposed"' not in page


def test_nothing_is_proposed_once_paypal_no_longer_offers_the_offer(monkeypatch):
    monkeypatch.setenv(SHORTCUTS, "true")
    web, fake = _web(monkeypatch, WithRequested(phase="after_offer"))
    assert 'data-state="offer-proposed"' not in web.get(PAGE).text


def test_the_proposed_form_goes_to_the_check_page_and_sends_nothing(monkeypatch):
    monkeypatch.setenv(SHORTCUTS, "true")
    web, fake = _web(monkeypatch, WithRequested())
    page = web.get(PAGE).text
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    form = {"csrf_token": TOKEN, "action": "offer", "amount": _value(page, "amount"), "note": _value(page, "note")}
    check = web.post(PAGE + "/preview", data=form)
    assert check.status_code == 200
    confirm = _confirm_form(check.text)                          # the check page: the exact request and its fingerprint
    body = store.get_check(confirm["token"], DISPUTE_ID)["body"]
    assert body["offer_amount"] == {"currency_code": "USD", "value": "15.00"}
    assert confirm["approval_sha256"] == act.request_sha256(body)
    assert not fake.posts()                         # reviewing is not sending


def test_the_message_form_comes_with_the_fixed_template_text(monkeypatch):
    monkeypatch.setenv(SHORTCUTS, "true")
    web, fake = _web(monkeypatch, WithRequested())
    assert len(_value(web.get(PAGE).text, "text") or "") > 40


def _check_page(web, page, **override):
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    form = {"csrf_token": TOKEN, "action": "offer", "amount": _value(page, "amount"), "note": _value(page, "note"),
            "proposed_from_record": "1"}
    form.update(override)
    first = web.post(PAGE + "/preview", data=form, follow_redirects=False)
    assert first.status_code == 303
    return web.get(first.headers["location"])


def test_the_check_page_carries_the_label_while_the_amount_is_the_one_from_the_record(monkeypatch):
    monkeypatch.setenv(SHORTCUTS, "true")
    web, fake = _web(monkeypatch, WithRequested())
    page = web.get(PAGE).text
    check = _check_page(web, page)
    assert LABEL in check.text and 'data-state="offer-proposed-label"' in check.text
    assert not fake.posts()


def test_the_label_is_gone_from_the_check_page_once_the_seller_changes_the_amount(monkeypatch):
    monkeypatch.setenv(SHORTCUTS, "true")
    web, fake = _web(monkeypatch, WithRequested())
    page = web.get(PAGE).text
    check = _check_page(web, page, amount="9.00")
    assert 'data-state="offer-proposed-label"' not in check.text and "9.00" in check.text


def test_the_label_cannot_be_claimed_by_a_form_that_the_page_did_not_propose(monkeypatch):
    monkeypatch.delenv(SHORTCUTS, raising=False)
    web, fake = _web(monkeypatch, WithRequested())
    page = web.get(PAGE).text
    check = _check_page(web, page, amount="15.00", note="A note of my own.")
    assert 'data-state="offer-proposed-label"' not in check.text
