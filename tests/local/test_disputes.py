"""Offline tests for copilot/core/disputes.py's plain-language layer. No
network: these test humanize_status/humanize_deadline as pure functions over normalize()-shaped dicts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.disputes import humanize_deadline, humanize_reason, humanize_status


def test_known_statuses_never_show_the_raw_paypal_constant():
    for raw in ("OPEN", "WAITING_FOR_SELLER_RESPONSE", "WAITING_FOR_BUYER_RESPONSE", "UNDER_REVIEW",
               "RESOLVED"):
        text = humanize_status(raw)
        assert raw not in text, text
        assert "_" not in text.split(" (")[0]   # no raw enum-with-underscores leaking into the main text


def test_waiting_for_seller_response_reads_as_waiting_for_your_response():
    assert humanize_status("WAITING_FOR_SELLER_RESPONSE") == "Waiting for your response"


def test_unrecognised_status_still_gets_a_readable_sentence_not_hidden():
    text = humanize_status("SOME_NEW_PAYPAL_STATUS")
    assert text == "Status not recognised: SOME_NEW_PAYPAL_STATUS"
    assert text != ""


def test_none_or_empty_status_says_unknown_not_blank():
    assert humanize_status(None) == "Status unknown"
    assert humanize_status("") == "Status unknown"


# ---- regression: humanize_reason (the "Reason" column/field leaked the raw PayPal constant until this
# ---- was caught by Playwright-validating the actual rendered page against the real sandbox, P-b) --------
def test_known_reasons_never_show_the_raw_paypal_constant():
    for raw in ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
               "UNAUTHORISED"):
        text = humanize_reason(raw)
        assert raw not in text, text


def test_item_not_received_reason_reads_in_plain_language():
    assert humanize_reason("MERCHANDISE_OR_SERVICE_NOT_RECEIVED") == \
        "Buyer says they did not receive the item or service"


def test_unrecognised_reason_still_gets_a_readable_sentence_not_hidden():
    text = humanize_reason("SOME_NEW_REASON")
    assert text == "Reason not recognised: SOME_NEW_REASON"


def test_none_or_empty_reason_says_unknown_not_blank():
    assert humanize_reason(None) == "Reason unknown"
    assert humanize_reason("") == "Reason unknown"


def test_no_deadline_in_response_says_no_verified_deadline_literally():
    norm = {"seller_response_due_date": None, "deadline_cdmx": None}
    assert humanize_deadline(norm) == "No verified deadline available"


def test_deadline_present_shows_computed_remaining_and_paypal_date():
    norm = {"seller_response_due_date": "2026-10-25T02:13:17Z", "deadline_cdmx": "2026-10-24 20:13 CST",
           "remaining_text": "19d 23h", "overdue": False}
    text = humanize_deadline(norm)
    assert "19d 23h" in text and "2026-10-24 20:13 CST" in text and "No verified" not in text


def test_overdue_deadline_says_overdue_not_a_fake_remaining_time():
    norm = {"seller_response_due_date": "2026-09-01T00:00:00Z", "deadline_cdmx": "2026-08-31 18:00 CST",
           "remaining_text": "overdue by 5d 2h", "overdue": True}
    text = humanize_deadline(norm)
    assert "Overdue" in text and "overdue by 5d 2h" in text


def test_deadline_never_invented_when_parse_failed_even_with_a_due_date_string():
    """If PayPal sent a due date string but normalize() couldn't parse it, deadline_cdmx is None - must
    still say 'No verified deadline available', never fabricate a value."""
    norm = {"seller_response_due_date": "garbled-not-a-real-date", "deadline_cdmx": None}
    assert humanize_deadline(norm) == "No verified deadline available"
