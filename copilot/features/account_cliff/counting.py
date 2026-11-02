"""Pure counting and threshold arithmetic. No I/O, no float: exact integer maths."""
from __future__ import annotations

from datetime import date, datetime, timezone

from copilot.features.account_cliff.rules import HIGH_VOLUME_MIN_SALES, HIGH_VOLUME_RATE_BPS

NEEDS_SALES_REASON = "needs more than 100 sales first"


def is_sale(row: dict) -> bool:
    """A completed sale: T0006 (PayPal checkout payment) with status S, the sales denominator."""
    info = (row or {}).get("transaction_info") or {}
    return info.get("transaction_event_code") == "T0006" and info.get("transaction_status") == "S"


def count_sales(rows: list[dict]) -> int:
    return sum(1 for r in rows if is_sale(r))


def dispute_date(norm: dict) -> date | None:
    """UTC calendar date of the dispute's create_time, None when missing or unreadable."""
    return utc_date((norm or {}).get("create_time"))


def sale_date(row: dict) -> date | None:
    """UTC calendar date a transaction row was initiated, None when missing or unreadable."""
    return utc_date(((row or {}).get("transaction_info") or {}).get("transaction_initiation_date"))


def utc_date(raw) -> date | None:
    """UTC calendar date of a PayPal timestamp string, None when missing or unreadable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif len(text) > 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = text[:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def count_disputes(norms: list[dict], start: date, end: date) -> int:
    """Disputes whose create_time UTC date falls in start..end, both inclusive."""
    count = 0
    for norm in norms:
        d = dispute_date(norm)
        if d is not None and start <= d <= end:
            count += 1
    return count


def count_undated(norms: list[dict]) -> int:
    """Disputes that cannot be placed in any window because create_time is missing or unreadable."""
    return sum(1 for n in norms if dispute_date(n) is None)


def rate_bps(disputes: int, sales: int) -> int | None:
    """Disputes per 10 000 sales, rounded down; None when there are no sales."""
    if sales <= 0:
        return None
    return disputes * 10000 // sales


def is_high_volume(sales: int, disputes: int) -> bool:
    return sales > HIGH_VOLUME_MIN_SALES.value and disputes * 10000 >= HIGH_VOLUME_RATE_BPS.value * sales


def disputes_to_threshold(sales: int, disputes: int) -> int | None:
    """Smallest n >= 0 with (disputes + n) * 10000 >= 150 * sales. None when sales <= 100 (NEEDS_SALES_REASON)."""
    if sales <= HIGH_VOLUME_MIN_SALES.value:
        return None
    needed = -(-HIGH_VOLUME_RATE_BPS.value * sales // 10000)
    return max(0, needed - disputes)


def rate_text(bps: int | None) -> str:
    """150 -> "1.50 %"; None -> "Not available"."""
    if bps is None:
        return "Not available"
    return f"{bps // 100}.{bps % 100:02d} %"
