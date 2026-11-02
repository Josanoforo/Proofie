import hashlib
import html
import os
import re


def test_case_classification_to_same_package_and_attachment_guards(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.web import routes_package
    from copilot.web.routes_disputes import get_client
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(name, raising=False)
    for name in ("SALE_READINESS", "FULFILMENT", "EVIDENCE_DOSSIER"):
        monkeypatch.setenv("COPILOT_FEATURE_" + name, "true")
    dispute, capture = "PP-R-M10-0000003", "5S647034CC1979815"
    raw = {"dispute_id": dispute, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"currency_code": "USD", "value": "42.00"}, "create_time": "2026-10-01T12:00:00Z", "seller_response_due_date": "2026-11-30T12:00:00Z", "disputed_transactions": [{"seller_transaction_id": capture}], "links": []}
    class Fake:
        def call(self, method, path, op, **kwargs):
            assert method == "GET"
            if path.startswith("/v1/customer/disputes/"): return raw
            if path.startswith("/v1/reporting/transactions"): return {"transaction_details": []}
            if path.startswith("/v1/shipping/trackers"): return {"trackers": []}
            raise AssertionError(op)
    payload = b"%PDF-1.4 synthetic delivery"
    digest = hashlib.sha256(payload).hexdigest()
    good = repo.insert_document(config.db_path(), dispute, "delivery.pdf", "application/pdf", len(payload), digest, "Synthetic delivery", True)
    write_raw(config.uploads_dir(), digest, payload)
    foreign = repo.insert_document(config.db_path(), "PP-R-M10-0000004", "other.pdf", "application/pdf", len(payload), digest, "Foreign", True)
    package = repo.insert_package(config.db_path(), dispute, "human_edit", True, [])
    app = create_app(); app.dependency_overrides[get_client] = lambda: Fake()
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    prep = client.get(f"/disputes/{dispute}/workspace/prepare")
    assert prep.status_code == 200 and f"/fulfilment/{capture}?from_dispute={dispute}&amp;from_package={package}#sale-readiness" in prep.text      # O4: the link carries the package
    page = client.get(f"/fulfilment/{capture}?from_dispute={dispute}")
    assert page.status_code == 200 and "delivery.pdf" in page.text
    token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
    form = {"csrf_token": token, "dispute_id": dispute, "kind": "physical", "buyer_ref": "buyer", "description": "example", "price_amount": "42.00", "currency": "USD", f"evidence_kind_{good}": "delivery"}
    assert client.post(f"/fulfilment/{capture}/readiness", data={**form, "csrf_token": "wrong"}).status_code == 403
    wrong = client.post(f"/fulfilment/{capture}/readiness", data={**form, f"evidence_kind_{foreign}": "delivery"})
    assert wrong.status_code == 400
    saved = client.post(f"/fulfilment/{capture}/readiness", data=form, follow_redirects=False)
    assert saved.status_code == 303
    review = client.get(f"/disputes/{dispute}/packages/{package}")
    assert review.status_code == 200 and "seller-associated original" in review.text and "delivery.pdf" in review.text
    assert "not a list of PayPal requirements" in review.text
    csrf = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', review.text).group(1))
    url = f"/disputes/{dispute}/packages/{package}/attach/{good}"
    assert client.post(url, data={"csrf_token": "wrong"}).status_code == 403
    assert client.post(f"/disputes/{dispute}/packages/{package}/attach/{foreign}", data={"csrf_token": csrf}).status_code == 404
    attached = client.post(url, data={"csrf_token": csrf}, follow_redirects=False)
    assert attached.status_code == 303 and repo.list_attachments(config.db_path(), package)[0]["document_id"] == good
    updated = client.get(f"/disputes/{dispute}/packages/{package}")
    assert "attached; original bytes and package digest match" in updated.text
    (config.uploads_dir() / digest).write_bytes(b"tampered")
    changed = client.get(f"/disputes/{dispute}/packages/{package}")
    assert "original byte hash does not match" in changed.text
    assert "seller-associated original" not in changed.text
    newtoken = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', changed.text).group(1))
    monkeypatch.setattr(routes_package, "_is_sent_version", lambda *args: True)
    assert client.post(url, data={"csrf_token": newtoken}).status_code == 409


def test_unsupported_category_and_unknown_type(monkeypatch, tmp_path):
    from copilot.features.evidence_dossier import service
    monkeypatch.setenv("COPILOT_FEATURE_SALE_READINESS", "false")
    raw = {"disputed_transactions": [{"items": [{"item_type": "FUTURE_TYPE"}]}]}
    result = service.assemble_case(raw, {"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}, [], [], [], {}, tmp_path)
    assert result["kind"] == "unknown" and result["proof_matrix"]["items"] == []


def test_seller_selected_kinds_map_only_exact_supported_originals(monkeypatch, tmp_path):
    from copilot.features.evidence_dossier import service
    data = b"%PDF-synthetic"
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / sha).write_bytes(data)
    doc = {"id": 1, "filename": "original.pdf", "sha256": sha, "content_type": "application/pdf", "size_bytes": len(data), "extraction_ok": True, "extracted_text": "seller record", "excluded": False}
    for kind, category, expected in (("physical", "delivery", "delivery record"), ("digital", "access", "access or delivery event"), ("service", "performance", "appointment or performance record")):
        monkeypatch.setattr(service, "_readiness_associations", lambda norm, k=kind, c=category: ({"kind": k, "evidence_kinds": {"1": c, "999": c}}, "5S647034CC1979815"))
        result = service.assemble_case({}, {"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}, [], [doc], [], {}, tmp_path)
        assert result["kind"] == kind
        row = next(row for row in result["proof_matrix"]["items"] if row["proof_type"] == expected)
        assert row["source_ids"] == ["doc_1"] and row["sources"][0]["name"] == "original.pdf"
        assert "seller-selected" in result["proof_matrix"]["basis"]
    assert all("legal" not in row["status"] for row in result["proof_matrix"]["items"])


def test_unavailable_attachment_type_is_distinct_from_source(monkeypatch, tmp_path):
    from copilot.features.evidence_dossier import service
    monkeypatch.setenv("COPILOT_FEATURE_SALE_READINESS", "false")
    data = b"synthetic text"; sha = hashlib.sha256(data).hexdigest()
    (tmp_path / sha).write_bytes(data)
    doc = {"id": 1, "filename": "note.txt", "sha256": sha, "content_type": "text/plain", "size_bytes": len(data), "extraction_ok": True, "extracted_text": "text"}
    item = service.assemble_case({}, {}, [], [doc], [], {}, tmp_path)["annexes"][0]
    assert item["bytes_available"] and not item["sendable_type"] and not item["attached"]
