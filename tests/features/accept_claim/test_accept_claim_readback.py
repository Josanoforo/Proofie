"""D-101 B3 read-back rule: PayPal's sandbox answers the accept with the link gone and the disputed transaction REFUNDED while the case is
still under its review (OCQ, 2026-11-02 00:45), and closes the case about two minutes later. The stored status follows PayPal's response
only: closed -> sent_confirmed; link gone + transaction REFUNDED + not closed -> sent_refunded_in_review; anything else -> sent_unconfirmed.
Synthetic fakes only, no network."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_accept_claim import ACCEPT, AcceptFake, PHRASE, _check, _csrf, _send, _web  # noqa: E402
from resolution_fakes import DISPUTE_ID, _links  # noqa: E402

from copilot.features.accept_claim import service as accept  # noqa: E402

HEADING = "PayPal shows the buyer refunded; the case is under PayPal&#39;s review"
HEADING_ALT = "PayPal shows the buyer refunded; the case is under PayPal's review"


class ReviewFake(AcceptFake):
    """After the accept POST the case reads UNDER_REVIEW with no accept link and the transaction `tx` (REFUNDED by default);
    `later_closed` flips it to RESOLVED, as the sandbox did about two minutes after the POST."""
    def __init__(self, *a, tx="REFUNDED", status="UNDER_REVIEW", keep_link=False, **k):
        super().__init__(*a, **k)
        self.tx, self.review_status, self.keep_link, self.later_closed = tx, status, keep_link, False

    def _dispute(self):
        if not self.closed:
            return super()._dispute()
        if self.later_closed:
            return self._with_tx(self._raw("RESOLVED", ()))
        return self._with_tx(self._raw(self.review_status, ("accept_claim",) if self.keep_link else ()))

    def _with_tx(self, raw):
        raw["disputed_transactions"] = [{**t, "transaction_status": self.tx} for t in raw["disputed_transactions"]]
        return raw


def _sent(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    r = _send(web, token, sha)
    assert r.status_code == 303 and "/accept-claim/sent/" in r.headers["location"]
    return web, r.headers["location"]


def _row_id(location):
    return int(location.rsplit("/", 1)[-1])


def test_link_gone_transaction_refunded_case_not_closed_is_stored_as_refunded_in_review(monkeypatch, tmp_path):
    fake = ReviewFake()
    web, where = _sent(monkeypatch, tmp_path, fake)
    row = accept.get_sent(DISPUTE_ID, _row_id(where))
    assert row["status"] == accept.STATUS_REVIEW == "sent_refunded_in_review"
    assert row["readback"]["transaction_status"] == "REFUNDED" and row["readback"]["status"] == "UNDER_REVIEW"
    assert row["readback"]["link_present"] is False                  # stored from PayPal's response, not from the page
    assert len(fake.accept_posts()) == 1
    page = web.get(where).text
    assert 'data-status="sent_refunded_in_review"' in page and (HEADING in page or HEADING_ALT in page)
    assert "Reload this page to read it again; nothing is sent when you do." in page
    assert "the dispute is under review" in page and "the accept option is gone" in page
    assert "Sent to PayPal; not confirmed yet" not in page and "PayPal shows the claim accepted" not in page
    assert len(fake.accept_posts()) == 1                              # the reloads wrote nothing


def test_closed_with_no_link_stays_confirmed_with_todays_sentence(monkeypatch, tmp_path):
    fake = AcceptFake()
    web, where = _sent(monkeypatch, tmp_path, fake)
    assert accept.get_sent(DISPUTE_ID, _row_id(where))["status"] == accept.STATUS_CONFIRMED
    page = web.get(where).text
    assert "PayPal shows the claim accepted" in page and "PayPal shows the claim accepted and the buyer refunded" in page and "PayPal's case status:" in page and not re.search(r"T\d\d:\d\d:\d\d", re.sub(r"<[^>]+>", "", page))      # the machine time stays in the time tag only


@pytest.mark.parametrize("fake", [
    ReviewFake(tx="HELD"),                          # not refunded yet
    ReviewFake(tx="REFUNDED", keep_link=True),      # the accept option is still offered
    ReviewFake(close_on_post=False),                # the POST changed nothing in PayPal's record
], ids=["not-refunded", "link-still-listed", "unchanged"])
def test_anything_else_stays_unconfirmed(monkeypatch, tmp_path, fake):
    web, where = _sent(monkeypatch, tmp_path, fake)
    assert accept.get_sent(DISPUTE_ID, _row_id(where))["status"] == accept.STATUS_UNCONFIRMED
    page = web.get(where).text
    assert "Sent to PayPal; not confirmed yet" in page and "under PayPal" not in page


def test_no_readback_response_is_unconfirmed():
    assert accept.classify(None, None) == accept.STATUS_UNCONFIRMED
    assert accept.transaction_status(None) is None and accept.transaction_status({"disputed_transactions": []}) is None
    mixed = {"disputed_transactions": [{"transaction_status": "REFUNDED"}, {"transaction_status": "HELD"}]}
    assert accept.transaction_status(mixed) == "HELD"                 # one transaction not refunded: not refunded
    assert accept.transaction_status({"disputed_transactions": [{"transaction_status": "refunded"}]}) == "REFUNDED"


def test_a_page_that_says_refunded_without_paypal_saying_so_changes_nothing(monkeypatch, tmp_path):
    """Only the response decides: with a HELD transaction no page text can lift the row to refunded-in-review."""
    fake = ReviewFake(tx="HELD")
    web, where = _sent(monkeypatch, tmp_path, fake)
    for _ in range(2):
        web.get(where)
    assert accept.get_sent(DISPUTE_ID, _row_id(where))["status"] == accept.STATUS_UNCONFIRMED


def test_recheck_moves_the_review_status_to_confirmed_when_paypal_closes_the_case(monkeypatch, tmp_path):
    fake = ReviewFake()
    web, where = _sent(monkeypatch, tmp_path, fake)
    row_id = _row_id(where)
    assert accept.get_sent(DISPUTE_ID, row_id)["status"] == accept.STATUS_REVIEW
    fake.later_closed = True                                           # the sandbox closes the case a couple of minutes later
    page = web.get(where).text                                         # the sent page does the one recheck GET
    row = accept.get_sent(DISPUTE_ID, row_id)
    assert row["status"] == accept.STATUS_CONFIRMED and row["readback"]["status"] == "RESOLVED"
    assert "PayPal shows the claim accepted" in page
    assert len(fake.accept_posts()) == 1                               # no second write


def test_recheck_never_drops_a_review_row_back_to_unconfirmed(monkeypatch, tmp_path):
    fake = ReviewFake()
    web, where = _sent(monkeypatch, tmp_path, fake)
    fake.review_status, fake.tx = "UNDER_REVIEW", "HELD"              # an odd later read
    web.get(where)
    assert accept.get_sent(DISPUTE_ID, _row_id(where))["status"] == accept.STATUS_REVIEW


def test_the_caps_and_the_pool_count_the_review_status(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    accept.save_sent("PP-R-TST-0000002", "a" * 64, accept.STATUS_REVIEW, None, {"status": "UNDER_REVIEW"}, None, "sess-1")
    assert accept.was_accepted("PP-R-TST-0000002")                      # the pool shows it Used
    assert "in this session" in (accept.cap_refusal("sess-1") or "")
    assert "today" in (accept.cap_refusal("another-session") or "")
    assert accept.STATUS_REVIEW in accept.SENT_STATUSES


def test_the_sent_page_names_the_state_paypal_returned_and_never_says_not_confirmed_yet(monkeypatch, tmp_path):
    """UNDER_REVIEW + dispute_state UNDER_PAYPAL_REVIEW + transaction REFUNDED (OCQ, copilot_web.jsonl:16): the page names that state."""
    class Real(ReviewFake):
        def _with_tx(self, raw):
            return {**super()._with_tx(raw), "dispute_state": "UNDER_PAYPAL_REVIEW"}
    web, where = _sent(monkeypatch, tmp_path, Real())
    page = web.get(where).text
    assert "not confirmed yet" not in page
    assert "the disputed transaction is refunded" in page and "the dispute is under review" in page and "the accept option is gone" in page
    assert HEADING in page or HEADING_ALT in page
