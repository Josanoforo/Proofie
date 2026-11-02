"""Draft page: the 'dispute stage' fact reads PayPal's stage in the table words.

Expected: INQUIRY, CHARGEBACK, PRE_ARBITRATION, ARBITRATION print Inquiry, Chargeback, Pre-arbitration, Arbitration (the last
two used to print 'Not available'); the legacy CLAIM prints Claim; an unknown or missing value prints 'Stage not recognized',
never the enum. Offline: fake PayPal (GET only), both web modes."""
import pytest
from _dom import parse

from draft_fakes import DISPUTE_ID, FakePayPal
from test_draft_routes import PAGE, _web

pytestmark = [pytest.mark.usefixtures("web_mode")]


class StageFake(FakePayPal):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage

    def call(self, method, path, op, **kw):
        body = super().call(method, path, op, **kw)
        if isinstance(body, dict) and body.get("dispute_id") == DISPUTE_ID:
            body = {k: v for k, v in body.items() if k != "dispute_life_cycle_stage"}
            if self.stage is not None:
                body["dispute_life_cycle_stage"] = self.stage
        return body


def _stage_cell(monkeypatch, stage):
    web, fake, _call = _web(monkeypatch, fake=StageFake(stage))
    dom = parse(web.get(PAGE).text)
    rows = [r for r in dom.find("table", data_state="facts").find_all("tr") if r.find("th").text() == "Dispute stage"]
    return rows[0].find("td").text() if rows else None


@pytest.mark.parametrize("value, word", [("INQUIRY", "Inquiry"), ("CHARGEBACK", "Chargeback"),
                                         ("PRE_ARBITRATION", "Pre-arbitration"), ("ARBITRATION", "Arbitration"),
                                         ("CLAIM", "Claim"), ("SOMETHING_NEW", "Stage not recognized")])
def test_the_dispute_stage_fact_uses_the_stage_words(monkeypatch, value, word):
    assert _stage_cell(monkeypatch, value) == word


def test_a_rejected_date_keeps_the_literal_the_model_wrote_and_adds_the_display_form():
    """the literal is the evidence; the seller's date format follows it in brackets."""
    from copilot.features.draft.check import Issue
    from copilot.features.draft.service import rejected_sentences

    def date(value):
        return Issue("date", "English", value)
    assert rejected_sentences([date("2026-10-02")]) == ["It has a date that is not in the facts: 2026-10-02 (2 Oct 2026)."]
    assert rejected_sentences([date("2026-10-02"), date("2026-10-03")]) == [
        "It has dates that are not in the facts: 2026-10-02 (2 Oct 2026), 2026-10-03 (3 Oct 2026)."]
    assert rejected_sentences([date("2026-13-45")]) == ["It has a date that is not in the facts: 2026-13-45."]
    assert rejected_sentences([Issue("number", "English", "98765")]) == ["It has numbers that are not in the facts: 98765."]
