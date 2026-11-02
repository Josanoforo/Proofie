"""Focused causal acceptance for the Ola4 P6 usual routes. All data and outputs are synthetic/local."""
from __future__ import annotations

import base64
import csv
import io
import os
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.core.context import Field, SRC_DISPUTE, SRC_SEARCH, TransactionContext
from copilot.features.accounting_line import routes as accounting
from copilot.features.accounting_line import service as accounting_service
from copilot.features.fulfilment import store
from copilot.features.outcome.read import OutcomeRead
from copilot.features.package_insert import routes as package_routes
from copilot.features.sale_lesson import routes as lesson_routes
from copilot.features.sale_lesson import service as lesson_service
from copilot.features.trade_profiles import service as profile_service
from copilot.web.routes_disputes import get_client


CASE = "PP-R-DEMO-0000001"
CAPTURE_A = "12345678901234567"
CAPTURE_B = "76543210987654321"


def _blocked(*_args, **_kwargs):
    raise AssertionError("P6 fakes forbid every HTTP call")


def _setup(monkeypatch, tmp_path, *, features):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "p6.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_RULES_COUNTRY", "MX")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    for feature in features:
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature.upper()}", "true")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: object()
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))


def _outcome(captures=(CAPTURE_A, CAPTURE_B), code="RESOLVED_WITH_PAYOUT"):
    norm = {"dispute_id": CASE, "seller_transaction_ids": list(captures),
            "create_time": "2026-10-02T12:00:00Z", "amount_cents": 1250, "currency": "USD"}
    return OutcomeRead("CLOSED", "RESOLVED", code, 250, True, (), False, None,
                       norm=norm, amount_refunded_currency="USD")


def test_closure_selects_capture_and_downloads_same_response_csv(monkeypatch, tmp_path):
    client = _setup(monkeypatch, tmp_path, features=("accounting_line", "closure_summary"))
    counts = {"outcome": 0, "context": 0}

    def read(_client, _case):
        counts["outcome"] += 1
        return _outcome()

    def context(_client, norm):
        counts["context"] += 1
        assert norm.get("create_time") == "2026-10-02T12:00:00Z"
        capture = norm["seller_transaction_ids"][0]
        if capture == CAPTURE_B:
            return TransactionContext({
                "total_amount_cents": Field(1250, SRC_DISPUTE),
                "total_currency": Field("USD", SRC_DISPUTE),
            })
        return TransactionContext({
            "total_amount_cents": Field(4999, SRC_SEARCH),
            "total_currency": Field("USD", SRC_SEARCH),
            "payment_date": Field("2026-10-01T09:30:00Z", SRC_SEARCH),
        })

    monkeypatch.setattr(accounting, "read_outcome", read)
    monkeypatch.setattr(accounting, "fetch_context", context)
    monkeypatch.setattr(accounting.ledger, "read", lambda *_args, **_kwargs: {"records": [], "limitations": []})

    choice = client.get(f"/disputes/{CASE}/accounting-line")
    assert choice.status_code == 200
    assert 'data-state="capture-choice"' in choice.text
    assert CAPTURE_A in choice.text and CAPTURE_B in choice.text
    assert counts == {"outcome": 1, "context": 0}

    selected = client.get(f"/disputes/{CASE}/accounting-line", params={"capture_id": CAPTURE_B})
    assert selected.status_code == 200
    assert "Disputed principal only; not gross sale" in selected.text
    assert "paypal_dispute" in selected.text
    from copilot.features.draft.facts import money_text
    assert money_text(1250, "USD") in selected.text      # O4 F shows the amount as money; the cents sit in the CSV checked below
    assert counts == {"outcome": 2, "context": 1}
    href = re.search(r'href="(data:text/csv;charset=utf-8;base64,[^"]+)"', selected.text)
    assert href
    csv_data_uri = href.group(1)
    payload = base64.b64decode(csv_data_uri.split(",", 1)[1]).decode("utf-8")
    expected_row = accounting_service.row(CASE, CAPTURE_B, 1250, "USD", 250, "USD", None, "MX", 0,
                                          closure_summary=True, sale_amount_source=SRC_DISPUTE,
                                          sale_currency_source=SRC_DISPUTE,
                                          outcome_code="RESOLVED_WITH_PAYOUT")
    assert payload == accounting_service.csv_text(expected_row)
    before_download = dict(counts)

    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        origin = "http://p6-csv.test/summary"
        css = Path("copilot/web/static/app.css").read_text(encoding="utf-8")

        def fulfill_local(route):
            if route.request.url.endswith("/static/app.css"):
                route.fulfill(status=200, content_type="text/css", body=css)
            elif route.request.url == origin:
                headers = {"content-type": selected.headers.get("content-type", "text/html; charset=utf-8")}
                if selected.headers.get("content-security-policy"):
                    headers["content-security-policy"] = selected.headers["content-security-policy"]
                route.fulfill(status=selected.status_code, headers=headers, body=selected.text)
            else:
                route.abort()

        page.route("**/*", fulfill_local)
        page.goto(origin, wait_until="domcontentloaded")
        with page.expect_download() as download_info:
            page.locator('[data-state="same-response-csv"]').click()
        download = download_info.value
        download_path = tmp_path / download.suggested_filename
        download.save_as(str(download_path))
        assert download_path.read_bytes() == payload.encode("utf-8")
        expect(page.locator('[data-state="same-response-csv"]')).to_be_visible()
        browser.close()
    assert counts == before_download

    legacy = client.get(f"/disputes/{CASE}/accounting-line.csv", params={"capture_id": CAPTURE_A})
    assert legacy.status_code == 200
    legacy_row = next(csv.DictReader(io.StringIO(legacy.text)))
    assert legacy_row["sale_amount_cents"] == "4999"
    assert "sale_amount_source" in legacy.text
    assert "2026-10-01T09:30:00Z" in legacy.text
    assert counts == {"outcome": 3, "context": 2}
    rejected = client.get(f"/disputes/{CASE}/accounting-line", params={"capture_id": "11111111111111111"})
    assert rejected.status_code == 400


def test_stale_lesson_keeps_unsaved_type_and_text_without_append(monkeypatch, tmp_path):
    client = _setup(monkeypatch, tmp_path, features=("sale_lesson", "closure_summary"))
    current = {"view": _outcome((CAPTURE_A,), "RESOLVED_WITH_PAYOUT")}
    monkeypatch.setattr(lesson_routes, "read_outcome", lambda *_args: current["view"])
    page = client.get(f"/disputes/{CASE}/sale-lesson", params={"capture_id": CAPTURE_A})
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    current["view"] = _outcome((CAPTURE_A,), "RESOLVED_SELLER_FAVOUR")
    submitted = "Keep this unsaved note after the outcome changes"
    response = client.post(f"/disputes/{CASE}/sale-lesson", data={
        "capture_id": CAPTURE_A, "sale_type": "physical", "lesson": submitted,
        "outcome_code": "RESOLVED_WITH_PAYOUT", "csrf_token": csrf,
    })
    assert response.status_code == 409
    assert submitted in response.text
    assert '<option value="physical" selected>' in response.text
    assert "recorded outcome changed" in response.text
    assert lesson_service.list_lessons() == []
    expired_note = "Keep this note visible when CSRF expires"
    expired = client.post(f"/disputes/{CASE}/sale-lesson", data={
        "capture_id": CAPTURE_A, "sale_type": "service", "lesson": expired_note,
        "outcome_code": "RESOLVED_SELLER_FAVOUR", "csrf_token": "expired-token",
    })
    assert expired.status_code == 403
    assert expired_note in expired.text and '<option value="service" selected>' in expired.text
    assert "disabled" in expired.text and lesson_service.list_lessons() == []


def test_closure_flag_off_keeps_legacy_stale_lesson_error(monkeypatch, tmp_path):
    client = _setup(monkeypatch, tmp_path, features=("sale_lesson",))
    current = {"view": _outcome((CAPTURE_A,), "RESOLVED_WITH_PAYOUT")}
    monkeypatch.setattr(lesson_routes, "read_outcome", lambda *_args: current["view"])
    page = client.get(f"/disputes/{CASE}/sale-lesson", params={"capture_id": CAPTURE_A})
    csrf = re.search(r'name="csrf_token" value="([^\"]+)"', page.text).group(1)
    current["view"] = _outcome((CAPTURE_A,), "RESOLVED_SELLER_FAVOUR")
    response = client.post(f"/disputes/{CASE}/sale-lesson", data={
        "capture_id": CAPTURE_A, "sale_type": "physical", "lesson": "Legacy flag-off text",
        "outcome_code": "RESOLVED_WITH_PAYOUT", "csrf_token": csrf,
    })
    assert response.status_code == 409
    assert "recorded outcome changed" in response.text
    assert "Legacy flag-off text" not in response.text
    assert 'name="seller-lesson"' not in response.text
    assert lesson_service.list_lessons() == []


def test_physical_lesson_is_explicitly_selected_and_attributed_in_insert(monkeypatch, tmp_path):
    client = _setup(monkeypatch, tmp_path, features=("fulfilment", "package_insert", "sale_lesson", "trade_profiles",
                                                      "closure_summary"))
    lesson_service.record_lesson("PP-R-SOURCE-0000001", "33333333333333333", "physical",
                                 "RESOLVED_BUYER_FAVOUR", "Photograph the packed item before dispatch.")
    lesson_service.record_lesson("PP-R-SOURCE-0000002", "44444444444444444", "digital",
                                 "DENIED", "Keep the activation receipt.")
    profile_service.select_profile("clothing")
    capture = "55555555555555555"
    token = store.save_check(capture, {"trackers": [{"tracking_number": "TRACK-NEW-SALE", "carrier": "USPS",
                                                       "status": "SHIPPED"}]},
                             "request-sha", {}, {}, {})
    check_page = client.get(f"/fulfilment/{capture}/check/{token}")
    assert check_page.status_code == 200
    entry = re.search(r'<a href="([^"]+/package-insert)">Prepare a bilingual package insert</a>', check_page.text)
    assert entry
    url = entry.group(1)
    form = client.get(url)
    assert form.status_code == 200
    assert "Photograph the packed item before dispatch." in form.text
    assert "Keep the activation receipt." not in form.text
    assert 'value="PP-R-SOURCE-0000001:33333333333333333"' in form.text
    assert 'value="PP-R-SOURCE-0000001:33333333333333333" checked' not in form.text
    assert 'data-state="trade-profile-checklist"' in form.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    before = lesson_service.list_lessons()
    printed = client.post(url, data={
        "csrf_token": csrf, "item_description": "Cotton scarf", "seller_contact": "shop@example.test",
        "selected_lesson_ids": ["PP-R-SOURCE-0000001:33333333333333333"],
    })
    assert printed.status_code == 200
    assert "Photograph the packed item before dispatch." in printed.text
    assert "Seller interpretation; not a PayPal-confirmed cause" in printed.text
    assert "source case PP-R-SOURCE-0000001" in printed.text
    assert "Keep the activation receipt." not in printed.text
    assert 'data-state="trade-profile-reminders"' in printed.text
    assert "TRACK-NEW-SALE" in printed.text
    assert lesson_service.list_lessons() == before
    foreign = client.post(url, data={
        "csrf_token": csrf, "item_description": "Another scarf", "seller_contact": "shop@example.test",
        "selected_lesson_ids": ["PP-R-SOURCE-0000002:44444444444444444"],
    })
    assert foreign.status_code == 400
    assert "unavailable" in foreign.text and "Another scarf" in foreign.text
    assert lesson_service.list_lessons() == before

