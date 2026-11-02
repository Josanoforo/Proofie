"""Capture the actual B product fixture and verify the protected review boundary."""
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from _hosted import authenticated_client, set_test_csrf
from test_ux_rulebook_v2_continuation import DID, FIXTURE, FLAG_SET, GetOnlyFake, _git_head, _git_tracked_status, _inline_assets
from copilot import config
from copilot.app import create_app
from copilot.core import auto_prep
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.web.routes_disputes import get_client


@pytest.mark.local_spec
def test_b_fidelity_capture_and_review_boundary(monkeypatch, tmp_path):
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
    fake = GetOnlyFake(body)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app)
    set_test_csrf(client, "b-fidelity-local")
    db = config.db_path()
    repo.ensure_schema(db)
    raw = b"%PDF-1.4\nSynthetic receipt: carrier accepted parcel.\n%%EOF\n"
    digest = hashlib.sha256(raw).hexdigest()
    doc = repo.insert_document(db, DID, "Synthetic receipt.pdf", "application/pdf", len(raw), digest,
                               "Tracking number: DEMO-FDX-000001\nCarrier: FedEx\nStatus: delivered", True)
    write_raw(config.uploads_dir(), digest, raw)
    pkg = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, pkg, [{"evidence_index": 0, "claim_index": 0,
        "text": "The carrier accepted the parcel.", "source_ids": [f"doc_{doc}"], "basis": "verified"}])
    repo.attach_document(db, pkg, doc)
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
                                     "carrier": {"value": "FEDEX", "confidence": "read"}})
    root = f"/disputes/{DID}/packages/{pkg}"
    routes = {"overview": f"/disputes/{DID}", "review": f"/disputes/{DID}/workspace/review",
              "package": root, "approval": root + "/active-approval"}
    pages = {name: client.get(route) for name, route in routes.items()}
    assert all(p.status_code == 200 for p in pages.values())
    import tempfile
    output = Path(os.environ.get("B_FIDELITY_CAPTURE_DIR") or tempfile.mkdtemp(prefix="b-fidelity-"))      # the capture folder is named by the operator; a plain suite run writes to a temporary one
    output.mkdir(parents=True, exist_ok=True)
    requests = []
    size_elements = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for name, response in pages.items():
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            page.on("request", lambda request: requests.append(request.url))
            page.set_content(_inline_assets(response.text), wait_until="load")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            size_elements[name] = page.evaluate("""() => [...document.querySelectorAll('main *')]
                .filter(e => e.getClientRects().length && !e.closest('details:not([open])') && !e.matches('button,input,select,textarea') && getComputedStyle(e).fontSize === '14px')
                .map(e => ({tag:e.tagName, cls:e.className, text:(e.innerText || '').slice(0,60)}))""")
            page.screenshot(path=str(output / (name + "-1440x900.png")))
            if name in ("package", "approval"):
                page.screenshot(path=str(output / (name + "-fullpage-1440.png")), full_page=True)
            if name == "package":
                approval_link = page.get_by_role("link", name="Open approval review")
                assert approval_link.get_attribute("href") == root + "/active-approval"
                outer = page.locator("#package-edit-sections")
                assert outer.get_attribute("open") is None
                page.locator('.package-contents-index a[href="#shipment-fold"]').click()
                assert outer.get_attribute("open") is not None
                assert page.locator("#shipment-fold").get_attribute("open") is not None
                page.locator("[data-package-return]").first.click()
                assert outer.get_attribute("open") is None
            if name == "review":
                source = page.locator(".claim-source-card").first
                assert source.locator(".claim-source-doc").inner_text() == "Synthetic receipt.pdf"
                assert source.locator(".b-source-details").evaluate("e => !!(e.compareDocumentPosition(e.closest('.claim-source-card').querySelector('details')) & Node.DOCUMENT_POSITION_FOLLOWING)")
            if name == "approval":
                assert page.locator(".claim-source-value .field-hint").inner_text() == "Document text; not a saved shipment-status field."
            page.set_viewport_size({"width": 375, "height": 812})
            page.evaluate("window.scrollTo(0, 0)")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
            page.screenshot(path=str(output / (name + "-375x812.png")))
            menu = page.locator('[data-state="case-more-nav"]')
            menu.locator('summary').first.click()
            assert menu.get_by_role('link', name='All disputes').is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
            menu.locator('summary').first.click()
            page.close()
        browser.close()
    product_paths = ["copilot/web/static/app.css", "copilot/web/static/app.js", "copilot/web/routes_disputes.py",
        "copilot/web/routes_package.py", "copilot/features/active_approval/routes.py", "copilot/features/active_approval/service.py",
        "copilot/web/templates/_case_primary_nav.html", "copilot/web/templates/_components.html", "copilot/web/templates/case_base.html",
        "copilot/web/templates/case_task.html", "copilot/web/templates/dispute_detail.html",
        "copilot/web/templates/case/_summary.html", "copilot/web/templates/case/_sending_status.html",
        "copilot/web/templates/case/_package_sending_status.html", "copilot/web/templates/case/_compact_header.html",
        "copilot/web/templates/features/claims/_claim_cards.html", "copilot/web/templates/package_review.html",
        "copilot/web/templates/features/active_approval/review.html"]
    root_dir = Path(__file__).resolve().parents[2]
    hashes = {path: hashlib.sha256((root_dir / path).read_bytes()).hexdigest() for path in product_paths}
    head = _git_head()
    tracked = _git_tracked_status()
    (output / "manifest.json").write_text(json.dumps({"routes": routes, "requests": requests,
        "fake_calls": fake.calls, "fixture": DID, "viewport": [1440, 900], "size_14": size_elements,
        "head": head, "tracked_status": tracked, "product_sha256": hashes}, indent=2), encoding="utf-8")
    assert not requests
    assert fake.calls and all(call["method"] == "GET" for call in fake.calls)
    assert "Cannot be verified" in pages["review"].text
    assert "Synthetic receipt.pdf" in pages["review"].text
    assert 'name="csrf_token"' in pages["approval"].text
    assert "Approving does not send anything. Sending is a separate step." in pages["approval"].text
    assert 'name="claim-' in pages["approval"].text
