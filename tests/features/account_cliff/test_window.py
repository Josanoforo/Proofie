from datetime import date, timedelta

import pytest

from copilot.features.account_cliff import window


def test_window_for_2026_10_09_is_july_to_september():
    assert window.previous_full_months(date(2026, 10, 9), 3) == (date(2026, 7, 1), date(2026, 9, 30))


def test_window_in_january_reaches_back_into_the_previous_year():
    assert window.previous_full_months(date(2026, 1, 15), 3) == (date(2025, 10, 1), date(2025, 12, 31))


def test_window_in_february_and_leap_year_end():
    assert window.previous_full_months(date(2026, 2, 1), 3) == (date(2025, 11, 1), date(2026, 1, 31))
    assert window.previous_full_months(date(2024, 3, 31), 1) == (date(2024, 2, 1), date(2024, 2, 29))
    assert window.previous_full_months(date(2026, 3, 1), 3) == (date(2025, 12, 1), date(2026, 2, 28))


def test_first_and_last_day_of_the_month_give_the_same_window():
    assert window.previous_full_months(date(2026, 10, 1), 3) == window.previous_full_months(date(2026, 10, 31), 3)


@pytest.mark.parametrize("start,end", [(date(2026, 7, 1), date(2026, 9, 30)), (date(2025, 10, 1), date(2025, 12, 31)),
                                       (date(2025, 11, 1), date(2026, 1, 31)), (date(2026, 7, 1), date(2026, 7, 1))])
def test_ranges_cover_the_window_exactly_once_and_stay_within_31_days(start, end):
    ranges = window.search_ranges(start, end)
    covered = []
    for a, b in ranges:
        assert b - a <= timedelta(days=31)
        assert (a.hour, a.minute, a.second) == (0, 0, 0) and (b.hour, b.minute, b.second) == (23, 59, 59)
        assert a.utcoffset() == timedelta(0)
        d = a.date()
        while d <= b.date():
            covered.append(d)
            d += timedelta(days=1)
    expected = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    assert covered == expected      # every day once, in order, no gap, no overlap
