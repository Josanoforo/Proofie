"""Resolution hub: the stage words and the refund pointer.

Expected: the stage in the status line reads Inquiry, Chargeback, Pre-arbitration or Arbitration (the last two used to read
'Not available'); the pointer 'Proofie cannot refund for you. To refund in full, use PayPal's Resolution Center.' shows only
while PayPal lists accept-claim for the dispute, links the sandbox Resolution Center, adds no form, no button and no route.
Offline: the fake PayPal of resolution_fakes, both web modes."""
import pytest
from _dom import parse

from copilot.web import links
from resolution_fakes import DISPUTE_ID, FakePayPal
from test_ux_rc41_resolution import PAGE, _get, _web

pytestmark = [pytest.mark.usefixtures("web_mode")]

POINTER = "Proofie cannot refund for you. To refund in full, use PayPal's Resolution Center."


@pytest.mark.parametrize("stage, word", [("INQUIRY", "Inquiry"), ("CHARGEBACK", "Chargeback"),
                                         ("PRE_ARBITRATION", "Pre-arbitration"), ("ARBITRATION", "Arbitration"),
                                         ("CLAIM", "Claim"), ("SOMETHING_NEW", "Stage not recognized")])
def test_the_status_line_prints_the_stage_words(monkeypatch, stage, word):
    web, _ = _web(monkeypatch, FakePayPal(settling_reads=0, stage=stage))
    html = _get(web, PAGE).text
    cell = parse(html).find("span", data_stage=stage)
    assert cell is not None and cell.text() == word
    assert stage not in html.replace(f'data-stage="{stage}"', "")                       # never the enum on screen


def _pointer(html):
    return parse(html).find("p", data_state="refund-pointer")


def test_the_pointer_shows_while_paypal_lists_accept_claim_and_links_the_sandbox_resolution_center(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=0, phase="fresh"))
    html = _get(web, PAGE).text
    pointer = _pointer(html)
    assert pointer is not None and pointer.text().replace(" (opens in a new tab)", "").replace(" .", ".") == POINTER
    anchor = pointer.find("a")
    assert anchor.attrs["href"] == links.RESOLUTION_CENTER_URL and anchor.attrs["rel"] == "noopener"
    assert anchor.children[0] == "PayPal's Resolution Center"
    section = parse(html).find("section", data_state="economics")
    assert section is not None and html.index('data-state="economics"') < html.index('data-state="refund-pointer"')


def test_the_pointer_is_absent_when_paypal_lists_no_accept_claim(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=0, phase="after_offer"))
    assert _pointer(_get(web, PAGE).text) is None


def test_the_pointer_adds_no_form_no_button_no_route_and_no_post(monkeypatch):
    fresh, fake = _web(monkeypatch, FakePayPal(settling_reads=0, phase="fresh"))
    with_pointer = parse(_get(fresh, PAGE).text)
    pointer = with_pointer.find("p", data_state="refund-pointer")
    assert not pointer.find_all("form") and not pointer.find_all("button")
    paths = list(fresh.app.openapi()["paths"])
    assert not [p for p in paths if "accept" in p]
    assert fake.posts() == []


# ------------------------------------------------------------------------------------------------ sending cannot be undone: offer and message
# Expected: both review pages (offer and message) say, in the send form and before the send button, that sending cannot be
# undone from Proofie and that Proofie refuses a repeat send; the sentence is true: a second register of the same token makes
# no second POST.
@pytest.mark.parametrize("action, noun", [("offer", "offer"), ("message", "message")])
def test_the_review_page_says_sending_cannot_be_undone_before_the_send_button(monkeypatch, action, noun):
    from test_ux_rc41_resolution import _prepare
    web, fake = _web(monkeypatch)
    html = _get(web, _prepare(web, action)).text
    form = parse(html).find("form", data_state="confirm-form")
    note = form.find("p", data_state="cannot-undo")
    assert note.text() == f"Sending cannot be undone from Proofie. Proofie refuses a repeat send of the same {noun}."
    walk = list(form.walk())
    assert walk.index(note) < walk.index(form.find("button"))


def test_the_cannot_be_undone_sentence_is_true_a_repeat_register_posts_nothing_more(monkeypatch):
    from test_ux_rc41_resolution import _confirm, _post, _prepare
    web, fake = _web(monkeypatch)
    page = _get(web, _prepare(web, "message")).text
    fields = _confirm(page)
    _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert len(fake.posts()) == 1
    _post(web, PAGE + "/register", fields, follow_redirects=False)
    assert len(fake.posts()) == 1


def test_a_dispute_with_no_stage_has_no_stage_clause_in_the_status_line(monkeypatch):
    class NoStage(FakePayPal):
        def call(self, method, path, op, **kw):
            body = super().call(method, path, op, **kw)
            if isinstance(body, dict) and body.get("dispute_id") == DISPUTE_ID:
                body = {k: v for k, v in body.items() if k != "dispute_life_cycle_stage"}
            return body
    web, _ = _web(monkeypatch, NoStage(settling_reads=0))
    dom = parse(_get(web, PAGE).text)
    line = dom.find("p", data_state="dispute-status")
    assert line is not None and "stage" not in line.text().lower() and line.find("span", data_stage="None") is None
    assert line.text().endswith(".") and "()" not in line.text() and "( )" not in line.text()
