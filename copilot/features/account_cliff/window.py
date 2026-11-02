"""The look-back window. All dates are UTC calendar dates (no local time zone is applied anywhere)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

MAX_RANGE_DAYS = 31  # Transaction Search accepts at most 31 days per request


def previous_full_months(today: date, months: int) -> tuple[date, date]:
    """First and last day (both inclusive) of the `months` full calendar months before the month of `today`.
    UTC calendar dates: the caller passes the UTC date. 2026-10-09, 3 -> (2026-07-01, 2026-09-30)."""
    index = today.year * 12 + (today.month - 1) - months
    start = date(index // 12, index % 12 + 1, 1)
    last = today.replace(day=1) - timedelta(days=1)
    return start, last


def search_ranges(start: date, end: date) -> list[tuple[datetime, datetime]]:
    """Split start..end (inclusive dates) into disjoint UTC ranges of at most 31 days each, from 00:00:00 of the
    first day to 23:59:59 of the last, covering every second of the window exactly once."""
    ranges: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=MAX_RANGE_DAYS - 1), end)
        ranges.append((datetime(cursor.year, cursor.month, cursor.day, tzinfo=timezone.utc),
                       datetime(stop.year, stop.month, stop.day, 23, 59, 59, tzinfo=timezone.utc)))
        cursor = stop + timedelta(days=1)
    return ranges
