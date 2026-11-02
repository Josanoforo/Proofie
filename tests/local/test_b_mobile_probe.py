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
from test_ux_rulebook_v2_continuation import DID, FIXTURE, FLAG_SET, GetOnlyFake, _inline_assets
from copilot import config
from copilot.app import create_app
from copilot.core import auto_prep
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.web.routes_disputes import get_client


@pytest.mark.local_spec
def test_b_package_mobile_observation(monkeypatch, tmp_path):
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
    output = Path(os.environ.get("B_MOBILE_PROBE_DIR") or tempfile.mkdtemp(prefix="b-mobile-"))      # the capture folder is named by the operator; a plain suite run writes to a temporary one
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 375, "height": 812}, device_scale_factor=1)
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.set_content(_inline_assets(pages["package"].text), wait_until="load")
        metrics = page.evaluate("""() => { const e=document.querySelector('main h1'); const r=e.getBoundingClientRect(); return {h1:{text:e.innerText,x:r.x,y:r.y,width:r.width,height:r.height}, viewport:{width:innerWidth,height:innerHeight},scrollWidth:document.documentElement.scrollWidth, visible:document.body.innerText.slice(0,2200), nav:[...document.querySelectorAll('nav a')].map(a=>a.innerText)}; }""")
        page.screenshot(path=str(output / "package-mobile-viewport.png"))
        page.screenshot(path=str(output / "package-mobile-fullpage.png"), full_page=True)
        (output / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        assert metrics["scrollWidth"] <= 375
        assert metrics["h1"]["text"] and "Evidence package" in metrics["nav"]
        assert "What PayPal did not return" in metrics["visible"]
        assert not requests and all(c["method"] == "GET" for c in fake.calls)
        browser.close()
