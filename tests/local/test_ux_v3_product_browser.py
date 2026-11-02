"""Single focused real-browser smoke for the UX v3 product integration; synthetic local state only."""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from copilot import config
from copilot.app import create_app
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[2]
DID = "PP-R-DEMO-0000001"
FIXTURE = ROOT / "pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json"


class GetOnlyFake:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def call(self, method, path, operation, **kwargs):
        if method != "GET":
            raise AssertionError(f"non-GET provider operation: {method} {operation}")
        self.calls.append({"method": method, "path": path, "operation": operation})
        if operation in ("show_dispute", "dispute_detail"):
            return self.body
        if operation == "list_disputes":
            return {"items": [self.body], "links": []}
        if operation == "context_transaction_search":
            return {"transaction_details": []}
        if operation == "context_tracking":
            return {"trackers": []}
        if operation == "recovery_get":
            return {"evidences": []}
        raise AssertionError(f"unexpected operation: {operation}")



def _open_statements(page):
    """O4 folds the editable part of the package page in "Open package details"; the statements fold is inside it."""
    for selector in ('#package-edit-sections > summary', '[data-state="fold-statements"] > summary'):
        if not page.locator(selector).evaluate("summary => summary.parentElement.open"):
            page.locator(selector).click()

def test_real_browser_case_package_original_and_desktop_reflow(monkeypatch, tmp_path):
    import os
    import pytest
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is unavailable in the checkout venv")

    for key in list(os.environ):
        if key.startswith(("COPILOT_FEATURE_", "PAYPAL_", "OLLAMA_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "data" / "evidence"))
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    config.set_db_path_override(tmp_path / "data" / "proofie.sqlite")
    config.set_uploads_dir_override(tmp_path / "data" / "uploads")
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]
    config.data_dir().mkdir(parents=True, exist_ok=True)
    repo.ensure_schema(config.db_path())
    original = b"%PDF-1.4\nsynthetic browser receipt\n%%EOF\n"
    sha = hashlib.sha256(original).hexdigest()
    doc_id = repo.insert_document(config.db_path(), DID, "Synthetic receipt.pdf", "application/pdf",
                                  len(original), sha, "Carrier accepted parcel at depot", True)
    write_raw(config.uploads_dir(), sha, original)
    repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    package_id = repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "The carrier accepted the parcel.", "source_ids": [f"doc_{doc_id}"], "basis": "verified"}])
    repo.attach_document(config.db_path(), package_id, doc_id)
    repo.set_manual_fields(config.db_path(), DID, {
        "tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
        "carrier": {"value": "FedEx", "confidence": "read"}})
    # Mark this exact synthetic fixture version as already prepared, so normal inbox navigation opens
    # the fixture package instead of scheduling the app's unrelated automatic-preparation path.
    from copilot.core import auto_prep
    from copilot.web.routes_disputes import normalize
    norm = normalize(body, datetime.now(timezone.utc))
    prep_hash = auto_prep.compute_version_hash(config.db_path(), norm)
    prep_token = repo.claim_auto_prep(config.db_path(), DID, prep_hash)
    assert prep_token and repo.set_auto_prep_result(config.db_path(), DID, prep_hash, prep_token,
                                                     "ready", package_id=package_id)
    fake = GetOnlyFake(body)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(150):
        if server.started:
            break
        if not thread.is_alive():
            raise AssertionError("owned loopback server exited before startup")
        time.sleep(0.1)
    assert server.started, "owned loopback server startup timed out"

    base = f"http://127.0.0.1:{port}"
    output = ROOT / "data/mejoras10/browser-v3-product-final/consolidation-correction"
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"method": "real Chromium via checkout Playwright; app served on 127.0.0.1 with synthetic fixture and GET-only fake",
        "viewports": [], "routes": [], "external_requests": [], "page_errors": [], "provider_calls": fake.calls,
        "original_sha256": sha, "screenshots": []}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            manifest["browser"] = browser.version
            blocked_package_url = None
            for width, height in ((1440, 1000), (1280, 800)):
                context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                page = context.new_page()
                page.on("pageerror", lambda error: manifest["page_errors"].append(str(error)))
                def guard(route):
                    if route.request.url == base or route.request.url.startswith(base + "/"):
                        route.continue_()
                    else:
                        manifest["external_requests"].append(route.request.url)
                        route.abort()
                page.route("**/*", guard)
                # Reach the case and its task from ordinary navigation; only the app root is entered directly.
                response = page.goto(base + "/", wait_until="networkidle", timeout=30000)
                assert response is not None and response.status == 200 and page.url.endswith("/disputes")
                page.locator(f'a[href="/disputes/{DID}"]').click()
                page.wait_for_load_state("networkidle")
                case_path = f"/disputes/{DID}"
                assert page.url.endswith(case_path)
                page.keyboard.press("Tab")
                focused = page.evaluate("document.activeElement && document.activeElement.tagName")
                assert focused not in (None, "BODY"), f"keyboard focus did not advance at {width}x{height}"
                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not overflow, f"horizontal overflow at {width}x{height} on case"
                path_out = output / f"case-full-{width}x{height}.png"
                page.screenshot(path=str(path_out), full_page=True)
                manifest["screenshots"].append(path_out.name)
                path_out = output / f"case-top-{width}x{height}.png"
                page.screenshot(path=str(path_out), full_page=False)
                manifest["screenshots"].append(path_out.name)
                manifest["routes"].append({"state": "case via inbox link", "path": case_path, "status": 200,
                    "horizontal_overflow": overflow, "keyboard_focus": focused})
                page.get_by_role("button", name="All tasks and case links").click()      # pass 3: the task table sits in a closed block on the overview
                task_link = page.locator('#tasks a.task-link[href*="/packages/"]').first
                assert task_link.count() == 1 and task_link.is_visible()
                task_link.click()
                page.wait_for_load_state("networkidle")
                blocked_package_url = page.url
                assert f"/disputes/{DID}/packages/" in page.url
                package_id = int(page.url.split("/packages/", 1)[1].split("/", 1)[0].split("#", 1)[0])
                assert page.locator("link[rel=stylesheet][href*='/static/app.css']").count() == 1
                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not overflow, f"horizontal overflow at {width}x{height} on package"
                _open_statements(page)      # pass 3: the statements section is closed with a one-line summary
                assert page.get_by_text("Extracted text excerpt — not the original").is_visible()
                assert page.get_by_text("Carrier accepted parcel at depot").is_visible()
                for state, locator in (("package-top", page.locator("h1")),
                                        ("claim-source", page.locator("#statements")),
                                        ("preview", page.locator('[data-state="will-send"]'))):
                    locator.evaluate("el => window.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + window.scrollY - 55))")
                    path_out = output / f"{state}-{width}x{height}.png"
                    page.screenshot(path=str(path_out), full_page=False)
                    manifest["screenshots"].append(path_out.name)
                    manifest["routes"].append({"state": state, "path": page.url.replace(base, ""),
                        "status": 200, "viewport": [width, height], "source_fixture": "synthetic dispute fixture + SQLite document/package"})
                with page.expect_download() as download_info:
                    page.get_by_role("link", name="Open original").first.click()
                download = download_info.value
                saved = output / f"original-{width}x{height}.pdf"
                download.save_as(saved)
                assert saved.read_bytes() == original
                if width == 1280:
                    assert page.get_by_role("button", name="Approve this package").is_visible()
                    assert "Approving does not send anything" in page.locator("body").inner_text()
                    page.get_by_role("button", name="Approve this package").click()
                    page.wait_for_load_state("networkidle")
                    # O4: the approval paragraph sits in the closed "Open package details" block; the visible fact is the Sending line of the state rail
                    assert page.locator('.package-state-rail [data-state="approved"]').first.is_visible()
                    send_button = page.get_by_role("button", name="Send evidence to PayPal")
                    send_button_visible = send_button.count() == 1 and send_button.is_visible()
                    next_step = page.locator("#next-step")
                    next_step.evaluate("el => window.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + window.scrollY - 45))")
                    path_out = output / f"approved-before-edit-{width}x{height}.png"
                    page.screenshot(path=str(path_out), full_page=False)
                    manifest["screenshots"].append(path_out.name)
                    manifest["routes"].append({"state": "approved version; send control not activated",
                        "path": page.url.replace(base, ""), "status": 200, "viewport": [width, height],
                        "send_button_visible": send_button_visible,
                        "send_remained_unpressed": True})
                    _open_statements(page)      # pass 3: closed after the reload
                    textarea = page.locator(".claim-edit textarea").first
                    textarea.fill("Delivered on 2026-09-20.")
                    page.locator(".claim-edit button[type=submit]").first.click()
                    page.wait_for_load_state("networkidle")
                    combined = page.locator('[data-state="approval-blocked-and-cleared"]')
                    assert combined.count() == 1 and combined.is_visible()
                    assert page.locator('[data-state="unsupported-delivery-approval-block"]').count() == 0
                    assert page.locator('[data-state="approval-cleared"]').count() == 0
                    assert "Approval is blocked until these delivery statements are corrected" in page.locator("body").inner_text()
                    for required_copy in ("package v3 was cleared because the content changed",
                                          "The original and extracted text are shown",
                                          "This block cannot be overridden",
                                          "approve again"):
                        assert required_copy in combined.inner_text()
                    combined.evaluate("el => window.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + window.scrollY - 35))")
                    path_out = output / f"approval-blocked-cleared-{width}x{height}.png"
                    page.screenshot(path=str(path_out), full_page=False)
                    manifest["screenshots"].append(path_out.name)
                    manifest["routes"].append({"state": "package v3 edited: approval cleared + delivery blocker consolidated into one panel",
                        "path": page.url.replace(base, ""),
                        "status": 200, "viewport": [width, height], "source_fixture": "synthetic dispute fixture + UI claim edit"})
                manifest["viewports"].append({"width": width, "height": height})
                context.close()
            # Same persisted blocked state, now captured at the taller viewport too.
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            page = context.new_page()
            page.on("pageerror", lambda error: manifest["page_errors"].append(str(error)))
            page.route("**/*", guard)
            page.goto(base + "/", wait_until="networkidle", timeout=30000)
            page.locator(f'a[href="/disputes/{DID}"]').click()
            page.wait_for_load_state("networkidle")
            page.get_by_role("button", name="All tasks and case links").click()
            page.locator('#tasks a.task-link[href*="/packages/"]').first.click()
            page.wait_for_load_state("networkidle")
            approval = page.locator("#approval")
            combined = page.locator('[data-state="approval-blocked-and-cleared"]')
            assert combined.count() == 1 and combined.is_visible()
            assert page.locator('[data-state="unsupported-delivery-approval-block"]').count() == 0
            assert page.locator('[data-state="approval-cleared"]').count() == 0
            approval.evaluate("el => window.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + window.scrollY - 45))")
            path_out = output / "approval-blocked-cleared-1440x1000.png"
            page.screenshot(path=str(path_out), full_page=False)
            manifest["screenshots"].append(path_out.name)
            manifest["routes"].append({"state": "package v3 edited: approval cleared + delivery blocker consolidated into one panel",
                "path": page.url.replace(base, ""),
                "status": 200, "viewport": [1440, 1000], "source_fixture": "same persisted synthetic DB state as 1280 capture"})
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    manifest["server_stopped"] = not thread.is_alive()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert manifest["server_stopped"]
    assert not manifest["external_requests"]
    assert not manifest["page_errors"]
    assert fake.calls and all(call["method"] == "GET" for call in fake.calls)
