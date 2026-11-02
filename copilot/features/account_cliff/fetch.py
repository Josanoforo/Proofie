"""Reads only, through the injected client (`client.call("GET", path, op)`, the pilot SandboxClient interface).
No other HTTP code and no host string here. Every expected PayPal failure becomes a Limitation; programming
errors still raise."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from copilot.core import paypal_words
from copilot.core.context import SRC_DISPUTE, SRC_SEARCH, Limitation
from copilot.core.disputes import fetch_all_disputes, normalize

SEARCH_OP = "account_cliff_search"
SEARCH_PATH = "/v1/reporting/transactions"
MAX_PAGES_PER_RANGE = 50  # local safety cap; hitting it is reported, never silently truncated


@dataclass(frozen=True)
class FetchResult:
    items: tuple
    limitations: tuple = ()

    @property
    def complete(self) -> bool:
        return not self.limitations


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure_message(exc: Exception, what: str) -> str | None:
    """Plain-English limitation for an expected failure; None for a programming error."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        if status in (401, 403):
            return f"PayPal did not allow this app to read {what} (the app may be missing that permission)."
        return f"PayPal could not return {what} right now. Try again in a few minutes."
    if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return f"PayPal did not answer in time when reading {what}."
    return None


def fetch_sales_rows(client, ranges) -> FetchResult:
    """Transaction Search rows (fields=transaction_info) for every range, following total_pages. A failure in any
    range stops the read and is reported: the count would be partial, so the caller must not use it."""
    what = "the transactions in the window"
    rows: list[dict] = []
    for start, end in ranges:
        page, total_pages = 1, 1
        while page <= total_pages:
            if page > MAX_PAGES_PER_RANGE:
                return FetchResult(tuple(rows), (Limitation(SRC_SEARCH, f"Stopped after {MAX_PAGES_PER_RANGE} pages "
                                                                        f"of transactions without reaching the last one."),))
            q = urlencode({"start_date": _fmt(start), "end_date": _fmt(end), "fields": "transaction_info",
                           "page_size": 500, "page": page})
            try:
                body = client.call("GET", f"{SEARCH_PATH}?{q}", SEARCH_OP)
            except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
                msg = _failure_message(exc, what)
                if msg is None:
                    raise
                return FetchResult(tuple(rows), (Limitation(SRC_SEARCH, msg, paypal_words.technical_of(exc)),))
            body = body if isinstance(body, dict) else {}
            rows.extend(body.get("transaction_details") or [])
            total_pages = body.get("total_pages") if isinstance(body.get("total_pages"), int) else 1
            page += 1
    return FetchResult(tuple(rows))


def fetch_disputes(client, now: datetime | None = None) -> FetchResult:
    """All disputes the account lists, normalised."""
    what = "the list of disputes"
    now = now or datetime.now(timezone.utc)
    try:
        items, _pages = fetch_all_disputes(client)
    except Exception as exc:  # noqa: BLE001
        msg = _failure_message(exc, what)
        if msg is None and type(exc) is RuntimeError:
            msg = f"PayPal could not return {what} completely (too many pages)."
        if msg is None:
            raise
        return FetchResult((), (Limitation(SRC_DISPUTE, msg, paypal_words.technical_of(exc)),))
    return FetchResult(tuple(normalize(raw, now) for raw in items))
