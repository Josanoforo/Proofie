"""The existing outcome page compares case documents with the mapped confirmed package snapshot."""
import hashlib
import os

from fastapi.testclient import TestClient


def test_outcome_page_maps_confirmed_package_and_current_document_hashes(monkeypatch, tmp_path):
    """Expected before run: existing outcome facts are separated from local interpretation; a confirmed same-case
    package freezes its recorded attachment hash; equal bytes under a new name stay unchanged, corrected bytes are new,
    missing official outcome/action stays unknown/unavailable, and no appeal writer control or external POST appears.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    monkeypatch.setenv("COPILOT_FEATURE_APPEAL_WORKSPACE", "true")
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "false")

    resolved_id, unknown_id = "PP-R-M10-0000005", "PP-R-M10-0000006"
    host = "https://api-m.sandbox.paypal.com"
    outcomes = {
        resolved_id: {"dispute_id": resolved_id, "status": "RESOLVED", "dispute_life_cycle_stage": "CHARGEBACK",
                      "reason": "UNAUTHORISED", "dispute_amount": {"currency_code": "USD", "value": "25.00"},
                      "dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"},
                      "links": [{"rel": "self", "href": f"{host}/v1/customer/disputes/{resolved_id}", "method": "GET"},
                                {"rel": "appeal", "href": f"{host}/v1/customer/disputes/{resolved_id}/appeal", "method": "POST"}]},
        unknown_id: {"dispute_id": unknown_id, "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "INQUIRY",
                     "reason": "UNAUTHORISED", "dispute_amount": {"currency_code": "USD", "value": "25.00"},
                     "links": [{"rel": "self", "href": f"{host}/v1/customer/disputes/{unknown_id}", "method": "GET"}]},
    }

    class ReadOnlyPayPal:
        def __init__(self):
            self.calls = []

        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET"
            if path.startswith("/v1/customer/disputes/"):
                return outcomes[path.rsplit("/", 1)[-1]]
            raise AssertionError(f"unexpected transport operation {op}: {path}")

    db, uploads = config.db_path(), config.uploads_dir()
    original_bytes = b"confirmed original delivery record"
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    write_raw(uploads, original_sha, original_bytes)
    original_id = repo.insert_document(db, resolved_id, "delivery.txt", "text/plain", len(original_bytes),
                                       original_sha, "Original delivery record", True)
    package_id = repo.insert_package(db, resolved_id, "human_edit", True, [])
    repo.insert_claims(db, package_id, [{"evidence_index": 0, "claim_index": 0, "text": "Delivery recorded.",
                                        "source_ids": [f"doc_{original_id}"], "basis": "seller_statement"}])
    repo.attach_document(db, package_id, original_id)
    package_sha = "a" * 64
    repo.insert_approval(db, package_id, package_sha, "vendor")
    attempts = SQLiteAttemptStore(db)
    try:
        attempts.write(resolved_id, package_sha, {"state": "confirmed", "request_input": None,
                                                   "attachment_filenames": ["delivery.txt"],
                                                   "attachment_sha256": [original_sha],
                                                   "readback": {"documents_present": True}})
    finally:
        attempts.close()

    renamed_bytes = original_bytes
    renamed_sha = hashlib.sha256(renamed_bytes).hexdigest()
    write_raw(uploads, renamed_sha, renamed_bytes)
    repo.insert_document(db, resolved_id, "delivery-renamed.txt", "text/plain", len(renamed_bytes),
                         renamed_sha, "Same bytes under another name", True)
    corrected = b"corrected delivery record with later scan"
    corrected_sha = hashlib.sha256(corrected).hexdigest()
    write_raw(uploads, corrected_sha, corrected)
    repo.insert_document(db, resolved_id, "delivery-corrected.txt", "text/plain", len(corrected),
                         corrected_sha, "Corrected delivery record", True)

    fake = ReadOnlyPayPal()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))

    resolved = client.get(f"/disputes/{resolved_id}/outcome")
    assert resolved.status_code == 200 and 'data-state="integrated-appeal-workspace"' in resolved.text
    assert "RESOLVED_BUYER_FAVOUR" in resolved.text and "No seller interpretation is stored" in resolved.text
    assert f"Confirmed package v1 (ID {package_id})" in resolved.text
    assert original_sha in resolved.text and corrected_sha in resolved.text
    assert "unchanged content" in resolved.text and "new content" in resolved.text
    assert "current recorded outcome/action facts" not in resolved.text
    assert "A local comparison preview is available" in resolved.text
    assert 'action="/disputes/' + resolved_id + '/outcome/appeal/register"' not in resolved.text

    unknown = client.get(f"/disputes/{unknown_id}/outcome")
    assert unknown.status_code == 200 and "unknown" in unknown.text
    assert "The evidence that was originally sent is unknown" in unknown.text
    assert "No appeal proposal is available" in unknown.text
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
