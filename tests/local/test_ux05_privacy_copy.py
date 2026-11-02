"""UX-05 contextual privacy copy: real app routes, offline synthetic state only."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[2]
DID = "PP-R-DEMO-0000001"
FIXTURE = ROOT / "pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
EXPECTED_UPLOAD = (
    "Uploading saves the original file and extracted text in the configured Proofie data folder. "
    "Leaving a file out of checks does not delete it. Preparing a package sends nothing to PayPal; "
    "the send is a separate step after you review and approve the package."
)
VISIBLE_UPLOAD = "Files stay in your configured Proofie data folder."
EXPECTED_PACKAGE = (
    "Removing a file from this response detaches it; its stored copy remains in the configured Proofie data folder. "
    "Nothing is sent to PayPal until you review and approve this package and then send it."
)


class GetOnlyFake:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def call(self, method, path, operation, **kwargs):
        if method != "GET":
            raise AssertionError(f"external write attempted: {method} {operation}")
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
        raise AssertionError(f"unexpected GET operation: {operation}")


def _isolated(monkeypatch, tmp_path, *, hosted=False):
    for key in list(os.environ):
        if key.startswith(("COPILOT_FEATURE_", "PAYPAL_", "OLLAMA_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true" if hosted else "false")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-access-code")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "data" / "evidence"))
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "false")
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    config.set_db_path_override(tmp_path / "data" / "proofie.sqlite")
    config.set_uploads_dir_override(tmp_path / "data" / "uploads")
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]
    config.data_dir().mkdir(parents=True, exist_ok=True)
    repo.ensure_schema(config.db_path())
    raw = b"%PDF-1.4\n% synthetic UX-05 file\n%%EOF\n"
    sha = hashlib.sha256(raw).hexdigest()
    doc_id = repo.insert_document(config.db_path(), DID, "SYNTHETIC UX-05 receipt.pdf",
                                  "application/pdf", len(raw), sha,
                                  "SYNTHETIC fixture text; not provider evidence.", True)
    write_raw(config.uploads_dir(), sha, raw)
    package_id = repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    repo.attach_document(config.db_path(), package_id, doc_id)
    fake = GetOnlyFake(body)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return app, fake, package_id


def test_local_inbox_detail_package_real_browser_render_uses_copy_css_js_and_get_only_fake(monkeypatch, tmp_path):
    app, fake, package_id = _isolated(monkeypatch, tmp_path)
    from playwright.sync_api import sync_playwright

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
    output = ROOT / "data/mejoras10/UX-05/runs"
    output.mkdir(parents=True, exist_ok=True)
    screenshots = output / "final-browser-close2"
    screenshots.mkdir(exist_ok=True)
    observed = {"viewport": {"width": 1440, "height": 900}, "dpr": 1,
                "routes": [], "blocked_external": [], "fake_gets": fake.calls}
    context = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport=observed["viewport"], device_scale_factor=1)
            page = context.new_page()

            def guard(route):
                request = route.request
                if request.url.startswith(base + "/") or request.url == base:
                    if request.method != "GET":
                        observed["blocked_external"].append("non-GET " + request.method + " " + request.url)
                        route.abort()
                    else:
                        route.continue_()
                else:
                    observed["blocked_external"].append(request.url)
                    route.abort()

            page.route("**/*", guard)
            loaded = []
            page.on("response", lambda response: loaded.append(response.url) if response.ok else None)

            def visit(path):
                response = page.goto(base + path, wait_until="networkidle", timeout=30000)
                assert response is not None and response.status == 200, (path, response.status if response else None)
                assert page.locator("link[rel=stylesheet][href*='/static/app.css']").count() == 1
                assert page.locator("script[src*='/static/app.js']").count() == 1
                page.wait_for_function("performance.getEntriesByType('resource').some(x => x.name.includes('/static/app.css'))")
                page.wait_for_function("performance.getEntriesByType('resource').some(x => x.name.includes('/static/app.js'))")
                observed["routes"].append({"path": path, "title": page.title(),
                    "h1": page.locator("h1").first.inner_text(),
                    "css": [x for x in loaded if "/static/app.css" in x],
                    "js": [x for x in loaded if "/static/app.js" in x]})
                return response

            visit("/disputes")
            visit(f"/disputes/{DID}/workspace/prepare#documents")      # v3: the upload note is in the Prepare view
            upload_note = page.locator('[data-state="upload-storage-note"]')
            assert upload_note.is_visible() and VISIBLE_UPLOAD in upload_note.inner_text()
            assert upload_note.get_attribute("data-safety") == "files-local"
            tooltip = upload_note.locator(".tip-body").evaluate("el => el.textContent.trim().replace(/\\s+/g, ' ')")
            assert tooltip == EXPECTED_UPLOAD
            page.screenshot(path=str(screenshots / "detail-upload-note.png"))
            visit(f"/disputes/{DID}/packages/{package_id}")
            # O4: the file controls and their storage note sit together in the "Open package details" block; the note is
            # visible as soon as the block that holds the files is open
            page.locator("#package-edit-sections > summary").click()
            package_note = page.locator('[data-state="attachment-storage-note"]')
            assert package_note.is_visible() and package_note.inner_text() == EXPECTED_PACKAGE
            package_note.evaluate("(element) => element.scrollIntoView()")
            page.screenshot(path=str(screenshots / "package-attachment-note.png"))
            observed["copy"] = {"upload": EXPECTED_UPLOAD, "package": package_note.inner_text()}
            observed["browser_version"] = browser.version
            assert not observed["blocked_external"], observed["blocked_external"]
            assert fake.calls and all(call["method"] == "GET" for call in fake.calls)
            assert len(observed["routes"]) == 3
            context.close()
            context = None
            browser.close()
    finally:
        if context is not None:
            context.close()
        server.should_exit = True
        thread.join(timeout=5)

    (screenshots / "render-observed.json").write_text(json.dumps(observed, indent=2), encoding="utf-8")
    assert not thread.is_alive(), "owned loopback server did not stop"


@pytest.mark.parametrize("variant", ["hosted", "ephemeral"])
def test_hosted_and_opt_in_ephemeral_notices_match_the_selected_storage_variant(monkeypatch, tmp_path, variant):
    if variant == "hosted":
        app, fake, _ = _isolated(monkeypatch, tmp_path, hosted=True)
    else:
        for key in list(os.environ):
            if key.startswith(("COPILOT_FEATURE_", "PAYPAL_", "OLLAMA_")):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
        monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-access-code")
        monkeypatch.setenv("COPILOT_EPHEMERAL_ROOT", str(tmp_path / "ephemeral"))
        monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "false")
        monkeypatch.setattr(config, "load_env_file", lambda: None)
        from copilot.hosted_ephemeral import create_app as create_ephemeral_app
        app = create_ephemeral_app()
    client = TestClient(app, base_url="https://localhost")
    response = client.get("/access")
    assert response.status_code == 200
    if variant == "hosted":
        assert "host restart may erase" in response.text
        assert "same disputes and uploads" in response.text
        assert "Data is cleared on restart" not in response.text
    else:
        assert "Each restart, redeploy, or idle sleep starts this demo with a fresh session." in response.text


