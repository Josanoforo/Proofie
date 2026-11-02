import hashlib
import os
from datetime import datetime, timezone


def test_real_checklist_conflict_versions_and_changed_original(monkeypatch, tmp_path):
    from copilot import config
    from copilot.core.checklist import evaluate, load_rules
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.context import TransactionContext
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.features.claims.adversarial import adversarial_review
    from copilot.features.claims.service import compose
    from copilot.features.claims.model import snapshot
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    did = "PP-R-DEMO-0000001"
    doc_ids = []
    for filename, text in (("first.pdf", "Tracking number: DEMO123456789"), ("second.pdf", "Tracking number: OTHER987654321")):
        data = text.encode(); sha = hashlib.sha256(data).hexdigest()
        doc_ids.append(repo.insert_document(config.db_path(), did, filename, "application/pdf", len(data), sha, text, True))
        write_raw(config.uploads_dir(), sha, data)
    first = repo.insert_package(config.db_path(), did, "human_edit", True, [])
    repo.insert_claims(config.db_path(), first, [{"evidence_index": 0, "claim_index": 0, "text": "DEMO123456789", "source_ids": [f"doc_{doc_ids[0]}"], "basis": "model_interpretation"}])
    checks = evaluate(TransactionContext(), extracted_docs_for(config.db_path(), did), load_rules(country="MX"))
    assert next(c for c in checks if c.id == "tracking_number").state == "conflict"
    before = snapshot(compose(config.db_path(), did, first, checks, {}))
    result = adversarial_review(config.db_path(), did, first, checks, {})
    row = result["rows"][0]
    assert row["claim"] == "DEMO123456789" and row["basis"] == "model_interpretation"
    assert row["assessment"] == "conflicting source readings"
    assert [(s["name"], s["value"]) for s in row["support"]] == [("first.pdf", "DEMO123456789")]
    assert [(s["name"], s["value"]) for s in row["opposing"]] == [("second.pdf", "OTHER987654321")]
    assert snapshot(compose(config.db_path(), did, first, checks, {})) == before
    second = repo.insert_package(config.db_path(), did, "human_edit", True, [])
    repo.insert_claims(config.db_path(), second, [{"evidence_index": 0, "claim_index": 0, "text": "OTHER987654321", "source_ids": [f"doc_{doc_ids[1]}"], "basis": "seller_statement"}])
    changed = adversarial_review(config.db_path(), did, second, checks, {})
    assert changed["previous_package_id"] == first and changed["changes"][0]["before"] == "DEMO123456789"
    assert changed["changes"][0]["after"] == "OTHER987654321" and changed["changes"][0]["sources_changed"]
    sha2 = repo.get_document(config.db_path(), doc_ids[1])["sha256"]
    (config.uploads_dir() / sha2).write_bytes(b"altered")
    stale = adversarial_review(config.db_path(), did, first, checks, {})["rows"][0]
    assert stale["assessment"] == "source needs attention"
    assert any(s["id"] == f"doc_{doc_ids[1]}" and s["state"] == "original changed" for s in stale["sources"])


def test_package_link_consumes_claim_review_with_fake(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from copilot import config
    from copilot.app import create_app
    from copilot.core.checklist import evaluate, load_rules
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.context import TransactionContext
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.features.claims import routes as claim_routes
    from copilot.web.routes_disputes import get_client
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_CLAIMS", "true")
    monkeypatch.setenv("COPILOT_FEATURE_EVIDENCE_DOSSIER", "true")
    did="PP-R-DEMO-0000001"
    data=[]
    for name,text in (("support.pdf","Tracking number: DEMO123456789"),("opposition.pdf","Tracking number: OTHER987654321")):
        b=text.encode(); sha=hashlib.sha256(b).hexdigest()
        doc=repo.insert_document(config.db_path(), did, name, "application/pdf", len(b), sha, text, True)
        write_raw(config.uploads_dir(), sha, b); data.append(doc)
    pkg=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    repo.insert_claims(config.db_path(),pkg,[{"evidence_index":0,"claim_index":0,"text":"DEMO123456789","source_ids":[f"doc_{data[0]}"],"basis":"seller_statement"}])
    raw={"dispute_id":did,"reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED","status":"WAITING_FOR_SELLER_RESPONSE","dispute_life_cycle_stage":"INQUIRY","dispute_amount":{"currency_code":"USD","value":"25.00"},"disputed_transactions":[],"links":[]}
    class Fake:
        def call(self, method, path, op, **kwargs):
            assert method == "GET"
            if op in ("show_dispute", "resolution_read"): return raw
            if op == "context_transaction_search": return {"transaction_details": []}
            if op == "context_tracking": return {"trackers": []}
            raise AssertionError(op)
    monkeypatch.setattr(claim_routes, "_checks_for", lambda norm, dispute_id, client, db: (evaluate(TransactionContext(), extracted_docs_for(db, dispute_id), load_rules(country="MX")), TransactionContext()))
    app=create_app(); app.dependency_overrides[get_client]=lambda: Fake()
    client=TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",12345))
    package=client.get(f"/disputes/{did}/packages/{pkg}")
    assert package.status_code == 200 and f"/disputes/{did}/packages/{pkg}/claims-review" in package.text
    review=client.get(f"/disputes/{did}/packages/{pkg}/claims-review")
    assert review.status_code == 200 and 'data-state="adversarial-review"' in review.text
    assert all(x in review.text for x in ("DEMO123456789", "OTHER987654321", "support.pdf", "opposition.pdf", "conflicting source readings"))
    assert "does not record a review" in review.text
    corrected=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    repo.insert_claims(config.db_path(),corrected,[{"evidence_index":0,"claim_index":0,"text":"OTHER987654321","source_ids":[f"doc_{data[1]}"],"basis":"seller_statement"}])
    corrected_review=client.get(f"/disputes/{did}/packages/{corrected}/claims-review")
    assert corrected_review.status_code == 200
    assert "Changes since the previous stored package" in corrected_review.text
    assert "DEMO123456789" in corrected_review.text and "OTHER987654321" in corrected_review.text
    assert "cited sources changed" in corrected_review.text


def test_absent_illegible_and_unsubstantiated_are_distinct(monkeypatch, tmp_path):
    from copilot import config
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.features.claims.adversarial import adversarial_review
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    did="PP-R-DEMO-0000001"; b=b"unreadable scan"; sha=hashlib.sha256(b).hexdigest()
    doc=repo.insert_document(config.db_path(),did,"scan.pdf","application/pdf",len(b),sha,"",False)
    write_raw(config.uploads_dir(),sha,b)
    pkg=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    repo.insert_claims(config.db_path(),pkg,[
        {"evidence_index":0,"claim_index":0,"text":"Missing source.","source_ids":["doc_999"],"basis":"verified"},
        {"evidence_index":0,"claim_index":1,"text":"Illegible source.","source_ids":[f"doc_{doc}"],"basis":"verified"},
        {"evidence_index":0,"claim_index":2,"text":"Model interpretation.","source_ids":[],"basis":"model_interpretation"}])
    rows=adversarial_review(config.db_path(),did,pkg,[],{})["rows"]
    assert rows[0]["sources"][0]["state"] == "missing"
    assert rows[1]["sources"][0]["state"] == "illegible"
    assert rows[2]["assessment"] == "corroboration needed" and rows[2]["verdict"] == "unverifiable"
    assert all(not row["opposing"] for row in rows)
