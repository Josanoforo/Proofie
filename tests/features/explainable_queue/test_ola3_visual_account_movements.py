"""Opt-in composition preview for the account impact fragment; its payload is synthetic."""
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest


BASELINE = "41245e74e8cb58ad73de7958920aeeaf3791471c"


@pytest.mark.skipif(os.environ.get("OLA3_CAPTURE") != "true", reason="set OLA3_CAPTURE=true for the Ola3 desktop capture")
def test_visual_account_movements_baseline_and_current_same_synthetic_summary(monkeypatch, tmp_path):
    """Expected in expected.md: baseline lacks the new movement detail; current renders synthetic rows by currency."""
    from jinja2 import ChoiceLoader, DictLoader
    from copilot import config
    from copilot.app import create_app
    from copilot.features.account_cliff import service as account_service
    from copilot.features.account_cliff.service import CliffSummary
    from copilot.features.explainable_queue.service import account_exposure_context
    from copilot.web import routes_disputes
    from copilot.web.routes_disputes import get_client, templates
    from playwright.sync_api import sync_playwright
    from _hosted import authenticated_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "visual-account.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF", "true")
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")

    disputes = [
        {"dispute_id": "SYN-01", "status": "OPEN", "create_time": "2026-08-20T10:00:00Z", "amount_cents": 1250, "currency": "USD"},
        {"dispute_id": "SYN-02", "status": "OPEN", "create_time": "2026-08-21T10:00:00Z", "amount_cents": 1250, "currency": "USD"},
    ]
    def tx(txid, value, currency, day):
        return {"transaction_info": {"transaction_id": txid, "transaction_event_code": "T0006",
                "transaction_status": "S", "transaction_initiation_date": day,
                "transaction_amount": {"value": value, "currency_code": currency}}}
    transaction_rows = [tx("SYN-USD-1", "12.34", "USD", "2026-07-02T10:00:00Z"),
                        tx("SYN-USD-2", "5.00", "USD", "2026-07-03T10:00:00Z"),
                        tx("SYN-JPY", "250", "JPY", "2026-08-03T10:00:00Z"),
                        tx("SYN-MXN", "12.99", "MXN", "2026-09-04T10:00:00Z")]
    exposure = account_exposure_context(
        disputes, sales_count=2, window_dispute_count=4,
        window_start=date(2026, 7, 1), window_end=date(2026, 9, 30),
        sales_complete=True, disputes_complete=True, computed_at=datetime(2026, 10, 31, 18, tzinfo=timezone.utc),
        transaction_rows=transaction_rows, transactions_complete=True)
    exposure["source_disputes"] = "Synthetic test fixture; no provider read."
    exposure["source_sales"] = "Synthetic test fixture; no provider read."
    exposure["completed_sales"]["source"] = "Synthetic test fixture; no Transaction Search provider read."
    exposure["completed_sales"]["scope"] = "Synthetic completed-sale rows only; no settlement, payout, refund or cash-flow inference."

    summary = CliffSummary(date(2026, 7, 1), date(2026, 9, 30), 2, 4, None, False, None,
                           None, 2500, (), datetime(2026, 10, 31, 18, tzinfo=timezone.utc), exposure, False)
    monkeypatch.setattr(account_service, "compute", lambda client, include_exposure=False: summary)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: object()
    client = authenticated_client(app, client=("127.0.0.1", 12345))

    baseline_account = subprocess.run(
        ["git", "show", f"{BASELINE}:copilot/web/templates/features/account_cliff/account.html"],
        check=True, capture_output=True, text=True, encoding="utf-8")
    baseline_fragment = subprocess.run(
        ["git", "show", f"{BASELINE}:copilot/web/templates/features/explainable_queue/_account_exposure.html"],
        check=True, capture_output=True, text=True, encoding="utf-8")
    old_loader = templates.env.loader
    try:
        templates.env.loader = ChoiceLoader([DictLoader({
            "features/account_cliff/account.html": baseline_account.stdout,
            "features/explainable_queue/_account_exposure.html": baseline_fragment.stdout,
        }), old_loader])
        templates.env.cache.clear()
        before = client.get("/account")
    finally:
        templates.env.loader = old_loader
        templates.env.cache.clear()
    after = client.get("/account")
    assert before.status_code == after.status_code == 200
    assert "Recorded completed-sale amounts" not in before.text
    assert "Recorded completed-sale amounts" in after.text
    assert "Synthetic test fixture; no provider read." in after.text
    assert "Synthetic completed-sale rows only" in after.text
    assert "17.34 USD" in after.text and "250 JPY" in after.text and "12.99 MXN" in after.text
    assert "2026-07-01" in after.text and "2026-09-30" in after.text

    css = Path("copilot/web/static/app.css").read_text(encoding="utf-8")
    out = Path("internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola3/luna-b")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for name, response in (("before", before), ("after", after)):
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                page.route("**/*", lambda route: route.abort())
                page.set_content(response.text, wait_until="domcontentloaded")
                page.add_style_tag(content=css + "\n.site-header { position: static !important; }")
                page.evaluate("""() => {
                  const label = document.createElement('p');
                  label.textContent = 'SYNTHETIC FIXTURE PREVIEW - no PayPal or Transaction Search provider read';
                  label.setAttribute('role', 'note');
                  label.style.cssText = 'margin:12px auto;padding:10px;max-width:960px;background:#fff3cd;color:#5b4300;font-weight:700;border:2px solid #9b7400';
                  document.querySelector('main').prepend(label);
                }""")
                assert page.locator('[role="note"]').inner_text() == "SYNTHETIC FIXTURE PREVIEW - no PayPal or Transaction Search provider read"
                detail = page.locator("section[data-state='account-dispute-exposure'] details")
                assert detail.locator("summary").inner_text() == "Period, sources and data coverage"
                detail.locator("summary").click()
                if name == "after":
                    assert page.get_by_text("Synthetic completed-sale rows only", exact=False).is_visible()
                    assert page.get_by_text("17.34 USD", exact=False).is_visible()
                page.screenshot(path=str(out / f"account-{name}-static-header.png"), full_page=True)
                page.close()
        finally:
            browser.close()
