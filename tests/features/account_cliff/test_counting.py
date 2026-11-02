"""Counting and threshold arithmetic on synthetic rows (no recorded response, no real ids)."""
from datetime import date

import pytest

from copilot.features.account_cliff import counting
from copilot.features.account_cliff.rules import cents_to_usd_text


def _row(code, status):
    return {"transaction_info": {"transaction_event_code": code, "transaction_status": status}}


def test_sales_count_only_t0006_with_status_s():
    rows = [_row("T0006", "S"), _row("T0006", "S"), _row("T0006", "P"), _row("T0200", "S"), {}, {"transaction_info": None}]
    assert [counting.is_sale(r) for r in rows] == [True, True, False, False, False, False]
    assert counting.count_sales(rows) == 2


def test_count_disputes_window_is_inclusive_on_both_ends():
    norms = [{"create_time": "2026-06-30T23:59:59Z"}, {"create_time": "2026-07-01T00:00:00Z"},
             {"create_time": "2026-09-30T23:59:59Z"}, {"create_time": "2026-10-01T00:00:00Z"}]
    assert counting.count_disputes(norms, date(2026, 7, 1), date(2026, 9, 30)) == 2


def test_count_disputes_uses_the_utc_date_of_an_offset_timestamp():
    # 2026-09-30 22:00 at -05:00 is 2026-10-01 03:00 UTC: outside the window.
    assert counting.count_disputes([{"create_time": "2026-09-30T22:00:00-05:00"}], date(2026, 7, 1), date(2026, 9, 30)) == 0
    assert counting.count_disputes([{"create_time": "2026-09-30T22:00:00+0000"}], date(2026, 7, 1), date(2026, 9, 30)) == 1


def test_undated_disputes_are_not_counted_and_are_reported():
    norms = [{"create_time": None}, {}, {"create_time": "not a date"}, {"create_time": "2026-08-01T10:00:00Z"}]
    assert counting.count_disputes(norms, date(2026, 7, 1), date(2026, 9, 30)) == 1
    assert counting.count_undated(norms) == 3


def test_threshold_200_sales_2_disputes_is_100_bps_not_high_and_one_more_reaches_it():
    assert counting.rate_bps(2, 200) == 100
    assert counting.is_high_volume(200, 2) is False
    assert counting.disputes_to_threshold(200, 2) == 1
    assert counting.rate_text(100) == "1.00 %"


def test_threshold_200_sales_3_disputes_is_150_bps_high_volume():
    assert counting.rate_bps(3, 200) == 150
    assert counting.is_high_volume(200, 3) is True
    assert counting.disputes_to_threshold(200, 3) == 0
    assert counting.rate_text(150) == "1.50 %"


def test_threshold_200_sales_1_dispute_needs_two_more():
    assert counting.disputes_to_threshold(200, 1) == 2


@pytest.mark.parametrize("sales,disputes,high", [(99, 5, False), (100, 2, False), (101, 1, False),
                                                 (101, 2, True), (101, 0, False), (1000, 14, False), (1000, 15, True)])
def test_high_volume_needs_more_than_100_sales_and_at_least_1_5_percent(sales, disputes, high):
    assert counting.is_high_volume(sales, disputes) is high


def test_disputes_to_threshold_is_none_up_to_100_sales():
    assert counting.disputes_to_threshold(100, 2) is None
    assert counting.disputes_to_threshold(99, 0) is None
    assert counting.NEEDS_SALES_REASON == "needs more than 100 sales first"


def test_disputes_to_threshold_rounds_up_exactly():
    # 101 sales: 1.5 % of 101 = 1.515, so 2 disputes are needed.
    assert counting.disputes_to_threshold(101, 0) == 2
    assert counting.disputes_to_threshold(101, 1) == 1


def test_zero_sales_has_no_rate():
    assert counting.rate_bps(0, 0) is None
    assert counting.rate_bps(3, 0) is None
    assert counting.rate_text(None) == "Not available"
    assert counting.is_high_volume(0, 3) is False


def test_rate_text_rounds_down_never_up():
    assert counting.rate_bps(149, 10000) == 149
    assert counting.rate_text(149) == "1.49 %"
    assert counting.rate_text(0) == "0.00 %"


def test_cents_to_usd_text():
    assert cents_to_usd_text(1500) == "15.00"
    assert cents_to_usd_text(3000) == "30.00"
    assert cents_to_usd_text(5) == "0.05"
