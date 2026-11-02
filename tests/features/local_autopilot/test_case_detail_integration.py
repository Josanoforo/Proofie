"""The local evaluator consumes current case state and its result returns to the same case."""
import hashlib
import json
import os
import re
from pathlib import Path

from _hosted import authenticated_client
from fastapi.testclient import TestClient
import pytest


@pytest.fixture(autouse=True)
def no_external_model_or_network(monkeypatch):
    from copilot.core import auto_prep, evidence_model, vision_model
    def forbidden(*args, **kwargs):
        raise AssertionError("external network or model invocation forbidden in this test")
    monkeypatch.setattr(auto_prep, "should_prepare", lambda norm: False)
    monkeypatch.setattr(auto_prep, "propose_package", forbidden)
    monkeypatch.setattr(auto_prep, "claims_for_dispute_images", forbidden)
    monkeypatch.setattr(evidence_model, "propose_package", forbidden)
    monkeypatch.setattr(vision_model, "claims_for_dispute_images", forbidden)
    import socket
    monkeypatch.setattr(socket, "create_connection", forbidden)
    connect = socket.socket.connect
    def connect_loopback_pair_only(sock, address):
        if not (isinstance(address, tuple) and address[0] == "127.0.0.1" and address[1] != 11434):
            return forbidden(sock, address)
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect_loopback_pair_only)


def test_inbox_case_policy_simulation_dedup_and_changed_state_exception(monkeypatch, tmp_path):
    """Expected before run: ordinary inbox/detail uses the same DB evidence and recorded offer link; one explicit
    2500-cent standard offer is simulated, exact repeat dedups, then a changed resolved snapshot creates a durable
    exception visible in the same case. Existing resolution preparation link remains visible; fake transport is GET-only.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.core import auto_prep
    monkeypatch.setattr(auto_prep, "should_prepare", lambda norm: False)
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.features.local_autopilot import routes as autopilot_routes
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "configured-data"))
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_AUTOPILOT", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_NEGOTIATION_DRAFTS", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    case_id = "PP-R-DEMO-0000001"
    fixture_path = Path(__file__).resolve().parents[3] / "pilot" / "fixtures" / "synthetic" / \
        "dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
    waiting = json.loads(fixture_path.read_text(encoding="utf-8"))["body"]
    from datetime import datetime, timedelta, timezone
    waiting["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")   # relative: the fixture's own date passes
    store_path = tmp_path / "configured-data" / "mejoras10" / "09" / "simulation.json"
    evidence_text = "Synthetic saved delivery record for current dispute"
    evidence_bytes = evidence_text.encode("utf-8")
    digest = hashlib.sha256(evidence_bytes).hexdigest()
    document_id = repo.insert_document(config.db_path(), case_id, "delivery-note.txt", "text/plain",
                                       len(evidence_bytes), digest, evidence_text, True)
    write_raw(config.uploads_dir(), digest, evidence_bytes)
    package_id = repo.insert_package(config.db_path(), case_id, "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "Seller-recorded delivery note.", "source_ids": [f"doc_{document_id}"], "basis": "seller_statement"}])

    class ReadOnlyFake:
        def __init__(self): self.calls, self.snapshot = [], waiting
        def call(self, method, path, operation, **kwargs):
            self.calls.append((method, path, operation))
            assert method == "GET", f"unexpected external write: {method} {path}"
            if operation == "list_disputes": return {"items": [self.snapshot], "links": []}
            if operation in {"show_dispute", "resolution_read"}: return self.snapshot
            if operation == "context_transaction_search": return {"transaction_details": []}
            if operation == "context_tracking": return {"trackers": []}
            raise AssertionError(f"unexpected GET operation {operation}: {path}")

    fake = ReadOnlyFake()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    inbox = client.get("/disputes")
    assert inbox.status_code == 200 and case_id in inbox.text
    assert client.get(f"/disputes/{case_id}").status_code == 200

    def run_current_snapshot():
        page = client.get(f"/disputes/{case_id}/workspace/hackathon")
        # the practice form is only drawn while an action is open; the token then comes from the Prepare view's upload form
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text) or re.search(
            r'name="csrf_token" value="([^"]+)"', client.get(f"/disputes/{case_id}/workspace/prepare").text)
        assert page.status_code == 200 and match and 'data-state="integrated-local-autopilot"' in page.text
        assert "A person reviews the specific draft" in page.text
        assert "doing nothing never approves an action" in page.text
        panel = page.text.split('data-state="integrated-local-autopilot"', 1)[1].split("</section>", 1)[0]
        for raw in ("integer cents", "record(s)", " cents", "scheduler", "PayPal writer"):
            assert raw not in panel, raw
        assert f"/disputes/{case_id}/resolution" in page.text
        return client.post(f"/disputes/{case_id}/local-autopilot/simulate", data={
            "csrf_token": match.group(1), "profile": "standard", "action": "offer", "amount": "25.00"})

    first = run_current_snapshot()
    assert first.status_code == 200 and "SIMULATED — NOT SENT" in first.text
    assert "Simulated" in first.text and "Offer of 25.00 USD" in first.text
    assert "up to 150.00 USD" in first.text and "ceiling" not in first.text.split('id="local-autopilot"', 1)[1].split("</section>", 1)[0]
    logical_id = re.search(r'local_autopilot_id=([0-9a-f]{64})', first.text).group(1)
    resolution = client.get(f"/disputes/{case_id}/resolution?local_autopilot_id={logical_id}")
    assert resolution.status_code == 200 and 'data-state="autopilot-draft-handoff"' in resolution.text
    assert 'name="amount"' in resolution.text and 'value="25.00"' in resolution.text
    assert logical_id in resolution.text and "2500 cents USD" in resolution.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', resolution.text).group(1)
    rejected = client.post(f"/disputes/{case_id}/resolution/draft", data={
        "csrf_token": csrf, "kind": "partial_offer", "locale": "en", "recipient": "Buyer (recorded fixture)",
        "amount": "20.00", "conditions": "Seller will review the proposal.", "autopilot_logical_id": logical_id})
    assert rejected.status_code == 409 and "amount differs from the imported simulation" in rejected.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', rejected.text).group(1)
    prepared = client.post(f"/disputes/{case_id}/resolution/draft", data={
        "csrf_token": csrf, "kind": "partial_offer", "locale": "en", "recipient": "Buyer (recorded fixture)",
        "amount": "25.00", "conditions": "Seller will review the proposal.", "autopilot_logical_id": logical_id})
    assert prepared.status_code == 200 and 'data-state="draft-simulation-provenance"' in prepared.text
    assert logical_id in prepared.text and "2500 cents USD" in prepared.text
    assert "USD 25.00" in prepared.text and "Seller will review the proposal." in prepared.text
    import html
    handoff = re.search(r'<form method="post" action="/disputes/' + case_id + r'/resolution/preview#preview">(.*?)</form>', prepared.text, re.S)
    assert handoff
    preview_data = {name: html.unescape(value) for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', handoff.group(1))}
    preview = client.post(f"/disputes/{case_id}/resolution/preview", data=preview_data, follow_redirects=False)
    assert preview.status_code == 303
    checked = client.get(preview.headers["location"])
    assert checked.status_code == 200 and "25.00 USD" in checked.text and "USD 25.00" in checked.text

    stale = dict(waiting)
    stale["update_time"] = "2026-10-23T18:30:00Z"
    fake.snapshot = stale
    stale_page = client.get(f"/disputes/{case_id}/resolution?local_autopilot_id={logical_id}")
    assert stale_page.status_code == 200 and 'data-state="autopilot-handoff-refused"' in stale_page.text
    assert 'name="amount"' in stale_page.text and 'value="25.00"' not in stale_page.text
    for amount in ({"currency_code": "USD", "value": "30.00"},
                   {"currency_code": "MXN", "value": "25.00"},
                   None):
        changed_amount = dict(waiting)
        changed_amount["dispute_amount"] = amount
        fake.snapshot = changed_amount
        changed_page = client.get(f"/disputes/{case_id}/resolution?local_autopilot_id={logical_id}")
        assert changed_page.status_code == 200 and 'data-state="autopilot-handoff-refused"' in changed_page.text
    fake.snapshot = waiting
    unsupported = dict(waiting)
    unsupported["dispute_amount"] = {"currency_code": "JPY", "value": "2500"}
    fake.snapshot = unsupported
    unsupported_page = client.get(f"/disputes/{case_id}/workspace/hackathon")
    assert unsupported_page.status_code == 200 and "does not support the currency JPY" in unsupported_page.text
    assert "up to 150.00 USD" not in unsupported_page.text and 'id="autopilot-amount"' not in unsupported_page.text
    fake.snapshot = waiting
    repeated = run_current_snapshot()
    assert repeated.status_code == 200 and "Simulated" in repeated.text

    expired = dict(waiting)
    expired["seller_response_due_date"] = "2000-01-01T00:00:00Z"
    fake.snapshot = expired
    late = run_current_snapshot()
    assert late.status_code == 200 and "Held for review" in late.text
    assert "deadline passed at current request observation" in late.text
    late_id = json.loads(store_path.read_text(encoding="utf-8").splitlines()[1])["logical_id"]
    conflicted_page = client.get(f"/disputes/{case_id}/resolution?local_autopilot_id={late_id}")
    assert conflicted_page.status_code == 200 and 'data-state="autopilot-handoff-refused"' in conflicted_page.text
    assert 'value="25.00"' not in conflicted_page.text
    late_repeat = run_current_snapshot()
    assert late_repeat.status_code == 200 and "Held for review" in late_repeat.text

    resolved = dict(waiting)
    resolved["status"] = "RESOLVED"
    resolved["dispute_outcome"] = {"outcome_code": "RESOLVED_BUYER_FAVOUR"}
    resolved["links"] = [link for link in waiting.get("links", []) if link.get("rel") == "self"]
    fake.snapshot = resolved
    changed = run_current_snapshot()
    assert changed.status_code == 200 and "Held for review" in changed.text
    assert "current state conflict or action stage not open" in changed.text
    assert changed.text.count("SIMULATED — NOT SENT") >= 2
    rows = [json.loads(line) for line in store_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3 and [row["decision"] for row in rows] == ["simulated_action", "exception", "exception"]
    assert "deadline passed at current request observation" in rows[1]["flags"]
    assert rows[0]["amount_cents"] == 2500 and rows[1]["case_id"] == case_id
    assert rows[0]["state_sha256"] != rows[1]["state_sha256"]
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
    for path in (store_path, store_path.with_suffix(store_path.suffix + ".lock")):
        path.unlink(missing_ok=True)


def test_amount_is_typed_as_money_and_parsed_to_integer_cents_by_code():
    """Expected before run: '15.00' -> 1500, '15' -> 1500, '0.5' -> 50, '15.5' -> 1550; '15.005', 'abc', '', '-1', '1,5',
    '1e3', '.5' and non-text raise ValueError carrying the plain help sentence."""
    import pytest
    from copilot.features.local_autopilot import service
    assert [service.parse_amount_cents(t) for t in ("15.00", "15", "0.5", "15.5", " 150.25 ")] == [1500, 1500, 50, 1550, 15025]
    for bad in ("15.005", "abc", "", "-1", "1,5", "1e3", ".5", None, 15):
        with pytest.raises(ValueError, match="at most 2 decimals"):
            service.parse_amount_cents(bad)


def test_case_form_defaults_to_the_disputed_amount_and_rejects_bad_amounts(monkeypatch, tmp_path):
    """Expected before run: the amount field starts at the disputed amount (never above it) and the hint names it; a typed
    '15.005' or 'abc' redirects to the case with the amount error and stores nothing. An offer above the disputed amount is held for review."""
    import re
    from datetime import datetime, timedelta, timezone
    from copilot import config
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "configured-data"))
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_AUTOPILOT", "true")
    case_id = "PP-R-DEMO-0000001"
    fixture_path = Path(__file__).resolve().parents[3] / "pilot" / "fixtures" / "synthetic" / \
        "dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
    waiting = json.loads(fixture_path.read_text(encoding="utf-8"))["body"]
    waiting["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    waiting["dispute_amount"] = {"currency_code": "USD", "value": "15.00"}

    class Fake:
        def call(self, method, path, operation, **kwargs):
            assert method == "GET"
            if operation == "list_disputes": return {"items": [waiting], "links": []}
            if operation in {"show_dispute", "resolution_read"}: return waiting
            if operation == "context_transaction_search": return {"transaction_details": []}
            return {}

    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    page = client.get(f"/disputes/{case_id}/workspace/hackathon")
    panel = page.text.split('id="local-autopilot"', 1)[1].split("</section>", 1)[0]
    assert 'id="autopilot-amount"' in panel and 'value="15.00"' in panel and "The disputed amount is 15.00 USD" in panel and "Proposed amount (USD)" in panel
    assert "(Mexico City time)" in panel
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    for bad in ("15.005", "abc"):
        resp = client.post(f"/disputes/{case_id}/local-autopilot/simulate", data={
            "csrf_token": csrf, "profile": "standard", "action": "offer", "amount": bad}, follow_redirects=False)
        assert resp.status_code == 303 and "local_autopilot_error=amount" in resp.headers["location"]
    again = client.get(f"/disputes/{case_id}/workspace/hackathon?local_autopilot_error=amount")
    assert "at most 2 decimals" in again.text
    store = tmp_path / "configured-data" / "mejoras10" / "09" / "simulation.json"
    assert not store.exists() or not store.read_text(encoding="utf-8").strip()
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', again.text).group(1)
    resp = client.post(f"/disputes/{case_id}/local-autopilot/simulate", data={
        "csrf_token": csrf, "profile": "standard", "action": "offer", "amount": "100.00"})
    assert resp.status_code == 200 and "Held for review" in resp.text
    assert "amount exceeds disputed amount" in resp.text

    # Malformed upstream money must not be invented or recorded by the simulation POST.
    old_lines = store.read_text(encoding="utf-8").splitlines()
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', resp.text).group(1)
    waiting["dispute_amount"] = {"currency_code": "USD", "value": "invalid"}
    malformed = client.post(f"/disputes/{case_id}/local-autopilot/simulate", data={
        "csrf_token": csrf, "profile": "standard", "action": "offer", "amount": "15.00"}, follow_redirects=False)
    assert malformed.status_code == 303 and "local_autopilot_error=1" in malformed.headers["location"]
    assert store.read_text(encoding="utf-8").splitlines() == old_lines
