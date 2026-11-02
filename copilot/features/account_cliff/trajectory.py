"""The dated path to the high-volume dispute rule (D-101 P3). Reads only, no AI, no float: every number is integer
arithmetic on PayPal's own transaction and dispute reads, every constant comes from rules.py with its source.

PayPal judges a case opened in month M by the three full calendar months before M (WINDOW_MONTHS, Help 350). So the
next thing to date is the window for cases opened NEXT month: the two full months already past plus the current month.
The rest of the current month is projected at the pace of the last 30 days (PACE_DAYS), not at the pace of the days of
the month so far: on the 1st of a month one day of data is not a pace. A figure that cannot be computed is None and the
page shows "Not available"; nothing is guessed."""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from copilot.core.context import SRC_DISPUTE, SRC_SEARCH, Limitation
from copilot.features.account_cliff import counting, fetch, window
from copilot.features.account_cliff.rules import (HIGH_VOLUME_FEE_CENTS, HIGH_VOLUME_MIN_SALES, HIGH_VOLUME_RATE_BPS,
                                                  SOURCE_FEES_PAGE, SOURCE_HELP350, STANDARD_FEE_CENTS,
                                                  WINDOW_MONTHS, cents_to_usd_text)

logger = logging.getLogger(__name__)
UNKNOWN, BELOW_MINIMUM, ALREADY_OVER, NO_PACE, NOT_THIS_MONTH, DATED = (
    "unknown", "below_minimum", "already_over", "no_pace", "not_this_month", "dated")
EXTRA_FEE_PER_DISPUTE_CENTS = HIGH_VOLUME_FEE_CENTS.value - STANDARD_FEE_CENTS.value
PACE_DAYS = 30  # the pace is the last 30 days, today included
NOT_AVAILABLE = "Not available"


@dataclass(frozen=True)
class CliffPath:
    state: str
    today: date
    window_start: date | None = None          # first day of the oldest month in the next window
    window_end: date | None = None            # last day of the current month
    applies_from: date | None = None          # first day of next month: cases opened from then use this window
    pace_start: date | None = None            # first day of the 30-day pace
    sales_counted: int | None = None          # sales in the window so far
    sales_projected: int | None = None        # with the rest of this month at the 30-day pace
    disputes_counted: int | None = None       # disputes created in the window so far
    pace_sales: int | None = None             # sales in the last 30 days
    pace_disputes: int | None = None          # disputes created in the last 30 days
    disputes_to_cross: int | None = None
    days_to_cross: int | None = None
    cross_date: date | None = None
    expected_next_month: int | None = None    # whole disputes next month at the 30-day pace
    extra_fee_per_dispute_cents: int = EXTRA_FEE_PER_DISPUTE_CENTS
    extra_fee_next_month_cents: int | None = None
    limitations: tuple = ()
    computed_at: datetime | None = None


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def project(*, today: date, sales_prev: int, sales_month: int, disputes_prev: int, disputes_month: int,
            pace_sales: int, pace_disputes: int, limitations=(), computed_at: datetime | None = None) -> CliffPath:
    """The pure part: counts in, path out. `prev` = the full months already past that stay in the next window, `month` =
    the current month so far (today included), `pace_*` = the last 30 days (today included)."""
    month_end = _month_end(today)
    in_month, remaining = month_end.day, (month_end - today).days
    start, _ = window.previous_full_months(today, WINDOW_MONTHS.value - 1)
    applies_from = month_end + timedelta(days=1)
    counted = sales_prev + sales_month
    sales_projected = counted + pace_sales * remaining // PACE_DAYS
    disputes_counted = disputes_prev + disputes_month
    common = dict(today=today, window_start=start, window_end=month_end, applies_from=applies_from,
                  pace_start=today - timedelta(days=PACE_DAYS - 1), sales_counted=counted,
                  sales_projected=sales_projected, disputes_counted=disputes_counted, pace_sales=pace_sales,
                  pace_disputes=pace_disputes, limitations=tuple(limitations), computed_at=computed_at)
    if sales_projected <= HIGH_VOLUME_MIN_SALES.value:
        return CliffPath(BELOW_MINIMUM, **common)
    expected = pace_disputes * in_month // PACE_DAYS
    extra = dict(expected_next_month=expected, extra_fee_next_month_cents=expected * EXTRA_FEE_PER_DISPUTE_CENTS)
    n = counting.disputes_to_threshold(sales_projected, disputes_counted)
    if n == 0:
        return CliffPath(ALREADY_OVER, disputes_to_cross=0, days_to_cross=0, **extra, **common)
    if pace_disputes == 0:
        return CliffPath(NO_PACE, disputes_to_cross=n, **extra, **common)
    days = -(-n * PACE_DAYS // pace_disputes)
    cross = today + timedelta(days=days)
    state = DATED if cross <= month_end else NOT_THIS_MONTH
    return CliffPath(state, disputes_to_cross=n, days_to_cross=days, cross_date=cross if state == DATED else None,
                     **extra, **common)


def compute_path(client, today: date | None = None, now: datetime | None = None) -> CliffPath:
    """Two reads (Transaction Search over the next window so far, the disputes list), then `project`. A read that fails
    in any way makes the whole path unknown: a partial count would put a wrong date on the page."""
    now = now or datetime.now(timezone.utc)
    today = today or now.astimezone(timezone.utc).date()
    prev_start, prev_end = window.previous_full_months(today, WINDOW_MONTHS.value - 1)
    month_start = today.replace(day=1)
    pace_start = today - timedelta(days=PACE_DAYS - 1)
    sales_result = fetch.fetch_sales_rows(client, window.search_ranges(prev_start, today))
    disputes_result = fetch.fetch_disputes(client, now)
    limits = list(sales_result.limitations) + list(disputes_result.limitations)
    if not (sales_result.complete and disputes_result.complete):
        return CliffPath(UNKNOWN, today, limitations=tuple(limits), computed_at=now)
    sales_prev = sales_month = pace_sales = undated_sales = 0
    for row in sales_result.items:
        if not counting.is_sale(row):
            continue
        d = counting.sale_date(row)
        if d is None:
            undated_sales += 1
            continue
        if prev_start <= d <= prev_end:
            sales_prev += 1
        elif month_start <= d <= today:
            sales_month += 1
        if pace_start <= d <= today:
            pace_sales += 1
    norms = list(disputes_result.items)
    undated = counting.count_undated(norms)
    if undated_sales:
        limits.append(Limitation(SRC_SEARCH, f"{undated_sales} sale(s) have no readable date and are not counted."))
    if undated:
        limits.append(Limitation(SRC_DISPUTE, f"{undated} dispute(s) have no readable creation date and are not counted."))
    return project(today=today, sales_prev=sales_prev, sales_month=sales_month,
                   disputes_prev=counting.count_disputes(norms, prev_start, prev_end),
                   disputes_month=counting.count_disputes(norms, month_start, today),
                   pace_sales=pace_sales, pace_disputes=counting.count_disputes(norms, pace_start, today),
                   limitations=limits, computed_at=now)


def compute_path_safe(client, today: date | None = None, now: datetime | None = None) -> CliffPath:
    """What the page calls: `compute_path`, but nothing it raises can turn /account into a 500. An unexpected failure
    (a body PayPal shaped differently, a bug) becomes the unknown state with a plain reason and is logged for the host."""
    try:
        return compute_path(client, today, now)
    except Exception:  # noqa: BLE001 - the section is an addition to the page; it must never take the page down
        logger.exception("account_cliff path: unexpected failure, showing it as unknown")
        now = now or datetime.now(timezone.utc)
        return CliffPath(UNKNOWN, today or now.astimezone(timezone.utc).date(), computed_at=now, limitations=(
            Limitation(SRC_SEARCH, "The path could not be worked out because PayPal's reply was not in the expected shape."),))


def _n(value: int | None) -> str:
    return NOT_AVAILABLE if value is None else str(value)


def _usd(cents: int | None) -> str:
    return NOT_AVAILABLE if cents is None else f"{cents_to_usd_text(cents)} USD"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def headline(p: CliffPath) -> str:
    """One English sentence, built by code. 'May apply' is as far as it goes: PayPal decides which fee a dispute carries."""
    minimum, bps = HIGH_VOLUME_MIN_SALES.value, HIGH_VOLUME_RATE_BPS.value
    rate = f"{bps // 100}.{bps % 100:02d} %"
    if p.state == UNKNOWN:
        return "Path to the cliff: could not be worked out. PayPal's sales or dispute records could not be read completely."
    where = f"For cases opened from {p.applies_from.isoformat()}"
    if p.state == BELOW_MINIMUM:
        return (f"{where}, the high-volume fee would not apply: the window is projected at {p.sales_projected} sales and "
                f"the rule needs more than {minimum}.")
    if p.state == ALREADY_OVER:
        return (f"{where}, the high-volume fee may apply: {p.disputes_counted} disputes against a projected "
                f"{p.sales_projected} sales is already at or above {rate}.")
    need = _plural(p.disputes_to_cross, "more dispute", "more disputes")
    pace = f"At the pace of the last {PACE_DAYS} days ({_plural(p.pace_disputes, 'dispute', 'disputes')})"
    if p.state == NO_PACE:
        return (f"{where}, {need} would reach {rate}. No dispute was created in the last {PACE_DAYS} days, so the crossing "
                f"cannot be dated.")
    if p.state == NOT_THIS_MONTH:
        return (f"{where}, {need} would reach {rate}. {pace}, they would not arrive before {p.applies_from.isoformat()}.")
    return (f"{pace}, {need} would reach {rate} on {p.cross_date.isoformat()}, in "
            f"{_plural(p.days_to_cross, 'day', 'days')}. {where}, the high-volume fee may apply.")


def rows(p: CliffPath) -> list[tuple[str, str]]:
    """Label and value pairs for the page, built by code. Unknown figures read 'Not available'."""
    shown = p.state != UNKNOWN
    crossing = p.state in (ALREADY_OVER, NO_PACE, NOT_THIS_MONTH, DATED)
    if p.state == DATED:
        days = f"{p.days_to_cross} (on {p.cross_date.isoformat()})"
    elif p.state == ALREADY_OVER:
        days = "0 (already at or above)"
    else:
        days = NOT_AVAILABLE
    window_text = (f"{p.window_start.isoformat()} to {p.window_end.isoformat()}, the rest of this month projected at the "
                   f"{PACE_DAYS}-day pace" if shown else NOT_AVAILABLE)
    return [
        ("Window for cases opened from", p.applies_from.isoformat() if shown else NOT_AVAILABLE),
        ("Window counted", window_text),
        ("Sales so far / projected", f"{p.sales_counted} / {p.sales_projected}" if shown else NOT_AVAILABLE),
        ("Disputes created in the window so far", _n(p.disputes_counted)),
        (f"Pace: sales / disputes in the last {PACE_DAYS} days",
         f"{p.pace_sales} / {p.pace_disputes}" if shown else NOT_AVAILABLE),
        ("More disputes to reach the threshold", _n(p.disputes_to_cross) if crossing else NOT_AVAILABLE),
        ("Days until then, at the 30-day pace", days),
        ("Extra fee per dispute if the high-volume fee applies", _usd(p.extra_fee_per_dispute_cents)),
        ("Extra fee next month at this pace", _usd(p.extra_fee_next_month_cents) if crossing else NOT_AVAILABLE),
    ]


def notes(p: CliffPath) -> list[str]:
    out = [f"Rule: more than {HIGH_VOLUME_MIN_SALES.value} sales and a dispute rate of "
           f"{HIGH_VOLUME_RATE_BPS.value // 100}.{HIGH_VOLUME_RATE_BPS.value % 100:02d} % or more over the {WINDOW_MONTHS.value} "
           f"full months before the case ({SOURCE_HELP350}).",
           f"Fees: {cents_to_usd_text(STANDARD_FEE_CENTS.value)} USD standard, {cents_to_usd_text(HIGH_VOLUME_FEE_CENTS.value)} USD "
           f"high volume ({SOURCE_FEES_PAGE}); the difference is the extra fee."]
    if p.state != UNKNOWN:
        out.append(f"Pace: {p.pace_start.isoformat()} to {p.today.isoformat()} (UTC). Counts are PayPal's own records up to "
                   "today; today's records may still be incomplete. The path is an estimate at that pace, not a forecast of sales.")
    return out
