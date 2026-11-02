"""Opt-in desktop captures for the real local case-records route (no browser network)."""
import os
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("OLA2_CAPTURE") != "true", reason="set OLA2_CAPTURE=true for the explicit Ola2 capture")
def test_visual_case_hub_baseline_and_current_same_fixture(monkeypatch, tmp_path):
    """Expected: same synthetic case/config/ASGI route; the preserved pre-review template shows the repetitive
    aggregate panel while current Case records retains only the progressive case explanation. Browser network is aborted.
    """
    from fastapi.testclient import TestClient
    from jinja2 import ChoiceLoader, DictLoader
    from copilot import config
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client, templates
    from playwright.sync_api import sync_playwright
    from _hosted import authenticated_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "visual.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")
    dispute = "PP-R-OLA2-0000001"
    raw = {"dispute_id": dispute, "reason": "UNAUTHORISED", "status": "WAITING_FOR_SELLER_RESPONSE",
           "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"value": "12.50", "currency_code": "USD"},
           "create_time": "2026-10-01T12:00:00Z", "update_time": "2026-10-30T12:00:00Z",
           "seller_response_due_date": "2026-11-05T12:00:00Z", "links": [], "disputed_transactions": []}

    class ReadOnlyFake:
        def call(self, method, path, op, **kwargs):
            assert method == "GET"
            assert op == "show_dispute"
            assert path.endswith(dispute)
            return raw

    app = create_app()
    app.dependency_overrides[get_client] = lambda: ReadOnlyFake()
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    old_loader = templates.env.loader
    try:
        prior_dir = Path("internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola2/luna-b")
        baseline_fragment = (prior_dir / "prior-case-queue-explanation.html").read_text(encoding="utf-8")
        baseline_exposure = (prior_dir / "prior-exposure-context.html").read_text(encoding="utf-8")
        templates.env.loader = ChoiceLoader([DictLoader({
            "case/_queue_explanation.html": baseline_fragment,
            "features/explainable_queue/_exposure_context.html": baseline_exposure,
        }), old_loader])
        templates.env.cache.clear()
        before = client.get(f"/disputes/{dispute}/workspace/records")
    finally:
        templates.env.loader = old_loader
        templates.env.cache.clear()
    after = client.get(f"/disputes/{dispute}/workspace/records")
    assert before.status_code == after.status_code == 200
    assert 'data-state="dispute-exposure-context"' in before.text
    assert 'data-state="dispute-exposure-context"' not in after.text
    assert "Why this case is in this state" in after.text
    assert 'data-state="queue-explanation-details"' in after.text
    css = Path("copilot/web/static/app.css").read_text(encoding="utf-8")
    out = Path("internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola2/luna-b")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for label, response in (("detail-before-review", before), ("detail-after-review", after)):
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.route("**/*", lambda route: route.abort())
                page.set_content(response.text, wait_until="domcontentloaded")
                page.add_style_tag(content=css)
                if label == "detail-before-review":
                    assert page.locator('[data-state="dispute-exposure-context"]').is_visible()
                else:
                    assert page.locator('[data-state="dispute-exposure-context"]').count() == 0
                page.screenshot(path=str(out / f"{label}.png"), full_page=True)
                page.close()
        finally:
            browser.close()
