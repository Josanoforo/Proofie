"""The fact sheet from a fixture: literals, numbers, None fields left out, integer arithmetic."""
import dataclasses

from copilot.features.draft import facts
from draft_fakes import DISPUTE_ID, NORM, TRACK, TXN, sheet
from draft_fakes import _no_httpx_calls  # noqa: F401  (autouse: no httpx call in these tests)


def test_the_sheet_from_the_fixture_has_every_fact():
    s = sheet()
    assert (s.order_date, s.amount_text, s.currency) == ("2026-09-28", "40.00 USD", "USD")
    assert s.shipping_address_line == "Domicilio Demo 123, Ciudad de Mexico, CDMX, 06600, MX"
    assert (s.tracking_number, s.carrier, s.tracking_status, s.delivery_estimate) == ("DEMO-FDX-000001", "FEDEX", "SHIPPED", "2026-10-10")
    assert (s.dispute_id, s.dispute_stage) == (DISPUTE_ID, "INQUIRY")


def test_literals_are_every_non_empty_value_as_text_and_numbers_are_the_1_plus_digit_runs():
    s = sheet()
    assert s.literals == ("2026-09-28", "40.00 USD", "USD", "Domicilio Demo 123, Ciudad de Mexico, CDMX, 06600, MX",
                          "DEMO-FDX-000001", "FEDEX", "SHIPPED", "2026-10-10", DISPUTE_ID, "INQUIRY")
    assert s.numbers == ("2026", "09", "28", "40", "00", "123", "06600", "000001", "10", "0000001")   # every run of 1+ digits; no duplicates


def test_none_fields_are_left_out_of_literals_and_of_the_labelled_list():
    s = facts.fact_sheet(None, {}, None)
    assert dataclasses.astuple(s)[:10] == (None,) * 10 and s.literals == () and s.numbers == () and s.labelled() == []
    partial = facts.fact_sheet({"create_time": "2026-10-01T15:00:00Z", "amount_cents": 1250, "currency": "USD"}, {}, None)
    assert partial.literals == ("2026-10-01", "12.50 USD", "USD") and partial.tracking_number is None
    assert [label for label, _v in partial.labelled()] == ["order date", "amount", "currency"]


def test_the_amount_text_is_integer_arithmetic_and_matches_the_resolution_template():
    assert [facts.money_text(c, "USD") for c in (5, 100, 4000, 123456)] == ["0.05 USD", "1.00 USD", "40.00 USD", "1234.56 USD"]
    assert facts.money_text(None, "USD") is None and facts.money_text(5, None) is None
    from copilot.features.resolution import actions
    assert facts.fact_sheet(NORM, TXN, TRACK).amount_text in actions.message_text(NORM, TXN, TRACK)


def test_a_large_amount_contributes_its_digit_run_to_numbers():
    s = facts.fact_sheet(NORM, {**TXN, "total_amount_cents": 123456}, TRACK)
    assert "1234" in s.numbers


def test_the_sheet_agrees_with_the_template_on_every_literal_it_shares():
    from copilot.features.resolution import actions
    template = actions.message_text(NORM, TXN, TRACK)
    s = sheet()
    from copilot.core import carriers
    # The fixed template writes the order date in words ("28 September 2026") and the carrier by its name
    # ("FedEx"), and no longer repeats PayPal's raw tracking status.
    for literal in (actions.date_words(s.order_date), s.amount_text, s.shipping_address_line, s.tracking_number,
                    carriers.display_name(s.carrier)):
        assert literal in template


def test_the_sheet_is_immutable_and_values_are_stripped():
    s = facts.fact_sheet({"dispute_id": "  PP-R-TEST-0000001  "}, {}, {"carrier": "  FEDEX "})
    assert s.dispute_id == "PP-R-TEST-0000001" and s.carrier == "FEDEX"
    try:
        s.carrier = "UPS"
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("the fact sheet must be frozen")
