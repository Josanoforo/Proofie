"""Opt-in P6 before/after route renders; never pulled into the default suite/browser stack."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.environ.get("OLA4_P6_CAPTURE") not in {"baseline-matched", "after", "after-money"},
                                reason="Set OLA4_P6_CAPTURE explicitly for a P6 capture.")


def test_ola4_p6_same_fixture_fullpage(monkeypatch, tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright

    from copilot import config
    from copilot.app import create_app
    from copilot.core.context import Field, SRC_SEARCH, TransactionContext
    from copilot.features.accounting_line import routes as accounting
    from copilot.features.fulfilment import store
    from copilot.features.outcome.read import OutcomeRead
    from copilot.features.sale_lesson import routes as lesson_routes
    from copilot.features.sale_lesson import service as lesson_service
    from copilot.features.trade_profiles import service as profile_service
    from copilot.web.routes_disputes import get_client

    def forbidden(*args, **kwargs):
        raise AssertionError("network is forbidden in the P6 visual fixture")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "fixture.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_RULES_COUNTRY", "MX")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    for feature in ("fulfilment", "accounting_line", "sale_lesson", "package_insert", "trade_profiles"):
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature.upper()}", "true")
    mode = os.environ["OLA4_P6_CAPTURE"]
    monkeypatch.setenv("COPILOT_FEATURE_CLOSURE_SUMMARY", "true" if mode in {"after", "after-money"} else "false")

    case = "PP-R-DEMO-0000001"
    capture = "12345678901234567"
    norm = {"dispute_id": case, "seller_transaction_ids": [capture], "create_time": "2026-10-02T12:00:00Z",
            "amount_cents": 1250, "currency": "USD"}
    outcome = OutcomeRead("CLOSED", "RESOLVED", "RESOLVED_WITH_PAYOUT", 250, True, (), False, None,
                          norm=norm, amount_refunded_currency="USD")
    context = TransactionContext({
        "total_amount_cents": Field(4999, SRC_SEARCH), "total_currency": Field("USD", SRC_SEARCH),
        "payment_date": Field("2026-10-01T09:30:00Z", SRC_SEARCH),
    })
    reads = {"outcome": 0, "context": 0}

    def read_outcome(_client, _case):
        reads["outcome"] += 1
        return outcome

    def fetch_context(_client, _norm):
        reads["context"] += 1
        return context

    monkeypatch.setattr(accounting, "read_outcome", read_outcome)
    monkeypatch.setattr(lesson_routes, "read_outcome", read_outcome)
    monkeypatch.setattr(accounting, "fetch_context", fetch_context)
    monkeypatch.setattr(accounting.ledger, "read", lambda *_args, **_kwargs: {"records": [], "limitations": []})
    app = create_app()
    app.dependency_overrides[get_client] = lambda: object()
    lesson_service.record_lesson("PP-R-SOURCE-0000001", "33333333333333333", "physical",
                                 "RESOLVED_BUYER_FAVOUR", "Photograph the packed item before dispatch.",
                                 now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    profile_service.select_profile("clothing")
    token = store.save_check(capture, {"trackers": [{"tracking_number": "TRACK-LOCAL-01", "carrier": "USPS",
                                                     "status": "SHIPPED"}]},
                             "request-hash", {}, {}, {})
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    accounting_response = client.get(f"/disputes/{case}/accounting-line")
    lesson_response = client.get(f"/disputes/{case}/sale-lesson", params={"capture_id": capture})
    package_check_response = client.get(f"/fulfilment/{capture}/check/{token}")
    entry = re.search(r'<a href="([^"]+/package-insert)">Prepare a bilingual package insert</a>', package_check_response.text)
    assert entry
    package_url = entry.group(1)
    package_response = client.get(package_url)
    assert accounting_response.status_code == lesson_response.status_code == package_check_response.status_code == package_response.status_code == 200
    assert "Item description" in package_response.text and "Seller contact" in package_response.text
    csrf = re.search(r'name="csrf_token" value="([^\"]+)"', package_response.text).group(1)
    insert_data = {"csrf_token": csrf, "item_description": "Cotton scarf", "seller_contact": "shop@example.test"}
    if mode in {"after", "after-money"}:
        insert_data["selected_lesson_ids"] = ["PP-R-SOURCE-0000001:33333333333333333"]
    package_print = client.post(package_url, data=insert_data)
    assert package_print.status_code == 200

    artifact_dir = Path(__file__).resolve().parents[3] / "internal" / "lanes" / "nivel2" / "mejoras-en-producto-2026-10-31" / "ola4" / "luna-b"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    css = (Path("copilot/web/static/app.css")).read_text(encoding="utf-8")
    if mode == "baseline-matched":
        assert "sale_amount_source" not in accounting_response.text
        assert "seller-lesson-suggestions" not in package_response.text
        legacy_csv = client.get(f"/disputes/{case}/accounting-line.csv")
        assert legacy_csv.status_code == 200 and "sale_amount_source" not in legacy_csv.text
        assert reads == {"outcome": 3, "context": 2}
    else:
        assert "Transaction Search gross" in accounting_response.text
        assert "49.99 USD" in accounting_response.text and "2.50 USD" in accounting_response.text
        assert "opening the legacy CSV URL" not in accounting_response.text
        assert "seller-lesson-suggestions" in package_response.text
        assert "Photograph the packed item before dispatch." in package_response.text
        assert "Photograph the packed item before dispatch." in package_print.text
    if mode == "baseline-matched":
        assert "seller-lesson-suggestions" not in package_response.text
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        captures = (("accounting", accounting_response),) if mode == "after-money" else (
            ("accounting", accounting_response), ("lesson", lesson_response),
            ("package-check", package_check_response), ("package-form", package_response),
            ("package-print", package_print))
        for label, response in captures:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            origin = f"http://p6-{label}.test/page"

            def serve_local(route, *, document=response):
                if route.request.url.endswith("/static/app.css"):
                    route.fulfill(status=200, content_type="text/css", body=css)
                elif route.request.url == origin:
                    headers = {"content-type": document.headers.get("content-type", "text/html; charset=utf-8")}
                    if document.headers.get("content-security-policy"):
                        headers["content-security-policy"] = document.headers["content-security-policy"]
                    route.fulfill(status=document.status_code, headers=headers, body=document.text)
                else:
                    route.abort()

            page.route("**/*", serve_local)
            page.goto(origin, wait_until="domcontentloaded")
            filename = "after-money-accounting.png" if mode == "after-money" else f"{mode}-v4-{label}.png"
            page.screenshot(path=str(artifact_dir / filename), full_page=True)
            page.close()
        browser.close()

