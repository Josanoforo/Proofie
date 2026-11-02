"""Local synthetic browser measurement for the 2026-10-30 UX continuation.

The UI is rendered by the real templates with a GET-only fake client. Browser content uses
in-memory CSS/JS and makes no network requests. Approval and read-back states are local synthetic rows only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _hosted import authenticated_client, set_test_csrf
from copilot import config
from copilot.app import create_app
from copilot.core import auto_prep
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.raw_storage import write_raw
from copilot.core.send import build_request_input, sha256_json
from copilot.db import repo
from copilot.web.routes_disputes import get_client

pytestmark = pytest.mark.usefixtures("web_mode")

ROOT = Path(__file__).resolve().parents[2]
DID = "PP-R-DEMO-0000001"
NO_REPOSITORY = "unversioned (no git repository: a kit extracted from the ZIP)"


def _git_head():
    """The product head stamped into the evidence manifests; the ZIP has no repository to ask."""
    done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else NO_REPOSITORY


def _git_tracked_status():
    done = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else NO_REPOSITORY
FIXTURE = ROOT / "pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
CSS = (ROOT / "copilot/web/static/app.css").read_text(encoding="utf-8")
JS = (ROOT / "copilot/web/static/app.js").read_text(encoding="utf-8")
FLAG_SET = ("active_approval", "claims", "deadlines", "explainable_queue", "outcome", "evidence_dossier",
            "sale_readiness", "fulfilment", "case_history", "ledger", "timeline")


class GetOnlyFake:
    def __init__(self, body):
        self.body = body
        self.tracking_body = {"trackers": []}
        self.calls = []
        second = {**body, "dispute_id": "PP-R-DEMO-0000002", "create_time": "2026-10-04T12:00:00Z"}
        self.list_body = {"items": [body, second], "links": []}

    def call(self, method, path, operation, **kwargs):
        assert method == "GET", f"unexpected fake provider write: {method} {operation}"
        self.calls.append({"method": method, "operation": operation})
        if operation in ("show_dispute", "dispute_detail", "recovery_get", "post_send_get"):
            return self.body if operation in ("show_dispute", "dispute_detail") else {"evidences": []}
        if operation == "list_disputes":
            return self.list_body
        if operation == "context_transaction_search":
            return {"transaction_details": []}
        if operation == "context_tracking":
            return self.tracking_body
        raise AssertionError(f"unexpected GET operation: {operation}")


def _inline_assets(html: str) -> str:
    return _inline_assets_from(html, CSS, JS)


def _inline_assets_from(html: str, css: str, js: str) -> str:
    html = re.sub(r'<link\b[^>]*rel=["\']stylesheet["\'][^>]*>', "", html, flags=re.I)
    html = re.sub(r'<script\b[^>]*src=["\'][^"\']*["\'][^>]*>.*?</script>', "", html, flags=re.I | re.S)
    html = html.replace("</head>", f"<style>{css}</style></head>")
    return html.replace("</body>", f"<script>{js}</script></body>")


def _git_text(revision: str, path: str) -> str:
    result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=ROOT, check=True, capture_output=True)
    return result.stdout.decode("utf-8")


def test_claim_source_cards_render_all_values_and_zero_source_state():
    """The visible comparison handles 0/N source counts without requiring disclosure interaction."""
    from copilot.web.routes_disputes import templates

    template = templates.env.get_template("features/claims/_claim_cards.html")
    source_claim = {"text": "Shipment reached the destination.", "verdict": "unverifiable",
        "claim_basis": "published_policy", "bases": ["published_policy"], "sources": [
            {"artifact_id": "doc_a", "field": "tracking_number", "value": "TRACK-A", "sha256": "a" * 64},
            {"artifact_id": "doc_b", "field": "delivery_status", "value": "Delivered", "sha256": "b" * 64}]}
    empty_claim = {"text": "The seller's own statement.", "verdict": "seller_statement",
        "claim_basis": "seller_statement", "bases": ["seller_statement"], "sources": []}
    html = template.render(safe_text=lambda value: str(value), claims=[source_claim, empty_claim],
        document_names={"doc_a": "Carrier receipt.pdf", "doc_b": "Delivery record.pdf"})
    assert html.count('class="claim-source-card"') == 2
    assert 'class="claim-source-value">TRACK-A</dd>' in html
    assert 'class="claim-source-value">Delivered</dd>' in html
    assert "Technical details" in html and "Document fingerprint (SHA-256)" in html
    assert "Your own statement. No document is cited." in html
    assert html.count('class="claim-source-card"') == 2  # the zero-source claim adds no fabricated card
    assert '<details open' not in html


@pytest.mark.local_spec
def test_package_shipment_statements_checks_open_from_index_and_direct_hash(monkeypatch, tmp_path):
    """Capture real package disclosures opened by index links and direct hashes on desktop/mobile."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is unavailable in the worktree venv")

    from urllib.parse import urlsplit

    for key in list(os.environ):
        if key.startswith(("COPILOT_FEATURE_", "PAYPAL_", "OLLAMA_")):
            monkeypatch.delenv(key, raising=False)
    for flag in FLAG_SET:
        monkeypatch.setenv("COPILOT_FEATURE_" + flag.upper(), "true")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(auto_prep, "should_prepare", lambda _norm: False)
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]
    body["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    app = create_app()
    fake = GetOnlyFake(body)
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(client, "package-details-capture-csrf")
    db = config.db_path()
    repo.ensure_schema(db)
    original = b"%PDF-1.4\nSynthetic receipt: carrier accepted parcel.\n%%EOF\n"
    sha = hashlib.sha256(original).hexdigest()
    document_id = repo.insert_document(db, DID, "Synthetic receipt.pdf", "application/pdf", len(original), sha,
        "Tracking number: DEMO-FDX-000001\nCarrier: FedEx\nStatus: delivered", True)
    write_raw(config.uploads_dir(), sha, original)
    package_id = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "The carrier accepted the parcel.", "source_ids": [f"doc_{document_id}"], "basis": "verified"}])
    repo.attach_document(db, package_id, document_id)
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
        "carrier": {"value": "FEDEX", "confidence": "read"}})
    package_path = f"/disputes/{DID}/packages/{package_id}"
    package_url = "http://proofie.test" + package_path
    import tempfile
    out = Path(os.environ.get("UX_PACKAGE_DETAIL_RENDER_DIR") or Path(tempfile.mkdtemp(prefix="ux-package-detail-")) / "render")      # the capture folder is named by the operator; a plain suite run writes to a temporary one
    assert not out.exists(), f"refusing to overwrite evidence directory: {out}"
    out.mkdir(parents=True)
    manifest = {"tested_product_head": _git_head(),
        "fixture": {"dispute": DID, "package_id": package_id, "version": 1,
            "claim": "The carrier accepted the parcel.", "source": "Synthetic receipt.pdf"},
        "method": "real local Chromium; real rendered package template; synthetic DB; GET-only fake; full-page screenshots",
        "viewport_runs": [], "provider_calls": [], "browser_requests": [], "external_requests": []}
    local_origin = "http://proofie.test"

    def serve_local(route):
        parsed = urlsplit(route.request.url)
        assert parsed.netloc == "proofie.test", f"unexpected browser host: {parsed.netloc}"
        assert route.request.method == "GET", f"capture attempted a write: {route.request.method}"
        response = client.get(parsed.path + (("?" + parsed.query) if parsed.query else ""))
        assert response.status_code == 200, f"{parsed.path}: {response.status_code}"
        route.fulfill(status=response.status_code, content_type="text/html; charset=utf-8",
            body=_inline_assets(response.text))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        manifest["browser"] = browser.version
        for width, height in ((1440, 900), (375, 812)):
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.route("http://proofie.test/**", serve_local)
            page.on("request", lambda request: manifest["browser_requests"].append(request.url))
            run = {"viewport": [width, height],
                "capture_method": "expand viewport height past document content; screenshot full_page=False to keep fixed navigation outside content",
                "screenshots": []}

            def capture(name, interaction, target_id=None):
                path = out / f"{width}x{height}-{name}.png"
                content_height = page.evaluate("document.documentElement.scrollHeight")
                capture_height = max(height, content_height + 120)
                page.set_viewport_size({"width": width, "height": capture_height})
                page.wait_for_timeout(50)
                dimensions = page.evaluate("({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})")
                page.screenshot(path=str(path), full_page=False)
                run["screenshots"].append({"name": name, "interaction": interaction,
                    "url": page.url, "path": path.name, "image_width": width,
                    "image_height": capture_height, "capture_viewport": [width, capture_height],
                    "document_width": dimensions["width"], "document_height": dimensions["height"],
                    "open_target": target_id})

            page.goto(package_url, wait_until="load")
            assert page.locator('[data-state="package-contents-index"]').is_visible()
            capture("package-index", "baseline package index")
            for target_id, label in (("shipment-fold", "shipment"),
                                     ("statements-fold", "statements"),
                                     ("checks-fold", "checks")):
                page.goto(package_url, wait_until="load")
                link = page.locator(f'[data-state="package-contents-index"] a[href="#{target_id}"]')
                assert link.is_visible(), f"package index entry missing: {target_id}"
                link.click()
                disclosure = page.locator(f"details#{target_id}")
                assert disclosure.get_attribute("open") is not None, f"index link did not open {target_id}"
                summary = disclosure.locator(":scope > summary")
                assert summary.is_visible()
                capture(f"{label}-from-index", "clicked package index link; existing disclosure opened", target_id)
                summary.click()
                assert disclosure.get_attribute("open") is None, f"disclosure did not close for {target_id}"
                assert page.locator('[data-state="package-contents-index"]').is_visible()
                capture(f"{label}-return", "closed disclosure; remained on package and returned to contents index", target_id)
                main_page = page
                page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
                page.route("http://proofie.test/**", serve_local)
                page.on("request", lambda request: manifest["browser_requests"].append(request.url))
                page.goto(package_url + "#" + target_id, wait_until="load")
                disclosure = page.locator(f"details#{target_id}")
                assert disclosure.get_attribute("open") is not None, f"direct hash did not open {target_id}"
                assert page.evaluate("window.location.hash") == "#" + target_id
                capture(f"{label}-direct-hash", "loaded direct hash; existing disclosure opened", target_id)
                page.close()
                page = main_page
            manifest["viewport_runs"].append(run)
            page.close()
        manifest["provider_calls"] = fake.calls
        manifest["external_requests"] = [url for url in manifest["browser_requests"]
            if not url.startswith(local_origin + "/")]
        assert all(call["method"] == "GET" for call in fake.calls)
        assert not manifest["external_requests"]
        browser.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def test_desktop_seller_journey_interaction_baseline(monkeypatch, tmp_path, request):
    """Capture one navigable, GET-only seller journey before further desktop edits."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is unavailable in the worktree venv")

    from urllib.parse import urlsplit

    from _hosted import authenticated_client, set_test_csrf
    from copilot import config
    from copilot.app import create_app
    from copilot.core import auto_prep
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client

    for key in list(os.environ):
        if key.startswith(("COPILOT_FEATURE_", "PAYPAL_", "OLLAMA_")):
            monkeypatch.delenv(key, raising=False)
    for flag in FLAG_SET:
        monkeypatch.setenv("COPILOT_FEATURE_" + flag.upper(), "true")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]
    body["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    monkeypatch.setattr(auto_prep, "should_prepare", lambda _norm: False)
    app = create_app()
    fake = GetOnlyFake(body)
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(client, "ux-desktop-journey-csrf")
    db = config.db_path()
    repo.ensure_schema(db)
    original = b"%PDF-1.4\nSynthetic receipt: carrier accepted parcel.\n%%EOF\n"
    sha = hashlib.sha256(original).hexdigest()
    document_id = repo.insert_document(db, DID, "Synthetic receipt.pdf", "application/pdf", len(original), sha,
        "Tracking number: DEMO-FDX-000001\nCarrier: FedEx\nStatus: delivered", True)
    write_raw(config.uploads_dir(), sha, original)
    package_id = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "The carrier accepted the parcel.", "source_ids": [f"doc_{document_id}"], "basis": "verified"}])
    repo.attach_document(db, package_id, document_id)
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
        "carrier": {"value": "FEDEX", "confidence": "read"}})
    package_path = f"/disputes/{DID}/packages/{package_id}"
    active_path = package_path + "/active-approval"
    evidence_root = Path(os.environ.get("UX_DESKTOP_JOURNEY_RENDER_DIR", tmp_path / "journey"))
    out = evidence_root / getattr(request.node, "callspec", None).id if hasattr(request.node, "callspec") else evidence_root
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"method": "single-tab local Chromium navigation through real rendered links; TestClient app; synthetic DB; GET-only fake",
        "tested_app_head": _git_head(),
        "fixture": {"dispute": DID, "package_id": package_id, "version": 1,
            "claim": "The carrier accepted the parcel.", "source": "Synthetic receipt.pdf"},
        "viewport": [1440, 900], "steps": [], "provider_calls": [], "browser_requests": []}
    local_origin = "http://proofie.test"

    def serve_local(route):
        request = route.request
        parsed = urlsplit(request.url)
        assert parsed.netloc == "proofie.test", f"unexpected browser host: {parsed.netloc}"
        assert request.method == "GET", f"baseline journey attempted a write: {request.method} {parsed.path}"
        response = client.get(parsed.path + (("?" + parsed.query) if parsed.query else ""))
        assert response.status_code == 200, f"{parsed.path}: {response.status_code}"
        route.fulfill(status=response.status_code, content_type="text/html; charset=utf-8",
            body=_inline_assets(response.text))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        manifest["browser"] = browser.version
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page.route("http://proofie.test/**", serve_local)
        page.on("request", lambda request: manifest["browser_requests"].append(request.url))

        def capture(name, observation):
            path = out / f"{len(manifest['steps']) + 1:02d}-{name}.png"
            page.screenshot(path=str(path), full_page=False)
            heading_text = page.locator("h1").first.inner_text()
            if page.locator("h1 .h1-id").count():
                assert re.search(r"USD\s+Dispute\s+PP-R-DEMO-", heading_text), heading_text
            row = {"name": name, "url": page.url, "screenshot": path.name,
                "heading": heading_text,
                "actions": page.locator("main a:visible, main button:visible").all_text_contents(),
                "observation": observation}
            manifest["steps"].append(row)

        page.goto(local_origin + "/disputes", wait_until="load")
        assert page.locator(f'tr[data-dispute="{DID}"] a.btn').count() == 1
        capture("inbox", "one seller action for the selected synthetic row; two rows are available")
        page.locator(f'tr[data-dispute="{DID}"] a.btn').click()
        assert page.url.endswith("/disputes/" + DID)
        capture("overview", "case identity, synthetic deadline, task navigation and next action")
        page.locator('[data-state="case-more-nav"] > summary').click()
        page.locator('[data-nav="prepare"]').click()
        capture("prepare", "document table, local file action and package-entry link; no file is uploaded")
        page.locator('.case-primary-nav a[href$="/workspace/review"]').click()
        review_notice = page.locator('[data-state="mismatch-unchecked"]').inner_text()
        source = page.locator(".claim-source-card").first
        assert source.is_visible() and "Synthetic receipt.pdf" in source.inner_text()
        assert source.locator(".claim-source-value").is_visible()
        technical = source.locator("details > summary").first
        technical.focus()
        page.keyboard.press("Enter")
        assert technical.locator("xpath=..").get_attribute("open") is not None
        capture("review-source", f"source opened; mismatch state: {review_notice}")
        page.locator('[data-state="full-package-link"] a').click()
        statements = page.locator('[data-state="fold-statements"] > summary')
        statements.click()
        page.locator('a[href$="/claims-review"]').click()
        assert page.locator('[data-state="sources-only-note"]').is_visible()
        capture("source-detail", "read-only source page; source is visible and review is not recorded here")
        page.locator(f'a[href="{package_path}"]').click()
        assert page.locator('[data-state="package-contents-index"]').is_visible()
        page.locator('[data-state="package-contents-index"] a[href="#files-fold"]').click()
        assert page.locator('[data-state="fold-attachments"]').get_attribute("open") is not None
        capture("package-files-index", "visible dynamic index anchor opens the existing Files disclosure")
        page.locator(f'.case-primary-nav a[href="/disputes/{DID}"]').click()
        page.locator('[data-state="case-more-nav"] > summary').click()
        page.locator('[data-nav="approve"]').click()
        capture("approve-preview", "exact package preview and explicit separation of approval from sending")
        page.locator(f'a[href="{active_path}"]').click()
        assert page.locator("form").count() == 1 and page.locator("input.claim-confirm").count() == 1
        capture("human-approval-control", "local seller confirmation is required; baseline does not submit it")
        # Return through the real Overview navigation entry; the visible breadcrumb is compacted away.
        page.locator(f'.case-primary-nav a[href="/disputes/{DID}"]').click()
        page.locator('[data-state="case-more-nav"] > summary').click()
        page.locator('[data-nav="send"]').click()
        send_text = page.locator("main").inner_text()
        assert "Cannot start yet" in send_text and "approve" in send_text.lower()
        capture("send-blocked", "send remains blocked until local approval; no send action is invoked")
        manifest["provider_calls"] = fake.calls
        manifest["external_requests"] = [url for url in manifest["browser_requests"]
            if not url.startswith(local_origin + "/")]
        assert all(call["method"] == "GET" for call in fake.calls)
        assert not manifest["external_requests"]
        browser.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def test_nine_views_render_at_desktop_and_phone_with_local_states(monkeypatch, tmp_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is unavailable in the worktree venv")

    for key in list(os.environ):
        if key.startswith(("COPILOT_FEATURE_", "PAYPAL_", "OLLAMA_")):
            monkeypatch.delenv(key, raising=False)
    for flag in FLAG_SET:
        monkeypatch.setenv("COPILOT_FEATURE_" + flag.upper(), "true")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setattr(config, "load_env_file", lambda: None)

    body = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]
    body["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    # This visual journey pins one package/version across views; background package preparation
    # belongs to its own tests and must not race the source-backed human-edit fixture here.
    monkeypatch.setattr(auto_prep, "should_prepare", lambda _norm: False)
    app = create_app()
    baseline_revision = os.environ.get("UX_CONTINUATION_PRESENTATION_BASELINE")
    baseline_templates = None
    css, js = CSS, JS
    if baseline_revision:
        from jinja2 import ChoiceLoader, DictLoader
        from copilot.web import routes_disputes, routes_package

        baseline_paths = (
            "copilot/web/templates/case/_documents.html",
            "copilot/web/templates/case/_other_responses.html",
            "copilot/web/templates/case/_queue_explanation.html",
            "copilot/web/templates/case_base.html",
            "copilot/web/templates/case_task.html",
            "copilot/web/templates/disputes_list.html",
            "copilot/web/templates/features/claims/_claim_cards.html",
            "copilot/web/templates/package_review.html",
        )
        baseline_templates = {path.removeprefix("copilot/web/templates/"): _git_text(baseline_revision, path)
                              for path in baseline_paths}
        for route_module in (routes_disputes, routes_package):
            route_module.templates.env.loader = ChoiceLoader([
                DictLoader(baseline_templates), route_module.templates.env.loader])
        css = _git_text(baseline_revision, "copilot/web/static/app.css")
        js = _git_text(baseline_revision, "copilot/web/static/app.js")
    fake = GetOnlyFake(body)
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    token = "ux-local-csrf-token"
    set_test_csrf(client, token)
    db = config.db_path()
    repo.ensure_schema(db)

    original = b"%PDF-1.4\nSynthetic receipt: carrier accepted parcel.\n%%EOF\n"
    sha = hashlib.sha256(original).hexdigest()
    doc_id = repo.insert_document(db, DID, "Synthetic receipt.pdf", "application/pdf", len(original), sha,
                                  "Tracking number: DEMO-FDX-000001\nCarrier: FedEx\nStatus: delivered", True)
    write_raw(config.uploads_dir(), sha, original)
    package_id = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "The carrier accepted the parcel.", "source_ids": [f"doc_{doc_id}"], "basis": "verified"}])
    repo.attach_document(db, package_id, doc_id)
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
                                     "carrier": {"value": "FEDEX", "confidence": "read"}})
    package_path = f"/disputes/{DID}/packages/{package_id}"

    routes = {
        "inbox-multifile": "/disputes",
        "overview": f"/disputes/{DID}",
        "prepare-files-local": f"/disputes/{DID}/workspace/prepare",
        "review-sources": f"/disputes/{DID}/workspace/review",
        "claim-source-details": f"{package_path}/claims-review",
        "active-approval": f"{package_path}/active-approval",
        "approve-preview": f"/disputes/{DID}/workspace/approve",
        "send-not-sent": f"/disputes/{DID}/workspace/send",
        "records": f"/disputes/{DID}/workspace/records",
        "other-responses": f"/disputes/{DID}/workspace/other",
        "package-approvable": package_path,
    }
    fullpage_baseline = os.environ.get("UX_CONTINUATION_FULLPAGE_BASELINE")
    baseline_package_html = None
    if fullpage_baseline:
        from jinja2 import ChoiceLoader, DictLoader
        from copilot.web import routes_package
        baseline_map = {"package_review.html": _git_text(fullpage_baseline, "copilot/web/templates/package_review.html")}
        old_loader = routes_package.templates.env.loader
        baseline_css = _git_text(fullpage_baseline, "copilot/web/static/app.css")
        baseline_js = _git_text(fullpage_baseline, "copilot/web/static/app.js")
        try:
            routes_package.templates.env.loader = ChoiceLoader([DictLoader(baseline_map), old_loader])
            baseline_response = client.get(package_path)
            assert baseline_response.status_code == 200
            baseline_package_html = _inline_assets_from(baseline_response.text, baseline_css, baseline_js)
        finally:
            routes_package.templates.env.loader = old_loader
    responses = {name: client.get(path) for name, path in routes.items()}
    assert all(response.status_code == 200 for response in responses.values()), {
        name: response.status_code for name, response in responses.items() if response.status_code != 200}
    assert 'data-dispute="PP-R-DEMO-0000002"' in responses["inbox-multifile"].text
    assert responses["inbox-multifile"].text.count('data-dispute="PP-R-DEMO-') == 2
    packages = repo.list_packages(db, DID)
    assert len(packages) == 1 and int(packages[0]["id"]) == package_id
    package = repo.get_package(db, package_id)
    package_version = int(package["version"])
    assert package_version == 1
    review_text = responses["review-sources"].text
    assert "1 statement in package v1." in review_text
    assert "The carrier accepted the parcel." in review_text
    assert "Synthetic receipt.pdf" in review_text
    if not baseline_revision:
        assert 'class="claim-source-card"' in review_text
        assert 'class="claim-source-value"' in review_text
        assert 'class="claim-source-disclosure"' not in review_text
        checks_details = re.search(
            r'<details class="review-checks-summary" data-state="review-checks-summary">(.*?)</details>',
            review_text, re.S)
        assert checks_details is not None
        assert "See the checks" in checks_details.group(1)
        assert re.search(r"\d+ of \d+ checks verified; \d+ cannot be checked\.", checks_details.group(1))
        assert f'href="/disputes/{DID}/packages/{package_id}#checks"' in checks_details.group(1)
    package_identity = f"/disputes/{DID}/packages/{package_id}"
    for name in ("overview", "review-sources", "approve-preview", "package-approvable"):
        assert package_identity in responses[name].text, name
        assert f"v{package_version}" in responses[name].text, name
    for name, current in (("overview", "Overview"), ("review-sources", "Review claims"),
                          ("package-approvable", "Evidence package")):
        page_html = responses[name].text
        assert 'data-state="case-primary-nav"' in page_html, name
        assert f'>{current}</a>' in page_html, name
        assert 'aria-current="page"' in page_html, name
        assert "Other case links" in page_html and 'data-state="case-more-nav"' in page_html, name
    package_html = responses["package-approvable"].text
    for target_id in ("shipment-fold", "files-fold", "statements-fold", "checks-fold",
                      "what-will-be-sent", "approval"):
        assert f'href="#{target_id}"' in package_html, target_id
        assert f'id="{target_id}"' in package_html, target_id
    assert 'data-state="package-contents-index"' in package_html
    assert re.search(r"1\s+statement\s*·\s*Cannot be verified", package_html)
    active_approval_html = responses["active-approval"].text
    assert 'data-state="approval-workspace"' in active_approval_html
    assert 'aria-label="Package approval and send status"' in active_approval_html
    assert 'data-state="paypal-limitations"' in active_approval_html
    assert "PayPal returned no transaction details" in active_approval_html
    assert "No tracking number is registered in PayPal" in active_approval_html
    assert "Approve this package" in active_approval_html and "Sending is a separate step" in active_approval_html
    overview_text = responses["overview"].text
    assert 'data-state="summary"' in overview_text and "Respond to the shipping request" in overview_text
    assert all(f'data-state="{state}"' in overview_text for state in
               ("status-missing", "status-mismatch", "status-sending", "missing-none",
                "mismatch-unchecked", "paypal-confirmed-none"))
    prepare_text = responses["prepare-files-local"].text
    assert "Review existing files" in prepare_text and "Files stay in your configured Proofie data folder." in prepare_text
    assert prepare_text.index("Synthetic receipt.pdf") < prepare_text.index('action="/disputes/' + DID + '/documents"')
    review_text = responses["review-sources"].text
    assert 'data-state="review-workspace"' in review_text and 'aria-label="Package review status"' in review_text
    assert review_text.index('data-state="review-sources"') < review_text.index('data-state="review-summary"')
    assert 'data-state="review-recording"' in review_text
    assert "Reading here isn't recorded; confirm in Approval." in review_text
    assert review_text.index('data-state="review-sources"') < review_text.index('data-state="review-recording"') < review_text.index('class="actions review-next"')
    assert 'Open statements in the package' in review_text and 'data-state="mismatch-unchecked"' in review_text
    assert "Not available; Not available" not in review_text
    assert "Synthetic receipt.pdf" in review_text and "Cannot be verified" in review_text
    assert 'Open package approval review' in responses["approve-preview"].text
    send_text = responses["send-not-sent"].text
    assert 'data-state="task-state-send"' in send_text and 'Open approval review' in send_text
    assert "Read-back: not sent yet" in send_text and "Cannot start yet." in send_text
    package_text = responses["package-approvable"].text
    assert 'data-state="review-vs-send"' in package_text
    assert "Ready to review; check the groups below for missing, differing or unverifiable facts before sending." in package_text
    assert 'data-state="package-workspace"' in package_text and 'aria-label="Package state"' in package_text
    ordered_package_markers = ('data-state="paypal-limitations"', 'data-state="package-contents-index"', 'id="next-step"',
        'id="shipment-details"', 'id="attachments"', 'id="statements"', 'id="checks"',
        'id="what-will-be-sent"', 'id="approval"', 'id="where-it-stands"',
        'data-state="where-it-stands-details"')
    positions = [package_text.index(marker) for marker in ordered_package_markers]
    assert positions == sorted(positions), positions
    assert package_text.index('data-state="package-chip"') > package_text.index('id="where-it-stands"')
    assert package_text.index('data-state="paypal-limitations"') < package_text.index('id="next-step"')
    assert "Approving sends nothing. Sending is a separate step." in package_text
    assert "Required to send" in package_text and "What PayPal did not return" in package_text
    assert "The carrier accepted the parcel." in package_text and "Synthetic receipt.pdf" in package_text
    assert all(call["method"] == "GET" for call in fake.calls)

    # A local seller action on this isolated synthetic package produces the approved-not-sent visual state.
    import importlib      # a feature package is imported by name: tests/local publishes with or without the namespace (export gap rule A2)
    rules = importlib.import_module("copilot.features.active_approval.rules")
    approval_service = importlib.import_module("copilot.features.active_approval.service")
    approval_page = client.get(package_path + "/active-approval")
    assert approval_page.status_code == 200
    responses["active-approval-review"] = approval_page
    assert 'data-state="case-primary-nav"' in approval_page.text
    assert 'href="' + package_path + '" aria-current="page">Evidence package</a>' in approval_page.text
    assert 'id="approval-control"' in approval_page.text and "Approving does not send anything. Sending is a separate step." in approval_page.text
    assert 'name="csrf_token"' in approval_page.text and 'method="post"' in approval_page.text
    approval_review = approval_service.read(fake, db, DID, package_id)
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', approval_page.text)
    assert csrf_match is not None
    approval_form = {"csrf_token": csrf_match.group(1), "review_sha256": approval_review.review_sha256}
    approval_form.update({"claim-" + claim.key:
        ("seller_statement" if claim.verdict == "unverifiable" else "confirmed")
        for claim in approval_review.claims})
    if approval_review.reasons:
        approval_form["phrase"] = rules.PHRASE
    if approval_review.flags:
        approval_form["sufficiency_override"] = "Synthetic local review fixture"
    approved = client.post(package_path + "/active-approval", data=approval_form, follow_redirects=False)
    assert approved.status_code == 303, approved.text
    responses["package-approved-not-sent"] = client.get(package_path)
    assert responses["package-approved-not-sent"].status_code == 200

    # Synthetic local read-back marker only; no PayPal request and no send route is called.
    claims = repo.list_claims(db, package_id)
    request_input = build_request_input(claims, "DEMO-FDX-000001", "FEDEX")
    content_sha = sha256_json(request_input)
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(DID, content_sha)
        store.write(DID, content_sha, {"state": "confirmed", "request_id": "local-ux-readback",
            "request_input": request_input, "attachment_filenames": []})
    finally:
        store.close()
    responses["package-sent-readback"] = client.get(package_path)
    assert responses["package-sent-readback"].status_code == 200
    assert fake.calls and all(call["method"] == "GET" for call in fake.calls)

    out = Path(os.environ.get("UX_CONTINUATION_RENDER_DIR", tmp_path / "render"))
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"method": "real local Chromium; in-memory inline app.css/app.js; synthetic DB; GET-only fake",
        "fixture": "one synthetic seller dispute; two inbox rows; one source-backed package",
        "tested_app_head": _git_head(),
        "presentation_identity": f"git-show baseline {baseline_revision}; overlay files: {sorted(baseline_templates)}"
            if baseline_revision else f"working-tree presentation based on committed c59ac0e2; test source sha256 {hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}",
        "states": ["approvable package", "approved-not-sent local approval", "synthetic local confirmed read-back"],
        "provider_calls": fake.calls, "screens": [], "viewports": [], "checks": {}, "external_requests": []}
    # Add the two-option configuration without rebuilding the DB or changing routes: flags are read per request.
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_DRAFT", "true")
    multi_option_response = client.get(f"/disputes/{DID}/workspace/other")
    assert multi_option_response.status_code == 200
    responses["other-responses-resolution-draft"] = multi_option_response
    prepared = {name: _inline_assets_from(response.text, css, js) for name, response in responses.items()}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        manifest["browser"] = browser.version
        try:
            for width, height in ((1440, 900), (375, 812)):
                context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                page = context.new_page()
                page.on("request", lambda request: manifest["external_requests"].append(request.url))
                for name, html in prepared.items():
                    page.set_content(html, wait_until="load")
                    page.wait_for_timeout(20)
                    metrics = page.evaluate("""() => {
                      const box = e => { const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; };
                      const primary = [...document.querySelectorAll('main a')].find(e => e.innerText.trim()==='Open statements in the package')
                        || document.querySelector('.review-next .btn-primary') || document.querySelector('.review-summary .btn-primary')
                        || document.querySelector('.next-step .btn-primary');
                      const visible = e => e.getClientRects().length && !e.closest('details:not([open])');
                      const textNodes = [...document.querySelectorAll('.case-body p, .case-body li, .case-body dd, .case-body td')]
                        .filter(visible);
                  // Native controls may retain browser-default typography; measure authored content hierarchy separately.
                  const sizes = [...new Set([...document.querySelectorAll('main *')]
                    .filter(e => visible(e) && !e.matches('button,input,select,textarea'))
                    .map(e => getComputedStyle(e).fontSize))];
                      const weighted = [...document.querySelectorAll('main *')].filter(e => e.getClientRects().length)
                        .map(e => getComputedStyle(e).fontWeight).filter(x => parseInt(x,10) >= 600).length;
                      const allText = [...document.querySelectorAll('main *')].filter(e => visible(e) && e.children.length===0 && !e.closest('.visually-hidden') && !e.closest('[aria-hidden="true"]'));
                      const bold = allText.filter(e => parseInt(getComputedStyle(e).fontWeight,10)>=600).length;
                      const records = document.querySelector('[data-state="audit-timeline"], [data-state="records-timeline"], .audit-timeline');
                      const chips = [...document.querySelectorAll('.tag,.chip')].filter(visible).map(e => ({text:e.innerText.trim(),
                        color:getComputedStyle(e).color, background:getComputedStyle(e).backgroundColor}));
                      const visualBoxes = [...document.querySelectorAll('main .panel, main .banner, main table, main .case-strip, main .review-summary, main details')]
                        .filter(visible).map(e=>({tag:e.tagName,cls:String(e.className||''),border:getComputedStyle(e).borderColor,
                          width:box(e).width,height:box(e).height,background:getComputedStyle(e).backgroundColor}));
                        const rect = selector => { const e=document.querySelector(selector); return e ? box(e) : null; };
                        return {scrollWidth:document.documentElement.scrollWidth, viewportWidth:innerWidth,
                        docHeight:document.documentElement.scrollHeight, mainHeight:box(document.querySelector('main')).height,
                        h1:document.querySelector('h1') ? box(document.querySelector('h1')) : null,
                        caseNav:document.querySelector('.case-nav') ? box(document.querySelector('.case-nav')) : null,
                         primaryNav:rect('.case-primary-nav'),
                         primaryNavLinks:[...document.querySelectorAll('.case-primary-nav a')].map(box),
                         moreNav:rect('.case-more-nav'),
                         overviewMain:rect('.overview-main'), overviewRail:rect('.overview-context-rail'),
                         reviewMain:rect('.review-main'), reviewRail:rect('.review-rail'),
                         packageMain:rect('.package-main'), packageRail:rect('.package-state-rail'),
                        approvalMain:rect('.approval-main'), approvalRail:rect('.approval-context-rail'),
                        reviewPrimary:primary ? box(primary) : null,
                        reviewCheckSummary:(() => { const d=document.querySelector('[data-state="review-checks-summary"]'); return d ? {open:d.open, summary:d.querySelector('summary')?.innerText, href:d.querySelector('a')?.getAttribute('href'), text:d.textContent.trim()} : null; })(),
                        wordsBeforeReviewPrimary:primary ? textNodes.filter(e=>e.getBoundingClientRect().bottom<=primary.getBoundingClientRect().top)
                          .map(e=>e.innerText||e.textContent).join(' ').trim().split(/\\s+/).filter(Boolean).length : null,
                        textSizes:sizes, boldRatio:allText.length ? bold/allText.length : 0,
                        textSizeElements:[...document.querySelectorAll('main *')].filter(e=>visible(e) && e.children.length===0 && !e.closest('.visually-hidden') && !e.closest('[aria-hidden="true"]') && !['14px','16px','22px','28px'].includes(getComputedStyle(e).fontSize)).map(e=>({tag:e.tagName,cls:e.className,size:getComputedStyle(e).fontSize,text:(e.innerText||e.textContent).trim()})),
                        boldText:allText.filter(e=>parseInt(getComputedStyle(e).fontWeight,10)>=600).map(e=>({tag:e.tagName,cls:e.className,text:(e.innerText||e.textContent).trim()})),
                        openDetails:[...document.querySelectorAll('details[open]')].length,
                        sourceCards:document.querySelectorAll('.claim-source-card').length,
                        sourceValues:[...document.querySelectorAll('.claim-source-value')].map(e=>e.innerText.trim()),
                        details:[...document.querySelectorAll('main details')].map(e=>({summary:e.querySelector('summary')?.innerText||'',
                          open:e.open,height:Math.round(e.getBoundingClientRect().height)})),
                        visibleOtherLinks:[...document.querySelectorAll('[data-state="other-responses-list"] a')].filter(visible).length,
                        maxVisibleNavStateWords:Math.max(0,...[...document.querySelectorAll('.case-nav-state')].map(e=>e.innerText.trim().split(/\\s+/).filter(Boolean).length)),
                        navAccessibleLabels:[...document.querySelectorAll('.case-nav a')].every(e=>e.getAttribute('aria-label') && e.title),
                        inboxFirstRow:document.querySelector('[data-dispute]') ? box(document.querySelector('[data-dispute]')) : null,
                        heading:[...document.querySelectorAll('main h1,main h2,main h3')].filter(visible).map(e=>({text:e.innerText.trim(),...box(e)})),
                        primaries:[...document.querySelectorAll('main .btn-primary,main button[type="submit"]')].filter(visible).map(e=>({text:e.innerText.trim(),...box(e)})),
                        safety:[...document.querySelectorAll('[data-safety],[data-state="mismatch-unchecked"],[data-state="inbox-inaction"],[data-state="checked-at"]')].filter(visible).map(e=>e.innerText.trim()),
                        recordsBoxCensus:visualBoxes, chipColors:chips,
                        inboxRows:document.querySelectorAll('tr[data-dispute]').length,
                        inboxActions:[...document.querySelectorAll('tr[data-dispute] a.btn')].map(e=>({text:e.innerText.trim(),href:e.getAttribute('href'),aria:e.getAttribute('aria-label')})),
                        inboxSummary:document.querySelector('[data-state="inbox-summary"]')?.innerText.trim(),
                        dueCells:[...document.querySelectorAll('tr[data-dispute] td[data-label="Respond by"]')].map(e=>e.innerText.trim())};
                    }""")
                    path = out / f"{name}-{width}x{height}.png"
                    page.screenshot(path=str(path), full_page=False)
                    if name == "active-approval-review" and width == 1440:
                        full_path = out / "active-approval-review-fullpage-1440x900.png"
                        page.screenshot(path=str(full_path), full_page=True)
                        manifest["screens"].append({"name": "active-approval-review-fullpage",
                            "viewport": [width, height], "path": full_path.name,
                            "document_height": metrics["docHeight"], "full_page": True})
                    (out / "debug-metrics.json").write_text(json.dumps({"name": name, "viewport": [width, height],
                        "metrics": metrics}, indent=2), encoding="utf-8")
                    if not baseline_revision:
                        assert metrics["scrollWidth"] <= metrics["viewportWidth"], (name, width, metrics)
                        # B uses bold labels for case facts and source provenance; a global
                        # ratio or fixed font-size census is not a legibility property.
                        assert page.locator("main").is_visible()
                        assert all(h["height"] > 0 and h["width"] > 0 for h in metrics["heading"])
                    if name == "package-approvable" and not baseline_revision:
                        # B keeps case identity and navigation above the package; verify the
                        # meaningful viewport property instead of the old fixed y coordinate.
                        heading = page.locator("main h1")
                        assert heading.is_visible() and heading.bounding_box()["y"] < height
                        case_bar = page.locator(".b-case-bar")
                        assert case_bar.is_visible() and DID in case_bar.inner_text()
                        assert "case status" in case_bar.inner_text().lower() and "respond by" in case_bar.inner_text().lower()
                        assert case_bar.locator('[data-state="case-primary-nav"]').is_visible()
                        assert case_bar.bounding_box()["y"] < heading.bounding_box()["y"]
                        notice_box = page.locator('[data-state="paypal-limitations"]').bounding_box()
                        assert notice_box and notice_box["y"] < height, (name, width, notice_box)
                        if width == 1440:
                            assert metrics["docHeight"] <= 3600, (name, width, metrics["details"])
                            assert metrics["openDetails"] <= 3, metrics
                            assert metrics["wordsBeforeReviewPrimary"] <= 60, metrics
                    if name == "review-sources" and not baseline_revision:
                        primary = metrics["reviewPrimary"]
                        assert primary, metrics
                        claim = page.locator(".claim-sentence").first
                        source_card = page.locator(".claim-source-card").first
                        source_value = source_card.locator(".claim-source-value")
                        assert claim.is_visible() and source_card.is_visible() and source_value.is_visible()
                        assert source_value.inner_text().strip() and "Synthetic receipt.pdf" in source_card.inner_text()
                        assert claim.bounding_box()["y"] < primary["y"]
                        assert source_value.bounding_box()["y"] < primary["y"]
                        if width == 1440:
                            assert primary["y"] < metrics["docHeight"] and claim.bounding_box()["y"] < height, metrics
                        else:
                            # Long source values stay visible above the continuation action in normal mobile flow;
                            # the CTA may be below the initial viewport rather than clipping source text.
                            assert source_value.bounding_box()["y"] < primary["y"] and metrics["scrollWidth"] <= width, metrics
                        if width == 1440:
                            assert metrics["reviewRail"]["x"] > metrics["reviewMain"]["x"]
                            assert source_card.bounding_box()["x"] > claim.bounding_box()["x"]
                        else:
                            assert metrics["reviewRail"]["y"] > metrics["reviewMain"]["y"]
                        assert metrics["reviewCheckSummary"]["open"] is False, metrics
                        assert "cannot be checked" in metrics["reviewCheckSummary"]["text"]
                        assert metrics["reviewCheckSummary"]["href"].endswith(f"/packages/{package_id}#checks")
                        assert page.locator('[data-state="mismatch-unchecked"]').is_visible()
                        assert "No differences found" in page.locator('[data-state="mismatch-unchecked"]').inner_text()
                        page.locator('[data-state="review-checks-summary"] > summary').focus()
                        page.keyboard.press("Enter")
                        assert page.locator('[data-state="review-checks-summary"]').get_attribute("open") is not None
                        assert page.locator('[data-state="review-checks-summary"] p').first.is_visible()
                    if name == "claim-source-details" and not baseline_revision:
                        assert metrics["sourceCards"] == 1 and metrics["sourceValues"][0], metrics
                    if name == "overview" and width == 1440 and not baseline_revision:
                        nav_links = metrics["primaryNavLinks"]
                        assert nav_links and max(a["y"] for a in nav_links) == min(a["y"] for a in nav_links), metrics
                        assert metrics["primaryNav"]["height"] <= max(a["height"] for a in nav_links) + 12, metrics
                        assert metrics["overviewRail"]["x"] > metrics["overviewMain"]["x"]
                        assert metrics["maxVisibleNavStateWords"] <= 3 and metrics["navAccessibleLabels"], metrics
                    if name == "package-approvable" and not baseline_revision:
                        if width == 1440:
                            assert metrics["packageRail"]["x"] > metrics["packageMain"]["x"], metrics
                        else:
                            assert metrics["packageRail"]["y"] > metrics["packageMain"]["y"], metrics
                        assert page.locator('[data-state="paypal-limitations"]').is_visible()
                        assert page.locator('[data-state="where-it-stands"]').is_visible()
                        assert page.locator('[data-state="package-chip"]').is_visible()
                    if name in ("overview", "review-sources", "package-approvable", "active-approval") and not baseline_revision:
                        primary_nav = page.locator('[data-state="case-primary-nav"]')
                        assert primary_nav.is_visible()
                        assert primary_nav.locator('a[aria-current="page"]').count() == 1
                        more = page.locator('[data-state="case-more-nav"]')
                        summary = more.locator(":scope > summary")
                        summary.focus()
                        page.keyboard.press("Tab")
                        page.keyboard.press("Shift+Tab")
                        assert page.evaluate("e => document.activeElement === e", summary.element_handle())
                        assert summary.evaluate("e => getComputedStyle(e).outlineStyle") != "none"
                        page.keyboard.press("Enter")
                        assert more.get_attribute("open") is not None
                        assert more.locator(".case-nav a").first.is_visible()
                        page.keyboard.press("Enter")
                        assert more.get_attribute("open") is None
                    if name == "inbox-multifile" and width == 1440 and not baseline_revision:
                        assert metrics["inboxFirstRow"]["y"] <= 450, metrics
                    if name == "inbox-multifile" and not baseline_revision:
                        assert metrics["inboxRows"] == 2 and len(metrics["inboxActions"]) == 2, metrics
                        assert all(a["href"].startswith("/disputes/") and a["text"].startswith("Respond to this dispute")
                                   for a in metrics["inboxActions"]), metrics
                        assert all(a["aria"] or "for " in a["text"] for a in metrics["inboxActions"]), metrics
                        assert "Soonest:" not in metrics["inboxSummary"]
                        assert all("Respond by" not in cell for cell in metrics["dueCells"])
                        tip = page.locator('[data-state="inbox-inaction"] .tip-btn')
                        tip.focus()
                        tip_rect = tip.evaluate("e => { const r=e.getBoundingClientRect(); return {width:r.width,height:r.height,outline:getComputedStyle(e).outlineStyle}; }")
                        assert tip_rect["width"] >= 44 and tip_rect["height"] >= 44, tip_rect
                        assert tip_rect["outline"] != "none"
                        # Existing toggletip opens on keyboard focus; native details below cover Enter-to-open.
                        assert tip.get_attribute("aria-expanded") == "true"
                        assert tip.get_attribute("aria-describedby")
                        page.keyboard.press("Enter")
                        assert tip.get_attribute("aria-expanded") == "true"
                        assert tip.evaluate("e => e.closest('[data-tip]').classList.contains('tip-pinned')")
                        assert page.locator('[data-state="inbox-inaction"] .tip-body').is_visible()
                        page.keyboard.press("Escape")
                        assert tip.get_attribute("aria-expanded") == "false"
                        page.locator("h1").click(position={"x": 4, "y": 4})
                        assert tip.get_attribute("aria-expanded") == "false"
                    if name == "active-approval" and not baseline_revision:
                        assert page.locator('[data-state="approval-workspace"]').is_visible()
                        assert page.locator('[data-state="paypal-limitations"]').is_visible()
                        assert page.locator('[aria-label="Package approval and send status"]').is_visible()
                        assert "Not sent yet" in page.locator('.approval-context-rail').inner_text()
                        if width == 1440:
                            assert metrics["approvalRail"]["x"] > metrics["approvalMain"]["x"], metrics
                        else:
                            assert metrics["approvalRail"]["y"] > metrics["approvalMain"]["y"], metrics
                        assert page.locator('#approval-control button[type="submit"]').count() == 1
                        assert page.locator('input.claim-confirm[required]').count() == 1
                    if name == "records" and not baseline_revision:
                        assert metrics["recordsBoxCensus"] and metrics["chipColors"], metrics
                    if name == "review-sources" and not baseline_revision:
                        assert "mismatch-unchecked" in page.locator('[data-state="mismatch-unchecked"]').get_attribute("data-state")
                    if name == "prepare-files-local" and not baseline_revision:
                        note = page.locator('[data-safety="files-local"]')
                        assert note.is_visible()
                        assert "configured Proofie data folder" in note.inner_text()
                        tip_button = note.locator(".tip-btn")
                        assert tip_button.count() == 1
                        tip_button.focus()
                        page.keyboard.press("Enter")
                        assert "Leaving a file out of checks does not delete it." in note.locator(".tip-body").inner_text()
                    if name in ("review-sources", "claim-source-details") and not baseline_revision:
                        summary = page.locator(".claim-source-card details > summary").first
                        summary.focus()
                        page.keyboard.press("Enter")
                        assert page.locator(".claim-source-card details").first.get_attribute("open") is not None
                    if name in ("other-responses", "other-responses-resolution-draft") and not baseline_revision:
                        assert metrics["scrollWidth"] <= width, metrics
                        assert metrics["visibleOtherLinks"] == 1, metrics
                        lead = page.locator('[data-state="other-leading"] a')
                        assert lead.count() == 1
                        expected_lead = (f"/fulfilment/DEMOTXN0000000001?from_dispute={DID}" if name == "other-responses"
                                         else f"/disputes/{DID}/resolution")
                        assert lead.get_attribute("href") == expected_lead
                        if name == "other-responses-resolution-draft":
                            more = page.locator('[data-state="more-options"]')
                            assert more.count() == 1 and more.get_attribute("open") is None
                            summary = more.locator(":scope > summary")
                            summary.focus()
                            page.keyboard.press("Enter")
                            assert more.get_attribute("open") is not None
                            assert page.locator('[data-state="other-responses-list"] a').evaluate_all(
                                "els => els.map(e => e.getAttribute('href'))") == [
                                    f"/disputes/{DID}/resolution", f"/disputes/{DID}/draft",
                                    f"/fulfilment/DEMOTXN0000000001?from_dispute={DID}"]
                        else:
                            assert page.locator('[data-state="more-options"]').count() == 0
                    manifest["screens"].append({"name": name, "viewport": [width, height], "path": path.name,
                        "metrics": metrics})
                context.close()
                manifest["viewports"].append([width, height])

            # These package states are displayed independently; the seeded read-back is synthetic local data.
            for name in ("package-approved-not-sent", "package-sent-readback"):
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.set_content(prepared[name], wait_until="load")
                content = page.locator("body").inner_text()
                assert "Approved" in content if name == "package-approved-not-sent" else "confirmed" in content.lower()
                path = out / f"{name}-1440x900.png"
                page.screenshot(path=str(path), full_page=False)
                manifest["screens"].append({"name": name, "viewport": [1440, 900], "path": path.name,
                    "state": "local synthetic only"})
                page.close()
            if baseline_package_html:
                for width, height in ((1440, 900), (375, 812)):
                    for label, html in (("baseline", baseline_package_html),
                                        ("final", prepared["package-approvable"])):
                        page = browser.new_page(viewport={"width": width, "height": height})
                        page.set_content(html, wait_until="load")
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(20)
                        doc_height = page.evaluate("document.documentElement.scrollHeight")
                        path = out / f"package-full-{label}-{width}x{height}.png"
                        page.screenshot(path=str(path), full_page=True)
                        manifest["screens"].append({"name":f"package-full-{label}",
                            "viewport":[width,height],"path":path.name,"document_height":doc_height,
                            "fixture":f"{DID}/package/{package_id}/v{package_version}",
                            "presentation":fullpage_baseline if label == "baseline" else "working tree"})
                        page.close()
                manifest["fullpage_comparison"] = {"baseline_revision":fullpage_baseline,
                    "same_dispute_package_version":f"{DID}/{package_id}/v{package_version}",
                    "note":"Same synthetic DB and package identity; rendered at separate times, not pixel equivalence."}
            # Positive mismatch is deliberately synthetic and read-only: it exercises the visible warning path.
            fake.tracking_body = {"trackers": [{"transaction_id": "DEMOTXN0000000001",
                "tracking_number": "DEMO-FDX-999999", "carrier": "FEDEX", "status": "SHIPPED"}]}
            mismatch = client.get(f"/disputes/{DID}/workspace/review")
            assert mismatch.status_code == 200
            for width, height in ((1440, 900), (375, 812)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.set_content(_inline_assets(mismatch.text), wait_until="load")
                warning = page.locator('[data-state="mismatch-unchecked"]')
                assert warning.is_visible() and "difference" in warning.inner_text().lower()
                assert not warning.evaluate("e => !!e.closest('details:not([open])')")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                path = out / f"review-positive-mismatch-{width}x{height}.png"
                page.screenshot(path=str(path), full_page=False)
                manifest["screens"].append({"name":"review-positive-mismatch","viewport":[width,height],"path":path.name,
                    "warning":warning.inner_text()})
                page.close()
        finally:
            browser.close()
    assert not manifest["external_requests"], manifest["external_requests"]
    assert fake.calls and all(call["method"] == "GET" for call in fake.calls)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
