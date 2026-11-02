"""PayPal's timeline on the dispute page (W1-2), behind COPILOT_FEATURE_TIMELINE. Offline: the fake PayPal client of the
dispute-page tests (it refuses any write and any unknown op) and a temp database per test; both web modes.

Expected before the code: flag off, the page has no timeline section and is made of the same PayPal reads as ever. Flag on,
one section between "What PayPal confirmed" and the app's own "Action timeline" lists PayPal's dated items with PayPal's own
time in Mexico City time, says "As PayPal's record reads at <time>" and that PayPal's record has no log of status changes,
shows no message body, no note and no link, escapes whatever PayPal sent, and adds no PayPal call and no POST.
"""
import copy
import re

import pytest

from copilot import config
from copilot.db import repo

import test_ux_rc41_dispute as dt
from test_ux_rc41_dispute import DID

pytestmark = [pytest.mark.usefixtures("web_mode")]

NO_LOG_SENTENCE = ("PayPal's dispute record has no log of status changes, so this list shows only the dated items PayPal "
                   "gives. It does not show when the status changed.")
BODY_TEXT = "BUYER-WROTE-THIS-SECRET-BODY"


def _body(**update):
    body = copy.deepcopy(dt.WAITING)
    body["messages"] = [{"posted_by": "BUYER", "time_posted": "2026-10-05T02:13:17.163Z", "content": BODY_TEXT,
                         "documents": [{"name": "scan.png", "url": "https://api-m.sandbox.paypal.com/v2/content/x/download"}]}]
    body.update(update)
    return body


def _timeline(web_body, monkeypatch, on=True):
    monkeypatch.setenv("COPILOT_FEATURE_TIMELINE", "true" if on else "false")
    web, fake = dt._web(web_body)
    text, dom = dt._page(web, view="records")
    return text, dom, dom.find("section", data_state="paypal-timeline"), fake


def test_flag_off_the_page_has_no_timeline(monkeypatch):
    text, dom, section, _ = _timeline(_body(), monkeypatch, on=False)
    assert section is None and "paypal-timeline" not in text and "As PayPal's record reads" not in text


@pytest.mark.parametrize("value", ["", "0", "1", "yes", "on", "False"])
def test_only_the_word_true_turns_it_on(monkeypatch, value):
    monkeypatch.setenv("COPILOT_FEATURE_TIMELINE", value)
    web, _ = dt._web(_body())
    assert "paypal-timeline" not in dt._page(web, view="records")[0]


def test_flag_on_the_section_lists_PayPals_dated_items_in_Mexico_City_time(monkeypatch):
    text, dom, section, _ = _timeline(_body(), monkeypatch)
    assert section is not None
    items = [li.text() for li in section.find_all("li")]
    assert items[0].startswith("4 Oct 2026, 8:13 pm (Mexico City time)")            # 2026-10-05T02:13:17Z at UTC-6
    assert any("Dispute opened" in i for i in items)
    assert any("The buyer posted a message" in i and "scan.png" in i for i in items)
    assert any("PayPal asked the seller for evidence: Proof of fulfillment" in i for i in items)
    assert any("PayPal last updated the dispute" in i and "4 Oct 2026, 8:16 pm" in i for i in items)
    times = section.find_all("time")
    assert times and all(t.attrs.get("datetime", "").endswith("-06:00") for t in times[1:]), [t.attrs for t in times]


def test_the_section_states_when_PayPals_record_was_read_and_that_it_has_no_status_log(monkeypatch):
    text, dom, section, _ = _timeline(_body(), monkeypatch)
    sentence = section.find("p", data_state="paypal-timeline-asof").text()
    assert re.fullmatch(r"As PayPal's record reads at \d{1,2} \w{3} \d{4}, \d{1,2}:\d{2} (am|pm) \(Mexico City time\) ?\.", sentence), sentence      # the DOM helper joins text nodes with a space
    assert section.find("p", data_state="paypal-timeline-note").text() == NO_LOG_SENTENCE


def test_no_message_body_note_or_link_is_on_the_section(monkeypatch):
    text, dom, section, _ = _timeline(_body(), monkeypatch)
    html = text[text.index('data-state="paypal-timeline"'):]
    html = html[:html.index("</section>")]
    assert BODY_TEXT not in html and "DEMO DATA" not in html and "https://" not in html and "<a " not in html


def test_the_section_sits_before_the_apps_action_timeline(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_TIMELINE", "true")
    web, _ = dt._web(_body())
    repo.log_action(config.db_path(), "system", "send_attempt_registered", DID,
                    {"package_id": 1, "request_id": "r", "attachments": []})
    text, dom = dt._page(web, view="records")
    assert 'data-state="timeline"' in text, "the app's own action timeline needs an audit row to show"
    assert text.index('data-state="paypal-timeline"') < text.index('data-state="timeline"')
    assert "what PayPal confirmed" in dt._page(web, view="send")[0]        # O4: the overview no longer carries it; the send view heads it "What was sent and what PayPal confirmed"
    assert "Action timeline (" in text


def test_a_time_with_no_offset_reads_not_available_and_is_last(monkeypatch):
    body = _body(evidences=[{"evidence_type": "OTHER", "source": "SUBMITTED_BY_SELLER", "date": "2026-10-04T10:00:00"}])
    text, dom, section, _ = _timeline(body, monkeypatch)
    items = [li.text() for li in section.find_all("li")]
    assert items[-1].startswith("Not available") and "Seller submitted evidence" in items[-1], items


def test_what_PayPal_sent_is_escaped(monkeypatch):
    hostile = '<img src=x onerror=alert(1)>.pdf'
    body = _body(messages=[{"posted_by": "BUYER", "time_posted": "2026-10-05T02:13:17Z", "documents": [{"name": hostile}]}])
    text, dom, section, _ = _timeline(body, monkeypatch)
    assert "<img src=x" not in text and "&lt;img src=x onerror=alert(1)&gt;.pdf" in text


def test_a_dispute_with_no_dated_item_says_so(monkeypatch):
    body = _body(messages=[], evidences=[], create_time=None, update_time=None, extensions={})
    text, dom, section, _ = _timeline(body, monkeypatch)
    assert section.find_all("li") == []
    assert "PayPal's record lists no dated items for this dispute." in section.text()
    assert NO_LOG_SENTENCE in section.text()


def test_flag_on_adds_no_paypal_call_and_no_write(monkeypatch):
    _, _, _, off = _timeline(_body(), monkeypatch, on=False)
    _, _, _, on = _timeline(_body(), monkeypatch, on=True)
    assert on.calls == off.calls and all(method == "GET" for method, _, _ in on.calls)


def test_the_synthetic_under_review_dispute_renders_too(monkeypatch):
    body = copy.deepcopy(dt.json.loads((dt.REC / "dispute_PP-R-DEMO-0000001_under_review.json").read_text(encoding="utf-8"))["body"])
    text, dom, section, _ = _timeline(body, monkeypatch)
    assert section is not None and section.find_all("li")
