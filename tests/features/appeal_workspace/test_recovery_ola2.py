from types import SimpleNamespace

from copilot.features.appeal_workspace.service import recovery_context
from copilot.features.case_history import service


def view(norm, decided, link, allowed=True, attempts=()):
    return SimpleNamespace(read=SimpleNamespace(norm=norm, decided=decided, appeal_link_present=link),
                           appeal_allowed=allowed, appeals=attempts)


def comparison(delta="new content", original=True, unavailable=()):
    return {"original_package": {"id": 1} if original else None,
            "proposed_evidence": [{"delta": delta}], "unavailable_sources": unavailable}


def test_outcome_and_novelty_safe_next_actions():
    assert recovery_context(view({}, True, True), comparison())["eligibility"] == "eligible"
    assert recovery_context(view({}, True, False), comparison())["eligibility"] == "not eligible"
    assert recovery_context(view(None, False, False), comparison())["eligibility"] == "unknown"
    assert recovery_context(view({}, True, True), comparison("unchanged content"))["evidence_novelty"] == "duplicate"
    assert recovery_context(view({}, True, True), comparison(original=False))["evidence_novelty"] == "unknown"
    assert recovery_context(view({}, True, True), comparison(unavailable=[{}]))["evidence_novelty"] == "unknown"
    assert "existing appeal preview" in recovery_context(view({}, True, True), comparison())["next_action"]
    for state, word in [("failed", "rejection"), ("sent_unconfirmed", "uncertain"), ("sent_confirmed", "readback")]:
        result = recovery_context(view({}, True, True, attempts=[{"status": state}]), comparison())
        assert word in result["next_action"]


def test_recorded_appeal_history(monkeypatch):
    monkeypatch.setattr(service.repo, "list_packages", lambda *args: [])
    monkeypatch.setattr(service.repo, "list_audit", lambda *args: [
        {"action": "outcome_appeal", "ts": f"2026-10-31T10:0{i}:00Z", "detail": {"status": state}}
        for i, state in enumerate(("failed", "sent_unconfirmed", "sent_confirmed"))])
    class Store:
        def __init__(self, db): pass
        def list_by_dispute(self, dispute): return []
        def close(self): pass
    monkeypatch.setattr(service, "SQLiteAttemptStore", Store)
    monkeypatch.setattr(service.send_service, "registered_attempts", lambda *args: {})
    events = service.build("temporary", "case")["events"]
    assert [e.kind for e in events] == ["attempt", "response", "attempt", "response", "attempt", "read"]
    assert "rejected" in events[1].text and "uncertain" in events[3].text
    assert "change" in events[5].text and "no automatic resend" in events[3].detail[0]


def test_same_case_new_evidence_reaches_existing_preview(monkeypatch, tmp_path):
    import hashlib, os, re
    from fastapi.testclient import TestClient
    from copilot import config
    from copilot.app import create_app
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for flag in ("OUTCOME", "OUTCOME_APPEAL", "APPEAL_WORKSPACE"):
        monkeypatch.setenv("COPILOT_FEATURE_" + flag, "true")
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "false")
    dispute = "PP-R-TST-0000002"
    raw = {"dispute_id": dispute, "status": "RESOLVED", "dispute_life_cycle_stage": "CHARGEBACK",
           "reason": "UNAUTHORISED", "dispute_amount": {"currency_code": "USD", "value": "25.00"},
           "dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"},
           "links": [{"rel": "appeal", "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute}/appeal", "method": "POST"}]}
    class Fake:
        def __init__(self): self.calls = []
        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET" and path.startswith("/v1/customer/disputes/")
            return raw
    db, uploads = config.db_path(), config.uploads_dir()
    def document(name, data):
        digest = hashlib.sha256(data).hexdigest()
        write_raw(uploads, digest, data)
        return repo.insert_document(db, dispute, name, "text/plain", len(data), digest, "Synthetic", True)
    original = document("original.txt", b"original delivery")
    sent = repo.insert_package(db, dispute, "human_edit", True, [])
    repo.insert_claims(db, sent, [{"evidence_index": 0, "claim_index": 0, "text": "Original delivery", "source_ids": [], "basis": "seller_statement"}])
    repo.attach_document(db, sent, original)
    repo.insert_approval(db, sent, "a" * 64, "vendor")
    attempts = SQLiteAttemptStore(db)
    try: attempts.write(dispute, "a" * 64, {"state": "confirmed", "readback": {"documents_present": True}})
    finally: attempts.close()
    added = document("later.txt", b"new delivery scan")
    draft = repo.insert_package(db, dispute, "human_edit", True, [])
    repo.insert_claims(db, draft, [{"evidence_index": 0, "claim_index": 0, "text": "Later scan", "source_ids": [], "basis": "seller_statement"}])
    repo.attach_document(db, draft, added)
    repo.set_manual_fields(db, dispute, {"tracking_number": {"value": "DEMO-123", "confidence": "read"}, "carrier": {"value": "FedEx", "confidence": "read"}})
    fake = Fake()
    app = create_app(); app.dependency_overrides[get_client] = lambda: fake
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    page = client.get(f"/disputes/{dispute}/outcome")
    assert page.status_code == 200 and "new content" in page.text and "later.txt" in page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    preview = client.post(f"/disputes/{dispute}/outcome/appeal/preview", data={"csrf_token": token, "package_id": str(draft)})
    assert preview.status_code == 200 and "later.txt" in preview.text and "DEMO-123" in preview.text
    assert 'data-state="request-sha256"' in preview.text
    assert fake.calls and all(call[0] == "GET" for call in fake.calls)
