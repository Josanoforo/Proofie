"""Actual package page integration acceptance; fake transport blocks every write."""
import hashlib
import json
from pathlib import Path

from _hosted import authenticated_client
from fastapi.testclient import TestClient


def test_case_package_consumes_saved_claims_and_verified_original_annex_bytes(monkeypatch, tmp_path):
    """Expected before run: the habitual package route renders saved claim/source links, actual annex hashes,
    attached/not-attached/missing-byte status and unresolved source IDs from the same case DB. Transport is GET-only.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "proofie-data"))
    monkeypatch.setenv("COPILOT_FEATURE_EVIDENCE_DOSSIER", "true")
    case_id = "PP-R-DEMO-0000001"
    fixture_path = Path(__file__).resolve().parents[3] / "pilot" / "fixtures" / "synthetic" / \
        "dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
    dispute_raw = json.loads(fixture_path.read_text(encoding="utf-8"))["body"]

    class ReadOnlyFake:
        def __init__(self):
            self.calls = []

        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET"
            if op == "show_dispute":
                return dispute_raw
            if op == "context_transaction_search":
                return {"transaction_details": []}
            if op == "context_tracking":
                return {"trackers": []}
            raise AssertionError(f"unexpected read {op}: {path}")

    fake = ReadOnlyFake()
    config.ensure_data_dir()
    repo.ensure_schema(config.db_path())
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))

    first_bytes = b"PDF-1 synthetic original evidence bytes"
    first_sha = hashlib.sha256(first_bytes).hexdigest()
    first_id = repo.insert_document(config.db_path(), case_id, "carrier-proof.pdf", "application/pdf",
                                    len(first_bytes), first_sha, "Carrier: Example; status: delivered", True)
    write_raw(config.uploads_dir(), first_sha, first_bytes)
    changed_bytes = b"stored digest whose original file has changed"
    changed_sha = hashlib.sha256(changed_bytes).hexdigest()
    changed_id = repo.insert_document(config.db_path(), case_id, "changed-original.pdf", "application/pdf",
                                      len(changed_bytes), changed_sha, "Recorded before change", True)
    (config.uploads_dir() / changed_sha).write_bytes(b"modified bytes at the recorded content path")
    missing_bytes = b"stored digest whose original file is unavailable"
    missing_sha = hashlib.sha256(missing_bytes).hexdigest()
    missing_id = repo.insert_document(config.db_path(), case_id, "missing-original.pdf", "application/pdf",
                                      len(missing_bytes), missing_sha, None, False)
    loose_bytes = b"unattached but stored evidence"
    loose_sha = hashlib.sha256(loose_bytes).hexdigest()
    loose_id = repo.insert_document(config.db_path(), case_id, "unattached.pdf", "application/pdf",
                                    len(loose_bytes), loose_sha, "Order note: synthetic", True)
    write_raw(config.uploads_dir(), loose_sha, loose_bytes)
    package_id = repo.insert_package(config.db_path(), case_id, "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [
        {"evidence_index": 0, "claim_index": 0, "text": "Carrier record says delivered.",
         "source_ids": [f"doc_{first_id}"], "basis": "seller_statement"},
        {"evidence_index": 0, "claim_index": 1, "text": "A second file is cited.",
         "source_ids": [f"doc_{changed_id}", "missing-record-77"], "basis": "buyer_statement"},
        {"evidence_index": 0, "claim_index": 2, "text": "A missing original is cited.",
         "source_ids": [f"doc_{missing_id}"], "basis": "seller_statement"},
        {"evidence_index": 0, "claim_index": 3, "text": "An un-attached source remains linked.",
         "source_ids": [f"doc_{loose_id}"], "basis": "seller_statement"},
    ])
    repo.attach_document(config.db_path(), package_id, first_id)
    repo.attach_document(config.db_path(), package_id, changed_id)
    repo.attach_document(config.db_path(), package_id, missing_id)

    response = client.get(f"/disputes/{case_id}/packages/{package_id}")
    assert response.status_code == 200, response.text
    assert 'data-state="integrated-evidence-dossier"' in response.text
    assert "This is an internal organizer" in response.text
    assert "documented defense scope is shipment proof for physical INR cases" in response.text
    assert "Case reason: Item or service not received." in response.text
    assert "Recorded transaction item type: Product." in response.text
    assert "The recorded Product type does not distinguish a physical product from a digital one" in response.text
    assert "cannot select type-specific evidence suggestions" in response.text
    assert "No type-specific suggestions selected" in response.text
    assert "Organizer suggestions are internal guidance, not PayPal requirements." in response.text
    assert "organizer-kind" not in response.text
    assert "No suggestions selected because the case reason is not recognized" not in response.text
    assert "Type: unknown" not in response.text
    assert "Carrier record says delivered." in response.text
    assert "Proofie stored document carrier-proof.pdf" in response.text
    assert first_sha in response.text and "Attached" in response.text
    assert "attached; original byte hash does not match the stored digest" in response.text
    assert "attached; original bytes unavailable" in response.text
    assert "Not attached" in response.text
    assert "Missing source reference: missing-record-77" in response.text
    assert "source authenticity is not independently verified" in response.text
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)

    # A SERVICE case uses the same saved source but does not pretend its category is known.
    dispute_raw["disputed_transactions"][0]["items"][0]["item_type"] = "SERVICE"
    service_response = client.get(f"/disputes/{case_id}/packages/{package_id}")
    assert service_response.status_code == 200
    assert "association not determined; review case documents" in service_response.text
    assert "Review case documents and originals" in service_response.text
    assert f"/disputes/{case_id}/workspace/prepare#documents" in service_response.text
    assert 'href="#attachments"' in service_response.text and 'href="#statements"' in service_response.text
    assert "Uploading a document, attaching it to a response, and sending the response are separate steps" in service_response.text
    # The same normal case/package route states a missing-field cause without obscuring its known reason.
    dispute_raw["disputed_transactions"][0]["items"][0].pop("item_type")
    missing_response = client.get(f"/disputes/{case_id}/packages/{package_id}")
    assert missing_response.status_code == 200, missing_response.text
    assert "Case reason: Item or service not received." in missing_response.text
    assert "Some transaction items have no recorded type" in missing_response.text
    assert "No type-specific suggestions selected" in missing_response.text
    assert "No suggestions selected because the case reason is not recognized" not in missing_response.text

    # An unrecognized reason is named independently; it is not disguised as a missing item type.
    dispute_raw["reason"] = "FUTURE_REASON"
    unknown_reason_response = client.get(f"/disputes/{case_id}/packages/{package_id}")
    assert unknown_reason_response.status_code == 200, unknown_reason_response.text
    assert "Case reason is not recognized in this record." in unknown_reason_response.text
    assert "No suggestions selected because the case reason is not recognized" in unknown_reason_response.text
