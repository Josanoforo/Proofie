"""Pure adapter for the already-fetched account_cliff rows/counts; no client or database."""
from datetime import date, datetime, timezone

from copilot.features.explainable_queue.service import account_exposure_context


def _dispute(case_id, status, cents, currency, created):
    row = {"dispute_id": case_id, "status": status, "create_time": created}
    if cents is not None:
        row["amount_cents"] = cents
    if currency is not None:
        row["currency"] = currency
    return row


def test_account_exposure_separates_current_principal_from_window_counts_and_sales():
    """Expected written before execution in expected-account.md: exact totals, population, dates, duplicates and
    historical counters preserve their own units; rows with missing facts are visible as incomplete.
    """
    rows = [
        _dispute("A", "WAITING_FOR_SELLER_RESPONSE", 1200, "USD", "2026-08-15T10:00:00Z"),
        _dispute("B", "OPEN", 345, "JPY", "2026-09-10T10:00:00Z"),
        _dispute("C", "UNDER_REVIEW", 990, "MXN", "2026-06-15T10:00:00Z"),
        _dispute("D", "WAITING_FOR_BUYER_RESPONSE", None, None, "unreadable"),
        _dispute("E", "RESOLVED", 2000, "USD", "2026-08-20T10:00:00Z"),
        _dispute("F", "UNRECOGNIZED_STATUS", 400, "USD", "2026-08-22T10:00:00Z"),
        _dispute("A", "WAITING_FOR_SELLER_RESPONSE", 1200, "USD", "2026-08-15T10:00:00Z"),
        {"status": "OPEN", "amount_cents": 50, "currency": "USD", "create_time": "2026-08-23T00:00:00Z"},
    ]
    context = account_exposure_context(
        rows, sales_count=2, window_dispute_count=4,
        window_start=date(2026, 7, 1), window_end=date(2026, 9, 30),
        sales_complete=True, disputes_complete=True,
        computed_at=datetime(2026, 10, 31, 12, tzinfo=timezone.utc),
    )
    amounts = {row["currency"]: row for row in context["principal_by_currency"]}
    assert context["population_count"] == 6
    assert context["active_case_count"] == 4
    assert context["closed_case_count"] == 1
    assert context["unclassified_status_count"] == 1
    assert context["duplicate_rows"] == 1 and context["unidentified_rows"] == 1
    assert amounts["USD"]["amount_cents"] == 1200 and amounts["USD"]["case_count"] == 1
    assert amounts["JPY"]["amount_cents"] == 345
    assert amounts["MXN"]["amount_cents"] == 990
    assert context["incomplete_amount_count"] == 1
    assert context["dated_dispute_count"] == 5 and context["undated_dispute_count"] == 1
    assert context["sales_count"] == 2 and context["window_dispute_count"] == 4
    assert context["period_label"] == context["window_label"]
    assert "UTC calendar dates, inclusive" in context["period_label"]
    assert context["complete"] is False
    from jinja2 import Environment, FileSystemLoader
    fragment = Environment(loader=FileSystemLoader("copilot/web/templates")).get_template(
        "features/explainable_queue/_account_exposure.html").render(account_exposure=context)
    assert fragment.count("12.00 USD") == 1
    assert "USD: 12.00 USD" not in fragment
    assert "Period, sources and data coverage" in fragment
    assert "345 JPY" in fragment and "9.90 MXN" in fragment
    assert "Coverage incomplete" in fragment


def test_partial_account_reads_do_not_turn_unavailable_counts_into_zero():
    """Expected: partial fetch flags null their respective counts and invalidate coverage; known returned principal
    may be shown only as partial. The unrelated completed source retains its real denominator.
    """
    row = _dispute("A", "OPEN", 1200, "USD", "2026-08-15T10:00:00Z")
    context = account_exposure_context(
        [row], sales_count=99, window_dispute_count=7,
        window_start=date(2026, 7, 1), window_end=date(2026, 9, 30),
        sales_complete=False, disputes_complete=True,
        computed_at=datetime(2026, 10, 31, 12, tzinfo=timezone.utc),
    )
    assert context["sales_count"] is None
    assert context["sales_denominator_label"].startswith("not available")
    assert context["window_dispute_count"] == 7
    assert context["principal_complete"] is True
    assert context["window_complete"] is False and context["complete"] is False
    assert context["principal_label"].startswith("Known principal in")

    context = account_exposure_context(
        [row], sales_count=99, window_dispute_count=7,
        window_start=date(2026, 7, 1), window_end=date(2026, 9, 30),
        sales_complete=True, disputes_complete=False,
        computed_at=datetime(2026, 10, 31, 12, tzinfo=timezone.utc),
    )
    assert context["sales_count"] == 99
    assert context["window_dispute_count"] is None
    assert context["dispute_listing_complete"] is False
    assert context["principal_complete"] is False and context["window_complete"] is False
    assert context["principal_label"].startswith("Known principal among returned")
