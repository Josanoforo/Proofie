"""A refused message or offer is explained in plain words; the HTTP status stays in Technical details.
Offline: the fake PayPal of resolution_fakes and a temp database."""
import re

import pytest
from _dom import parse

from copilot.core import paypal_words
from copilot.core.disputes import PayPalError
from resolution_fakes import FakePayPal
from test_ux_rc41_fix_sends import _fields_and_page, _post, _web
from test_ux_rc41_resolution import PAGE

pytestmark = [pytest.mark.usefixtures("web_mode")]


def _refused_page(monkeypatch, issue, body=None):
    fake = FakePayPal(post_raises=PayPalError(400, issue, "dbg-1", body or {}))
    web, _ = _web(monkeypatch, fake)
    fields, _page = _fields_and_page(web)
    response = _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert response.status_code in (409, 502)
    return response.text


def _outside_details(html):
    return re.sub(r"<details.*?</details>", "", html, flags=re.S)


def test_a_refused_message_says_what_to_do_and_keeps_the_status_in_technical_details(web_mode, monkeypatch):
    html = _refused_page(monkeypatch, "INVALID_REQUEST")
    summary = parse(html).find("section", class_="error-summary").text()
    assert "PayPal could not send the message." in summary and "Your text is kept." in summary
    assert "Change it and send again, or check the dispute in PayPal's Resolution Center." in summary
    assert "PayPal says something in this request is not valid." in summary          # the cause, from PayPal's issue
    visible = _outside_details(html)
    assert not re.search(r"error 400|\b400\b|INVALID_REQUEST", visible)
    details = " ".join(n.text() for n in parse(html).find_all("details"))
    assert "HTTP status 400" in details and "INVALID_REQUEST" in details


def test_a_refusal_with_an_issue_nobody_mapped_says_that_paypal_did_not_say_why(web_mode, monkeypatch):
    html = _refused_page(monkeypatch, "SOME_NEW_CODE")
    summary = parse(html).find("section", class_="error-summary").text()
    assert "PayPal did not say why" in summary and "SOME_NEW_CODE" not in _outside_details(html)
    assert "SOME_NEW_CODE" in " ".join(n.text() for n in parse(html).find_all("details"))


def test_the_plain_failure_sentence_never_carries_a_status():
    exc = PayPalError(400, "INVALID_TRACKING_NUMBER", "dbg", {})
    text = paypal_words.could_not(exc, "register the tracking")
    assert text.startswith("PayPal could not register the tracking.") and "400" not in text and "error" not in text.lower()
    assert paypal_words.technical_of(exc) == "HTTP status 400; PayPal issue INVALID_TRACKING_NUMBER"
    assert "right now" in paypal_words.could_not(PayPalError(503, None, "d", {}), "read the dispute")


def test_the_delivery_facts_sentence_has_its_own_subject_and_sits_in_the_delivery_facts_paragraph(web_mode, monkeypatch):
    """Under "3. Defend with your evidence" the old sentence read as a condition on the Defend link."""
    web, _fake = _web(monkeypatch, COPILOT_FEATURE_DELIVERY_FACTS="true")
    dom = parse(web.get(PAGE).text)
    paragraph = dom.find("p", data_state="delivery-facts-links")
    assert paragraph.text().startswith("Local delivery facts: available after a local tracking file has been saved.")
    defend = dom.find("div", data_state="action-defend")
    assert "tracking file" not in defend.text() and paragraph not in list(defend.walk())
