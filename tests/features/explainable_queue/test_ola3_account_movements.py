from datetime import date, datetime, timezone

from copilot.features.explainable_queue.service import account_exposure_context


def _dispute(case_id, status="OPEN", amount=1250, currency="USD"):
    return {"dispute_id": case_id, "status": status, "create_time": "2026-08-20T10:00:00Z",
            "amount_cents": amount, "currency": currency}


def _transaction(tx_id, value, currency, *, code="T0006", status="S", day="2026-08-01T10:00:00Z"):
    return {"transaction_info": {"transaction_id": tx_id, "transaction_event_code": code,
                                 "transaction_status": status, "transaction_initiation_date": day,
                                 "transaction_amount": {"value": value, "currency_code": currency}}}


def _context(rows, *, rows_complete=True, sales_complete=True):
    return account_exposure_context(
        [_dispute("LNB-CASE")], sales_count=2, window_dispute_count=4,
        window_start=date(2026, 7, 1), window_end=date(2026, 9, 30),
        sales_complete=sales_complete, disputes_complete=True,
        computed_at=datetime(2026, 10, 31, 18, tzinfo=timezone.utc),
        transaction_rows=rows, transactions_complete=rows_complete,
    )


def test_only_unique_dated_completed_sale_rows_sum_by_minor_unit_currency():
    """Expected in expected.md: unique T0006/S rows only; other statuses/codes never become settled totals."""
    rows = [
        _transaction("TX-DUP", "9.00", "USD"),
        _transaction("TX-DUP", "9.00", "USD"),
        _transaction("TX-USD", "12.34", "USD", day="2026-07-02T10:00:00Z"),
        _transaction("TX-USD-2", "5.00", "USD", day="2026-07-03T10:00:00Z"),
        _transaction("TX-JPY", "250", "JPY", day="2026-08-03T10:00:00Z"),
        _transaction("TX-MXN", "12.99", "MXN", day="2026-09-04T10:00:00Z"),
        _transaction("TX-PENDING", "100.00", "USD", status="P"),
        _transaction("TX-OTHER", "4.00", "USD", code="T1100"),
        _transaction("TX-BAD-DATE", "3.00", "USD", day="unreadable"),
        _transaction("TX-BAD-AMOUNT", "1.001", "USD"),
        _transaction("TX-OUTSIDE", "2.00", "USD", day="2026-10-01T00:00:00Z"),
        {"transaction_info": {"transaction_event_code": "T0006", "transaction_status": "S",
                               "transaction_initiation_date": "2026-08-05T10:00:00Z",
                               "transaction_amount": {"value": "5.00", "currency_code": "USD"}}},
    ]
    context = _context(rows)
    movement = context["completed_sales"]
    amounts = {row["currency"]: row for row in movement["by_currency"]}
    assert set(amounts) == {"USD", "JPY", "MXN"}
    assert amounts["USD"]["amount_minor"] == 1734 and amounts["USD"]["transaction_count"] == 2
    assert amounts["USD"]["by_date"][0]["date_utc"] == "2026-07-02"
    assert amounts["JPY"]["amount_minor"] == 250 and amounts["JPY"]["amount_text"] == "250 JPY"
    assert amounts["MXN"]["amount_minor"] == 1299
    assert movement["duplicate_transaction_rows"] == 1
    assert movement["non_completed_t0006_rows"] == 1
    assert movement["unclassified_event_rows"] == 1
    assert movement["incomplete_transaction_rows"] == 4
    assert movement["unidentified_transaction_rows"] == 1
    assert movement["complete_for_returned_t0006_s"] is False
    assert context["sales_count"] == 2 and context["window_dispute_count"] == 4
    assert context["period_label"] == movement["period_label"]
    assert "settlement" in movement["scope"] and "refund" in movement["scope"]

    from jinja2 import Environment, FileSystemLoader
    html = Environment(loader=FileSystemLoader("copilot/web/templates")).get_template(
        "features/explainable_queue/_account_exposure.html").render(account_exposure=context)
    assert "Recorded completed-sale amounts" in html
    assert "T0006 rows not completed under the existing Proofie rule" in html
    assert "other event codes not classified" in html
    assert "12.34 USD" in html and "250 JPY" in html and "12.99 MXN" in html
    assert "100.00 USD" not in html


def test_partial_transaction_search_keeps_known_subtotal_labeled_partial_not_zero():
    context = _context([_transaction("TX-PARTIAL", "7.50", "USD")],
                       rows_complete=False, sales_complete=False)
    movement = context["completed_sales"]
    assert movement["by_currency"][0]["amount_minor"] == 750
    assert movement["fetch_complete"] is False
    assert movement["complete_for_returned_t0006_s"] is False
    assert "Partial Transaction Search read" in movement["coverage_label"]
    assert context["sales_count"] is None
    assert context["sales_denominator_label"].startswith("not available")
