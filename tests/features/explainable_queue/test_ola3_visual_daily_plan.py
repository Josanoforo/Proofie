"""Opt-in full-page before/after capture of the habitual inbox, using the exact Ola2 product baseline."""
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import pytest


BASELINE = "41245e74e8cb58ad73de7958920aeeaf3791471c"


@pytest.mark.skipif(os.environ.get("OLA3_CAPTURE") != "true", reason="set OLA3_CAPTURE=true for the Ola3 desktop capture")
def test_visual_inbox_plan_baseline_and_current_same_records(monkeypatch, tmp_path):
    """Expected in expected.md: only the current in-memory fragment adds the plan; app/fixture/config are identical."""
    from jinja2 import ChoiceLoader, DictLoader
    from copilot import config
    from copilot.app import create_app
    from copilot.core import auto_prep
    from copilot.core.disputes import normalize
    from copilot.db import repo
    from copilot.web import routes_disputes
    from copilot.web.routes_disputes import get_client, templates
    from playwright.sync_api import sync_playwright
    from _hosted import authenticated_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "visual-inbox.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(routes_disputes, "datetime", FrozenDateTime)
    def dispute(case_id, status="WAITING_FOR_SELLER_RESPONSE", due=None, reason="UNAUTHORISED"):
        return {"dispute_id": case_id, "reason": reason, "status": status,
                "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"value": "12.50", "currency_code": "USD"},
                "create_time": "2026-10-01T12:00:00Z", "update_time": "2026-10-30T12:00:00Z",
                "seller_response_due_date": due, "links": [], "disputed_transactions": []}

    ids = [f"PP-R-OLA3-{i:07d}" for i in range(1, 9)]
    raw_cases = [dispute(ids[0], due="2026-10-31T17:00:00Z", reason=auto_prep.PREPARED_REASON),
                 dispute(ids[1], due="2026-11-02T17:00:00Z"),
                 dispute(ids[2], due="2026-11-05T18:00:00Z"),
                 dispute(ids[3]), dispute(ids[4], due="not-a-date"),
                 dispute(ids[5], "WAITING_FOR_BUYER_RESPONSE"),
                 dispute(ids[6], "UNDER_REVIEW"), dispute(ids[7], "RESOLVED")]

    class ReadOnlyFake:
        def call(self, method, path, op, **kwargs):
            assert method == "GET"
            assert op == "list_disputes" and path == "/v1/customer/disputes"
            return {"items": raw_cases, "links": []}

    # Give one case genuine local progress records so the task shown after the refresh is the next current task.
    document_id = repo.insert_document(config.db_path(), ids[0], "source.pdf", "application/pdf", 4,
                                       "b" * 64, "synthetic visual fixture", True)
    package_id = repo.insert_package(config.db_path(), ids[0], "human_edit", True, [], effective_next_step="Review")
    repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
                                                       "text": "Seller statement from fixture", "source_ids": [str(document_id)],
                                                       "basis": "seller_statement"}])
    norm = normalize(raw_cases[0], datetime.now(timezone.utc))
    version_hash = auto_prep.compute_version_hash(config.db_path(), norm)
    token = repo.claim_auto_prep(config.db_path(), ids[0], version_hash)
    assert token and repo.set_auto_prep_result(config.db_path(), ids[0], version_hash, token,
                                               "ready", package_id=package_id)

    app = create_app()
    app.dependency_overrides[get_client] = lambda: ReadOnlyFake()
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    before_template = subprocess.run(
        ["git", "show", f"{BASELINE}:copilot/web/templates/features/explainable_queue/_exposure_context.html"],
        check=True, capture_output=True, text=True, encoding="utf-8")
    old_loader = templates.env.loader
    try:
        templates.env.loader = ChoiceLoader([DictLoader({
            "features/explainable_queue/_exposure_context.html": before_template.stdout,
        }), old_loader])
        templates.env.cache.clear()
        before = client.get("/disputes")
    finally:
        templates.env.loader = old_loader
        templates.env.cache.clear()
    after = client.get("/disputes")
    assert before.status_code == after.status_code == 200
    assert 'data-state="daily-work-plan"' not in before.text
    assert 'data-state="daily-work-plan"' in after.text
    assert f'href="/disputes/{ids[0]}/packages/{package_id}"' in after.text
    assert "Current local records: 1 case with documents" in after.text

    css = Path("copilot/web/static/app.css").read_text(encoding="utf-8")
    out = Path("internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola3/luna-b")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for name, response, summary_text in (
                    ("before", before, "Scope, sources and coverage details"),
                    ("after", after, "Daily plan, scope, sources and progress")):
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                page.route("**/*", lambda route: route.abort())
                page.set_content(response.text, wait_until="domcontentloaded")
                # Full-page Chromium capture would tile the real sticky header across the document; normalize only its
                # positioning in the capture so the actual header controls remain visible once, without overlay.
                page.add_style_tag(content=css + "\n.site-header { position: static !important; }")
                details = page.locator('section[data-state="dispute-exposure-context"] details')
                assert details.locator("summary").inner_text() == summary_text
                details.locator("summary").click()
                if name == "after":
                    assert page.locator('[data-state="daily-work-plan"]').is_visible()
                    assert page.locator('[data-state="daily-work-plan"]').get_by_role(
                        "link", name="Review the draft", exact=True).is_visible()
                page.screenshot(path=str(out / f"{name}-static-header.png"), full_page=True)
                page.close()
        finally:
            browser.close()
