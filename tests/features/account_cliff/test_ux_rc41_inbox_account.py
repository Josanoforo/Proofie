"""The account fee line on the inbox and the account page. Moved from tests/local/test_ux_rc41_inbox.py at integration: it imports
copilot.features.account_cliff, so it lives with that feature's tests (the flags-off public export excludes the feature together
with its tests; tools/test_export_public.py dependency closure). The assertions are unchanged.
"""
from datetime import datetime, timedelta
from html import unescape

import pytest
from _dom import parse  # noqa: F401
from _hosted import authenticated_client  # noqa: F401
import _ux_lint as lint  # noqa: F401

from copilot.app import create_app  # noqa: F401
from copilot.core import disputes as core_disputes  # noqa: F401
from copilot.web.routes_disputes import get_client  # noqa: F401
from test_ux_rc41_inbox import Fake, UTC, item  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


def account_page(monkeypatch, sales, disputes=2, fail=False):
    class AccountFake(Fake):
        searches = 0

        def call(self, method, path, op, **kw):
            if op == "account_cliff_search":
                if fail:
                    raise core_disputes.PayPalError(500, None, None, {})
                from copilot.features.account_cliff import window
                start, _end = window.previous_full_months(datetime.now(UTC).date(), 3)
                rows = [{"transaction_info": {"transaction_event_code": "T0006", "transaction_status": "S"}}] * sales
                return {"transaction_details": rows if f"start_date={start.isoformat()}" in path else [], "total_pages": 1}
            if op == "list_disputes":
                created = (datetime.now(UTC).replace(day=1) - timedelta(days=20)).strftime("%Y-%m-%dT10:00:00Z")
                return {"items": [item(f"PP-H{n}", "RESOLVED", links=[]) | {"create_time": created} for n in range(disputes)]
                        + [item("PP-A", due_hours=100)], "links": []}
            return super().call(method, path, op, **kw)
    monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF", "true")
    from copilot.features.account_cliff import service
    service.clear_cache()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: AccountFake([])
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    return web.get("/account").text, web.get("/disputes").text


def test_account_fee_line_three_cases_and_numbers_from_the_counts(monkeypatch):
    account, inbox = account_page(monkeypatch, sales=0, disputes=0)
    assert "High-volume dispute fee: not reached. PayPal applies it only above 100 sales in 3 full months." in inbox
    assert "Proofie counted 0 sales in PayPal&#39;s records for that period." in inbox
    account, inbox = account_page(monkeypatch, sales=0, disputes=0, fail=True)
    assert "High-volume dispute fee: could not be checked." in unescape(inbox)
    assert "sales records could not be read completely" in unescape(inbox)
    account, inbox = account_page(monkeypatch, sales=300, disputes=6)
    assert "High-volume dispute fee: may apply. Proofie counted 300 sales" in unescape(inbox)


def test_inbox_has_at_most_one_banner_above_the_table_and_the_fee_line_below_it(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")
    account, inbox = account_page(monkeypatch, sales=0, disputes=0)
    dom = parse(inbox)
    assert inbox.count('class="banner ') <= 1
    assert inbox.index('data-table="inbox"') < inbox.index('class="account-cliff"') < inbox.index("Tracking at the sale: files")
    assert any(a.text() == "Fee details" and a.attrs["href"] == "/account" for a in dom.find_all("a"))


def test_account_page_has_breadcrumb_hint_captions_and_no_back_link(monkeypatch):
    account, _ = account_page(monkeypatch, sales=0, disputes=0)
    dom = parse(account)
    assert lint.breadcrumb_items(account) == [("All disputes", "/disputes"), ("Account and dispute fees", None)]
    assert lint.back_links(account) == []
    assert "Counted period" in account and "Dates in UTC, as PayPal counts them." in account
    tables = dom.find_all("table")
    assert len(tables) == 2 and all(t.find("caption") is not None for t in tables)
    assert dom.find("title").text() == "Account and dispute fees - Proofie"


def test_the_constants_table_stacks_with_a_label_on_every_cell_and_aligns_its_values(monkeypatch):
    """The 3-column constants table overflowed at 320 px; it is a stack table now (CSS turns each row into a card and prints each
    cell's data-label), and the numeric Value cells are amount cells."""
    account, _ = account_page(monkeypatch, sales=0, disputes=0)
    table = parse(account).find("table", class_="account-cliff-constants")
    assert "stack" in table.attrs["class"].split()
    rows = [r for r in table.find_all("tr") if "data-constant" in r.attrs]
    assert rows
    from copilot.features.account_cliff.rules import ALL_CONSTANTS
    assert len(rows) == len(ALL_CONSTANTS)
    for row, constant in zip(rows, ALL_CONSTANTS):
        assert [c.attrs.get("data-label") for c in row.find_all("td")] == ["Constant", "Value", "Source"]
        value_cell = row.find_all("td")[1]
        numeric = isinstance(constant.value, (int, float))
        assert ("amount" in value_cell.attrs.get("class", "").split()) == numeric, constant.name    # only numeric or money cells
    assert any(isinstance(c.value, str) for c in ALL_CONSTANTS)          # the currency row is text and stays left-aligned
    heads = table.find_all("th")
    assert [h.attrs["scope"] for h in heads] == ["col", "col", "col"] and "amount" in heads[1].attrs["class"].split()
