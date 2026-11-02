"""The hub keeps the same case identity across Proofie's normal entry points."""
import hashlib
import json
import os
import subprocess
import types
from pathlib import Path

import pytest
from _hosted import authenticated_client
from fastapi.testclient import TestClient


def test_habitual_case_hub_package_and_return_keep_same_ids(monkeypatch, tmp_path):
    """Expected before run: / redirects to the existing inbox; its real local case opens the case-aware hub;
    hub links to the same DB package/document and back; the package's integrated dossier/evaluation panels show
    those IDs and return to that same case. Only injected GET transport is allowed; no private checkout path appears.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_DEMO_HUB", "true")
    monkeypatch.setenv("COPILOT_FEATURE_EVIDENCE_DOSSIER", "true")
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")
    monkeypatch.setenv("COPILOT_FEATURE_EVALUATION_WORKBENCH", "true")
    monkeypatch.setenv("COPILOT_FEATURE_NEGOTIATION_DRAFTS", "true")
    monkeypatch.setenv("COPILOT_FEATURE_APPEAL_WORKSPACE", "true")
    monkeypatch.setenv("COPILOT_FEATURE_MCP_LOCAL_CONTRACTS", "true")
    monkeypatch.setenv("COPILOT_FEATURE_JUDGE_WALKTHROUGH", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "product-data"))

    case_id = "PP-R-DEMO-0000001"
    monkeypatch.setenv("COPILOT_AUTOPREP_EXCLUDE", case_id)
    monkeypatch.setattr(config, "AUTOPREP_EXCLUDE_IDS", frozenset({case_id}))
    fixture = __import__("pathlib").Path(__file__).resolve().parents[3] / "pilot" / "fixtures" / "synthetic" / \
        "dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))["body"]
    source_bytes = b"synthetic source bytes for the same Proofie case"
    digest = hashlib.sha256(source_bytes).hexdigest()
    document_id = repo.insert_document(config.db_path(), case_id, "recorded-order.txt", "text/plain",
                                       len(source_bytes), digest, source_bytes.decode(), True)
    write_raw(config.uploads_dir(), digest, source_bytes)
    package_id = repo.insert_package(config.db_path(), case_id, "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "Recorded order details.", "source_ids": [f"doc_{document_id}"], "basis": "seller_statement"}])
    repo.attach_document(config.db_path(), package_id, document_id)

    class ReadOnlyFake:
        def __init__(self): self.calls = []
        def call(self, method, path, operation, **kwargs):
            self.calls.append((method, path, operation))
            assert method == "GET", f"unexpected provider write: {method} {path}"
            if operation == "list_disputes": return {"items": [raw], "links": []}
            if operation == "show_dispute": return raw
            if operation in {"outcome_read", "resolution_read"}: return raw
            if operation in {"context_transaction_search", "context_tracking"}:
                return {"transaction_details": []} if operation == "context_transaction_search" else {"trackers": []}
            raise AssertionError(f"unexpected transport operation {operation}: {path}")

    fake = ReadOnlyFake()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307 and root.headers["location"] == "/disputes"
    inbox = client.get(root.headers["location"])
    assert inbox.status_code == 200 and case_id in inbox.text
    detail = client.get(f"/disputes/{case_id}")
    assert detail.status_code == 200 and case_id in detail.text
    assert f"/features/local_demo_hub?case_id={case_id}" in detail.text

    hub = client.get(f"/features/local_demo_hub?case_id={case_id}")
    assert hub.status_code == 200
    assert f"case <span translate=\"no\">{case_id.lower()}</span>" in hub.text.lower()
    assert f"/disputes/{case_id}/packages/{package_id}" in hub.text
    assert f"/disputes/{case_id}" in hub.text
    assert str(document_id) in hub.text and digest in hub.text
    assert "C:\\Users\\" not in hub.text

    # Partial flags must not expose links to routers that create_app did not register.
    monkeypatch.delenv("COPILOT_FEATURE_RESOLUTION")
    monkeypatch.delenv("COPILOT_FEATURE_OUTCOME")
    partial_app = create_app()
    partial_app.dependency_overrides[get_client] = lambda: fake
    partial_client = authenticated_client(partial_app, client=("127.0.0.1", 12345))
    partial = partial_client.get(f"/features/local_demo_hub?case_id={case_id}")
    assert partial.status_code == 200
    assert f'href="/disputes/{case_id}/resolution"' not in partial.text
    assert f'href="/disputes/{case_id}/outcome"' not in partial.text
    assert "COPILOT_FEATURE_RESOLUTION" in partial.text and "COPILOT_FEATURE_OUTCOME" in partial.text
    assert partial_client.get(f"/disputes/{case_id}/resolution").status_code == 404
    assert partial_client.get(f"/disputes/{case_id}/outcome").status_code == 404
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")

    # A local MCP note is an explicit, isolated read fixture. This does not invoke the MCP writer/protocol.
    import hashlib as _hashlib
    note_root = tmp_path / "product-data" / "mejoras10" / "06" / "cases" / _hashlib.sha256(case_id.encode()).hexdigest()
    note_root.mkdir(parents=True)
    (note_root / "prepared-notes.json").write_text(json.dumps([
        {"created_at": "fixture-time", "note": "fixture-only note", "status": "prepared locally; not sent"}
    ]), encoding="utf-8")

    connected = client.get(f"/features/local_demo_hub?case_id={case_id}")
    assert f'href="/disputes/{case_id}/resolution"' in connected.text
    assert f'href="/disputes/{case_id}/outcome"' in connected.text
    assert "prepared locally; not sent" in connected.text
    resolution = client.get(f"/disputes/{case_id}/resolution")
    outcome = client.get(f"/disputes/{case_id}/outcome")
    mcp_notes = client.get(f"/disputes/{case_id}/workspace/hackathon")
    walkthrough = client.get(f"/features/judge_walkthrough?case_id={case_id}")
    assert resolution.status_code == 200 and case_id in resolution.text
    assert outcome.status_code == 200 and case_id in outcome.text
    assert 'data-state="outcome-sentence"' in outcome.text
    assert "PayPal has not decided yet." in outcome.text
    assert mcp_notes.status_code == 200 and "fixture-only note" in mcp_notes.text
    assert walkthrough.status_code == 200 and f"/disputes/{case_id}" in walkthrough.text

    package = client.get(f"/disputes/{case_id}/packages/{package_id}")
    assert package.status_code == 200
    assert 'data-state="integrated-evidence-dossier"' in package.text
    assert 'data-state="integrated-evaluation-manifest"' in package.text
    assert f"doc_{document_id}" in package.text and digest in package.text
    back = client.get(f"/features/local_demo_hub?case_id={case_id}")
    assert back.status_code == 200 and case_id in back.text and str(package_id) in back.text
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)


def test_standalone_links_without_case_use_only_their_own_flags(monkeypatch, tmp_path):
    """Expected: a fresh app with hub + each standalone router enabled and resolution/outcome off
    links both feature pages without a case, and each target GET is 200. With a case ID, those same
    entries instead target the case-aware endpoints and are disabled because resolution/outcome are off.
    This proves both fallbacks and the separate-app registration boundary.
    """
    from copilot import config
    from copilot.app import create_app

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "standalone.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "product-data"))
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    for feature in ("LOCAL_DEMO_HUB", "NEGOTIATION_DRAFTS", "APPEAL_WORKSPACE"):
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature}", "true")

    # create_app snapshots route registration. No resolution/outcome routers are registered here.
    app = create_app()
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    hub = client.get("/features/local_demo_hub")
    assert hub.status_code == 200
    assert 'href="/features/negotiation_drafts"' in hub.text
    assert 'href="/features/appeal_workspace"' in hub.text
    assert client.get("/features/negotiation_drafts").status_code == 200
    assert client.get("/features/appeal_workspace").status_code == 200

    case_id = "PP-R-DEMO-0000001"
    contextual = client.get(f"/features/local_demo_hub?case_id={case_id}")
    assert contextual.status_code == 200
    assert f'href="/disputes/{case_id}/resolution"' not in contextual.text
    assert f'href="/disputes/{case_id}/outcome"' not in contextual.text
    assert "COPILOT_FEATURE_RESOLUTION" in contextual.text
    assert "COPILOT_FEATURE_OUTCOME" in contextual.text
    assert client.get(f"/disputes/{case_id}/resolution").status_code == 404
    assert client.get(f"/disputes/{case_id}/outcome").status_code == 404


@pytest.mark.skipif(os.environ.get("P05_CAPTURE") != "true", reason="explicit P05 visual capture only")
def test_visual_case_hub_baseline_and_current_same_fixture(monkeypatch, tmp_path):
    """Expected: full desktop screenshots from real ASGI-rendered hub HTML using the same case,
    package, config, and local CSS. The historical baseline is loaded in memory from 801881fe;
    current code is captured after it. Provider/model/network calls are hard-failed; fake transport
    permits GET only. The baseline displays case links despite absent destination flags; current UI
    explains the required flags and suppresses those links.
    """
    import httpx
    from jinja2 import ChoiceLoader, DictLoader
    from playwright.sync_api import sync_playwright
    from copilot import config
    from copilot.app import create_app
    from copilot.core import evidence_model, vision_model
    from copilot.db import repo
    from copilot.web import routes_disputes
    from copilot.web.routes_disputes import get_client

    def forbidden(*args, **kwargs):
        raise AssertionError("external network or model use is forbidden in this visual harness")

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "visual.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "product-data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    for feature in ("LOCAL_DEMO_HUB", "NEGOTIATION_DRAFTS", "APPEAL_WORKSPACE"):
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature}", "true")
    monkeypatch.setattr(evidence_model, "propose_package", forbidden)
    monkeypatch.setattr(vision_model, "claims_for_dispute_images", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)

    case_id = "PP-R-DEMO-0000001"
    repo.ensure_schema(config.db_path())
    source_bytes = b"Synthetic case-hub visual fixture; no provider record."
    digest = hashlib.sha256(source_bytes).hexdigest()
    document_id = repo.insert_document(config.db_path(), case_id, "visual-fixture.txt", "text/plain",
                                       len(source_bytes), digest, source_bytes.decode(), True)
    package_id = repo.insert_package(config.db_path(), case_id, "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "Recorded synthetic order details.", "source_ids": [f"doc_{document_id}"], "basis": "seller_statement"}])
    repo.attach_document(config.db_path(), package_id, document_id)

    class GetOnlyFake:
        def call(self, method, path, operation, **kwargs):
            assert method == "GET", f"unexpected provider write: {method} {path}"
            raise AssertionError(f"hub GET must not invoke provider transport: {operation} {path}")

    app = create_app()
    app.dependency_overrides[get_client] = lambda: GetOnlyFake()
    client = authenticated_client(app, client=("127.0.0.1", 12345))

    from copilot.features.local_demo_hub import service
    baseline_source = subprocess.check_output([
        "git", "show", "801881fe09aac15aca036bb1a8519a46147f6f45:copilot/features/local_demo_hub/service.py"
    ], text=True, encoding="utf-8")
    baseline_service = types.ModuleType("p05_baseline_hub_service")
    baseline_service.__file__ = str(Path(__file__).resolve().parents[3] / "copilot" / "features" / "local_demo_hub" / "service.py")
    exec(compile(baseline_source, "baseline_local_demo_hub_service.py", "exec"), baseline_service.__dict__)
    baseline_template = subprocess.check_output([
        "git", "show", "801881fe09aac15aca036bb1a8519a46147f6f45:copilot/web/templates/features/local_demo_hub/index.html"
    ], text=True, encoding="utf-8")
    loader = routes_disputes.templates.env.loader
    routes_disputes.templates.env.loader = ChoiceLoader([
        DictLoader({"features/local_demo_hub/index.html": baseline_template}), loader
    ])
    routes_disputes.templates.env.cache.clear()
    output = Path(__file__).resolve().parents[3] / "internal" / "lanes" / "nivel2" / "mejoras-en-producto-2026-10-31" / "runs" / "luna-b"
    css = (Path(__file__).resolve().parents[3] / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    current_page_entries = service.page_entries
    try:
        monkeypatch.setattr(service, "page_entries", baseline_service.page_entries)
        before = client.get(f"/features/local_demo_hub?case_id={case_id}")
        assert before.status_code == 200 and f'href="/disputes/{case_id}/resolution"' in before.text
        routes_disputes.templates.env.loader = loader
        routes_disputes.templates.env.cache.clear()
        monkeypatch.setattr(service, "page_entries", current_page_entries)
        after = client.get(f"/features/local_demo_hub?case_id={case_id}")
        assert after.status_code == 200 and f'href="/disputes/{case_id}/resolution"' not in after.text
        assert f'href="/disputes/{case_id}/outcome"' not in after.text
        assert str(package_id) in before.text and str(package_id) in after.text
        assert "visual-fixture.txt" in before.text and "visual-fixture.txt" in after.text
        assert "COPILOT_FEATURE_RESOLUTION" in after.text and "COPILOT_FEATURE_OUTCOME" in after.text

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            page.route("**/*", lambda route: route.abort())
            for response, filename in ((before, "before.png"), (after, "after.png")):
                page.set_content(response.text, wait_until="domcontentloaded")
                page.add_style_tag(content=css)
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(output / filename), full_page=True)
            browser.close()
    finally:
        routes_disputes.templates.env.loader = loader
        routes_disputes.templates.env.cache.clear()
