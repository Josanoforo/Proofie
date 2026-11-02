"""Opt-in full-page preview of the protected account-template include proposed for D."""
import os
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("OLA2_CAPTURE") != "true", reason="set OLA2_CAPTURE=true for the explicit account capture")
def test_visual_account_exposure_proposed_include_same_summary(monkeypatch, tmp_path):
    """Expected: actual /account ASGI route and CSS, same fake summary/config on both renders. The second render
    applies only the proposed include in the in-memory Jinja loader; no protected file is edited or claimed integrated.
    """
    from datetime import date, datetime, timezone
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from jinja2 import ChoiceLoader, DictLoader
    from copilot import config
    from copilot.app import create_app
    from copilot.features.explainable_queue.service import account_exposure_context
    from copilot.web.routes_disputes import get_client, templates
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "visual-account.sqlite3")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF", "true")
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")

    norms = [
        {"dispute_id": "PP-R-F-0000001", "status": "WAITING_FOR_SELLER_RESPONSE", "amount_cents": 1200,
         "currency": "USD", "create_time": "2026-08-15T10:00:00Z"},
        {"dispute_id": "PP-R-F-0000002", "status": "UNDER_REVIEW", "amount_cents": 345,
         "currency": "JPY", "create_time": "2026-09-10T10:00:00Z"},
        {"dispute_id": "PP-R-F-0000003", "status": "OPEN", "amount_cents": None,
         "currency": None, "create_time": None},
    ]
    context = account_exposure_context(
        norms, sales_count=2, window_dispute_count=2,
        window_start=date(2026, 7, 1), window_end=date(2026, 9, 30),
        sales_complete=True, disputes_complete=True,
        computed_at=datetime(2026, 10, 31, 12, tzinfo=timezone.utc),
    )
    summary = SimpleNamespace(window_start=date(2026, 7, 1), window_end=date(2026, 9, 30), sales=2, disputes=2,
                              rate_bps=10000, high_volume=False, disputes_to_threshold=None, fee_now_cents=1500,
                              fee_if_high_cents=3000, limitations=(), computed_at=datetime(2026, 10, 31, 12,
                              tzinfo=timezone.utc), exposure_context=context)
    from copilot.features.account_cliff import service as cliff_service
    monkeypatch.setattr(cliff_service, "compute", lambda client: summary)

    class NoCallClient:
        def call(self, *args, **kwargs):
            raise AssertionError("the mocked compute must not use a client")

    app = create_app()
    app.dependency_overrides[get_client] = lambda: NoCallClient()
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    before = client.get("/account")
    account_template = Path("copilot/web/templates/features/account_cliff/account.html").read_text(encoding="utf-8")
    anchor = "<p>These are the account counts read from PayPal for this window. The same formula is used for the fee comparison.</p>"
    assert account_template.count(anchor) == 1
    proposed_include = (anchor + '\n{% if feature_enabled("explainable_queue") and summary.exposure_context %}'
                       '\n{% set account_exposure = summary.exposure_context %}'
                       '\n{% include "features/explainable_queue/_account_exposure.html" %}'
                       '\n{% endif %}')
    old_loader = templates.env.loader
    try:
        templates.env.loader = ChoiceLoader([DictLoader({
            "features/account_cliff/account.html": account_template.replace(anchor, proposed_include, 1),
        }), old_loader])
        templates.env.cache.clear()
        after = client.get("/account")
    finally:
        templates.env.loader = old_loader
        templates.env.cache.clear()
    assert before.status_code == after.status_code == 200
    assert 'data-state="account-dispute-exposure"' not in before.text
    assert 'data-state="account-dispute-exposure"' in after.text
    assert after.text.count("12.00 USD") == 1 and "345 JPY" in after.text
    assert "Period, sources and data coverage" in after.text
    css = Path("copilot/web/static/app.css").read_text(encoding="utf-8")
    out = Path("internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola2/luna-b")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for name, response in (("account-before-input-patch", before), ("account-with-input-patch", after)):
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.route("**/*", lambda route: route.abort())
                page.set_content(response.text, wait_until="domcontentloaded")
                page.add_style_tag(content=css)
                if name == "account-with-input-patch":
                    assert page.locator('[data-state="account-dispute-exposure"]').is_visible()
                page.screenshot(path=str(out / f"{name}.png"), full_page=True)
                page.close()
        finally:
            browser.close()
