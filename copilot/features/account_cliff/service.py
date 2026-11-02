"""One computation from sandbox reads to the summary and the inbox line. Every number comes from code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from copilot.core.context import SRC_DISPUTE, Limitation
from copilot.features.account_cliff import counting, fetch, window
from copilot.features.account_cliff.rules import (HIGH_VOLUME_FEE_CENTS, HIGH_VOLUME_MIN_SALES, HIGH_VOLUME_RATE_BPS,
                                                  STANDARD_FEE_CENTS, WINDOW_MONTHS)

CACHE_TTL = timedelta(minutes=10)
_cache: tuple[datetime, "CliffSummary"] | None = None


@dataclass(frozen=True)
class CliffSummary:
    window_start: date
    window_end: date
    sales: int | None
    disputes: int | None
    rate_bps: int | None
    high_volume: bool | None
    disputes_to_threshold: int | None
    fee_now_cents: int | None
    fee_if_high_cents: int
    limitations: tuple
    computed_at: datetime
    account_exposure: dict | None = None
    account_exposure_error: bool = False


def compute(client, today: date | None = None, now: datetime | None = None, *, include_exposure: bool = False) -> CliffSummary:
    now = now or datetime.now(timezone.utc)
    today = today or now.astimezone(timezone.utc).date()
    start, end = window.previous_full_months(today, WINDOW_MONTHS.value)
    sales_result = fetch.fetch_sales_rows(client, window.search_ranges(start, end))
    disputes_result = fetch.fetch_disputes(client, now)
    limits = list(sales_result.limitations) + list(disputes_result.limitations)
    sales = counting.count_sales(list(sales_result.items)) if sales_result.complete else None
    disputes = None
    norms = list(disputes_result.items)
    if disputes_result.complete:
        disputes = counting.count_disputes(norms, start, end)
        undated = counting.count_undated(norms)
        if undated:
            limits.append(Limitation(SRC_DISPUTE, f"{undated} dispute(s) have no readable creation date and are "
                                                  f"not counted in the window."))
    known = sales is not None and disputes is not None
    rate = counting.rate_bps(disputes, sales) if known else None
    high = counting.is_high_volume(sales, disputes) if known else None
    to_threshold = counting.disputes_to_threshold(sales, disputes) if known else None
    fee_now = (HIGH_VOLUME_FEE_CENTS.value if high else STANDARD_FEE_CENTS.value) if known else None
    exposure, exposure_error = None, False
    if include_exposure:
        from copilot.features.explainable_queue.service import account_exposure_context
        try:
            exposure = account_exposure_context(
                norms, sales_count=sales, window_dispute_count=disputes,
                window_start=start, window_end=end, sales_complete=sales_result.complete,
                disputes_complete=disputes_result.complete, transaction_rows=sales_result.items,
                transactions_complete=sales_result.complete, computed_at=now)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            exposure_error = True
    return CliffSummary(start, end, sales, disputes, rate, high, to_threshold, fee_now,
                        HIGH_VOLUME_FEE_CENTS.value, tuple(limits), now, exposure, exposure_error)


def line_text(s: CliffSummary) -> str:
    """The one inbox line, English, built by code. Three cases; the numbers come from the rule constants and the counts,
    never from the model. 'May apply' is as far as it goes: PayPal decides which fee a dispute carries."""
    label = "High-volume dispute fee"
    threshold, months = HIGH_VOLUME_MIN_SALES.value, WINDOW_MONTHS.value
    if s.sales is None or (s.sales > threshold and s.disputes is None):
        return f"{label}: could not be checked. PayPal's sales records could not be read completely."
    counted = f"Proofie counted {s.sales} sales in PayPal's records for that period."
    if s.sales <= threshold:
        return (f"{label}: not reached. PayPal applies it only above {threshold} sales in {months} full months. "
                f"{counted}")
    if s.high_volume:
        return f"{label}: may apply. {counted}"
    return (f"{label}: not reached. PayPal applies it only above {threshold} sales in {months} full months and at a "
            f"dispute rate of {counting.rate_text(HIGH_VOLUME_RATE_BPS.value)} or more. "
            f"Proofie counted {s.sales} sales and {s.disputes} disputes in PayPal's records for that period.")


def clear_cache() -> None:
    global _cache
    _cache = None


def inbox_line(client, now: datetime | None = None) -> str | None:
    """The inbox line, or None when anything goes wrong (the inbox never breaks because of this line).
    Cached in-process for 10 minutes by code: the inbox already calls PayPal on every load, and this line would
    add a full Transaction Search sweep to each one. One module-level pair, not a per-client key: the app builds a
    new client per request and serves one local seller account. Summaries with limitations are not cached, so a
    failed read is retried on the next load."""
    global _cache
    try:
        now = now or datetime.now(timezone.utc)
        if _cache is not None and now - _cache[0] < CACHE_TTL:
            return line_text(_cache[1])
        summary = compute(client, now=now)
        _cache = (now, summary) if not summary.limitations else None
        return line_text(summary)
    except Exception:  # noqa: BLE001 - by design: see docstring
        return None
