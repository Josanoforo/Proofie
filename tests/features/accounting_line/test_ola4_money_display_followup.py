"""Focused P6 display and capture-selector regression; all records are synthetic."""
from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.core.context import Field, SRC_SEARCH, TransactionContext
from copilot.features.accounting_line import routes as accounting
from copilot.features.accounting_line import service
from copilot.features.outcome.read import OutcomeRead
from copilot.web.routes_disputes import get_client


CASE = "PP-R-DEMO-0000001"
CAPTURE_A = "12345678901234567"
CAPTURE_B = "76543210987654321"


def test_closure_money_display_csv_contract_and_capture_breadcrumb(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network is forbidden in this fixture")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "fixture.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_RULES_COUNTRY", "US")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    for feature in ("accounting_line", "closure_summary"):
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature.upper()}", "true")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: object()
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))

    state = {"cents": 4999, "currency": "USD", "refund": 250, "refund_currency": "USD",
             "fee": 1500, "captures": [CAPTURE_A]}
    counts = {"outcome": 0, "context": 0}

    def read_outcome(_client, _case):
        counts["outcome"] += 1
        norm = {"dispute_id": CASE, "seller_transaction_ids": list(state["captures"]),
                "create_time": "2026-10-02T12:00:00Z", "amount_cents": 1250, "currency": "USD"}
        return OutcomeRead("CLOSED", "RESOLVED", "RESOLVED_WITH_PAYOUT", state["refund"], True,
                           (), False, None, norm=norm,
                           amount_refunded_currency=state["refund_currency"])

    def fetch_context(_client, norm):
        counts["context"] += 1
        assert norm["seller_transaction_ids"] == [state["captures"][0]]
        fields = {}
        if state["cents"] is not None:
            fields["total_amount_cents"] = Field(state["cents"], SRC_SEARCH)
        if state["currency"]:
            fields["total_currency"] = Field(state["currency"], SRC_SEARCH)
        return TransactionContext(fields)

    monkeypatch.setattr(accounting, "read_outcome", read_outcome)
    monkeypatch.setattr(accounting, "fetch_context", fetch_context)
    monkeypatch.setattr(accounting.ledger, "read", lambda *_args, **_kwargs: {"records": [], "limitations": []})
    monkeypatch.setattr(accounting.account_cliff, "compute",
                        lambda *_args, **_kwargs: SimpleNamespace(fee_now_cents=state["fee"]))

    usd = client.get(f"/disputes/{CASE}/accounting-line", params={"capture_id": CAPTURE_A})
    assert usd.status_code == 200
    assert f'href="/disputes/{CASE}"' in usd.text
    assert 'href="/disputes/"' not in usd.text
    assert "49.99 USD" in usd.text, usd.text[usd.text.find('data-state="closure-summary"'):][:1800]
    assert "2.50 USD" in usd.text
    assert "15.00 USD" in usd.text
    assert "Transaction amount (minor units)" not in usd.text
    assert "opening the legacy CSV URL" not in usd.text
    uri = usd.text.split('href="data:text/csv;charset=utf-8;base64,', 1)[1].split('"', 1)[0]
    csv_bytes = base64.b64decode(uri)
    expected_row = service.row(CASE, CAPTURE_A, 4999, "USD", 250, "USD", 1500, "US", 0,
                               closure_summary=True, sale_amount_source=SRC_SEARCH,
                               sale_currency_source=SRC_SEARCH, outcome_code="RESOLVED_WITH_PAYOUT")
    assert tuple(expected_row) == service.CLOSURE_HEADER
    assert csv_bytes == service.csv_text(expected_row).encode("utf-8")
    assert b"4999" in csv_bytes and b"250" in csv_bytes and b"1500" in csv_bytes

    state.update(cents=250, currency="JPY", refund=250, refund_currency="JPY", fee=None)
    jpy = client.get(f"/disputes/{CASE}/accounting-line", params={"capture_id": CAPTURE_A})
    assert jpy.status_code == 200 and "250 JPY" in jpy.text
    assert "250.00 JPY" not in jpy.text

    state.update(cents=None, currency=None, refund=None, refund_currency=None, fee=None)
    unknown = client.get(f"/disputes/{CASE}/accounting-line", params={"capture_id": CAPTURE_A})
    assert unknown.status_code == 200
    for text in ("Transaction amount</dt><dd>Unknown", "Refund returned</dt><dd>Unknown", "Fee estimate</dt><dd>Unknown"):
        assert text in unknown.text
    unknown_uri = unknown.text.split('href="data:text/csv;charset=utf-8;base64,', 1)[1].split('"', 1)[0]
    unknown_row = service.row(CASE, CAPTURE_A, None, None, None, None, None, "US", 0,
                              closure_summary=True, outcome_code="RESOLVED_WITH_PAYOUT")
    assert base64.b64decode(unknown_uri) == service.csv_text(unknown_row).encode("utf-8")
    assert unknown_row["sale_amount_cents"] == unknown_row["paypal_refund_cents"] == ""

    state["captures"] = [CAPTURE_A, CAPTURE_B]
    before = dict(counts)
    choice = client.get(f"/disputes/{CASE}/accounting-line")
    assert choice.status_code == 200
    assert f'href="/disputes/{CASE}"' in choice.text
    assert 'href="/disputes/"' not in choice.text
    assert counts["outcome"] == before["outcome"] + 1
    assert counts["context"] == before["context"]
