"""fetch.py on a fake client: pagination, the op name, the allowed paths, failures as Limitations."""
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from copilot.core.disputes import PayPalError
from copilot.features.account_cliff import fetch, window

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
RANGES = window.search_ranges(date(2026, 7, 1), date(2026, 7, 20))


def _row(code="T0006", status="S"):
    return {"transaction_info": {"transaction_event_code": code, "transaction_status": status}}


def _dispute(did, created):
    return {"dispute_id": did, "status": "RESOLVED", "create_time": created,
            "dispute_amount": {"currency_code": "USD", "value": "10.00"}}


class Fake:
    def __init__(self, pages=None, disputes=None, raises=None):
        self.pages = pages or {}
        self.disputes = disputes or []
        self.raises = raises
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if self.raises is not None:
            raise self.raises
        if op == "list_disputes":
            return {"items": self.disputes}
        page = int(parse_qs(urlparse(path).query)["page"][0])
        return {"transaction_details": self.pages[page], "total_pages": len(self.pages), "page": page}


def test_two_pages_are_both_read_with_the_account_cliff_op():
    fake = Fake(pages={1: [_row(), _row("T0200")], 2: [_row("T0006", "P")]})
    result = fetch.fetch_sales_rows(fake, RANGES)
    assert result.complete and len(result.items) == 3
    assert [parse_qs(urlparse(c[1]).query)["page"][0] for c in fake.calls] == ["1", "2"]
    assert {c[2] for c in fake.calls} == {"account_cliff_search"}
    assert {c[0] for c in fake.calls} == {"GET"}
    for _m, path, _op in fake.calls:
        q = parse_qs(urlparse(path).query)
        assert urlparse(path).path == "/v1/reporting/transactions"
        assert q["fields"] == ["transaction_info"] and q["page_size"] == ["500"]
        assert q["start_date"] == ["2026-07-01T00:00:00Z"] and q["end_date"] == ["2026-07-20T23:59:59Z"]


def test_disputes_are_listed_and_normalised_through_the_pilot_function():
    fake = Fake(disputes=[_dispute("PP-X-1", "2026-08-01T10:00:00Z"), _dispute("PP-X-2", "2026-09-01T10:00:00Z")])
    result = fetch.fetch_disputes(fake, NOW)
    assert result.complete
    assert [n["dispute_id"] for n in result.items] == ["PP-X-1", "PP-X-2"]
    assert result.items[0]["create_time"] == "2026-08-01T10:00:00Z"
    assert [c[1] for c in fake.calls] == ["/v1/customer/disputes?page_size=50"] and fake.calls[0][2] == "list_disputes"


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_paypal_failure_in_search_becomes_a_limitation_not_an_exception(status):
    fake = Fake(raises=PayPalError(status, "ISSUE", "dbg", {}))
    result = fetch.fetch_sales_rows(fake, RANGES)
    assert not result.complete and result.items == ()
    assert result.limitations[0].source == "paypal_transaction_search"
    assert "dbg" not in result.limitations[0].message and "ISSUE" not in result.limitations[0].message


def test_a_paypal_failure_listing_disputes_becomes_a_limitation():
    result = fetch.fetch_disputes(Fake(raises=PayPalError(403, "NOT_AUTHORIZED", "dbg", {})), NOW)
    assert not result.complete and result.limitations[0].source == "paypal_dispute"


def test_a_timeout_becomes_a_limitation():
    result = fetch.fetch_sales_rows(Fake(raises=TimeoutError()), RANGES)
    assert not result.complete and "in time" in result.limitations[0].message


def test_a_programming_error_still_raises():
    with pytest.raises(KeyError):
        fetch.fetch_sales_rows(Fake(raises=KeyError("bug")), RANGES)
    with pytest.raises(KeyError):
        fetch.fetch_disputes(Fake(raises=KeyError("bug")), NOW)


def test_a_failure_on_the_second_page_reports_a_limitation_and_marks_the_read_incomplete():
    class Flaky(Fake):
        def call(self, method, path, op, **kw):
            if "page=2" in path:
                raise PayPalError(500, None, None, {})
            return super().call(method, path, op, **kw)
    out = fetch.fetch_sales_rows(Flaky(pages={1: [_row()], 2: [_row()]}), RANGES)
    assert not out.complete and len(out.items) == 1
