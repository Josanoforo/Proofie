import hashlib
import os
def test_inline_adversarial_review_on_real_package(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from copilot import config
    from copilot.app import create_app
    from copilot.core.checklist import evaluate, load_rules
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.context import TransactionContext
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.features.claims import routes as claim_routes
    from copilot.web import routes_package
    from copilot.features.claims.model import snapshot
    from copilot.features.claims.service import compose
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
    monkeypatch.setattr(routes_package, "_checks_for", lambda norm, dispute_id, client, db: (evaluate(TransactionContext(), extracted_docs_for(db, dispute_id), load_rules(country="MX")), TransactionContext()))
    checks=evaluate(TransactionContext(), extracted_docs_for(config.db_path(),did), load_rules(country="MX"))
    before_snapshot=snapshot(compose(config.db_path(),did,pkg,checks,{}))
    app=create_app(); app.dependency_overrides[get_client]=lambda: Fake()
    client=TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",12345))
    package=client.get(f"/disputes/{did}/packages/{pkg}")
    assert package.status_code == 200 and f"/disputes/{did}/packages/{pkg}/claims-review" in package.text
    assert 'data-state="adversarial-review"' in package.text
    assert all(x in package.text for x in ("DEMO123456789", "OTHER987654321", "support.pdf", "opposition.pdf", "conflicting source readings"))
    assert snapshot(compose(config.db_path(),did,pkg,checks,{}))==before_snapshot
    review=client.get(f"/disputes/{did}/packages/{pkg}/claims-review")
    assert review.status_code == 200 and 'data-state="adversarial-review"' in review.text
    assert all(x in review.text for x in ("DEMO123456789", "OTHER987654321", "support.pdf", "opposition.pdf", "conflicting source readings"))
    assert "does not record a review" in review.text
    corrected=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    repo.insert_claims(config.db_path(),corrected,[{"evidence_index":0,"claim_index":0,"text":"OTHER987654321","source_ids":[f"doc_{data[1]}"],"basis":"seller_statement"}])
    corrected_inline=client.get(f"/disputes/{did}/packages/{corrected}")
    assert corrected_inline.status_code==200 and 'Changes since the previous stored package' in corrected_inline.text
    corrected_review=client.get(f"/disputes/{did}/packages/{corrected}/claims-review")
    assert corrected_review.status_code == 200
    assert "Changes since the previous stored package" in corrected_review.text
    assert "DEMO123456789" in corrected_review.text and "OTHER987654321" in corrected_review.text
    assert "cited sources changed" in corrected_review.text


    changed_doc=repo.get_document(config.db_path(),data[1])
    (config.uploads_dir()/changed_doc['sha256']).write_bytes(b'altered original')
    changed_page=client.get(f"/disputes/{did}/packages/{pkg}")
    assert changed_page.status_code==200 and 'original changed' in changed_page.text
    from copilot.features.claims import adversarial
    monkeypatch.setattr(adversarial,'adversarial_review',lambda *args: (_ for _ in ()).throw(ValueError('synthetic failure')))
    failed=client.get(f"/disputes/{did}/packages/{corrected}")
    assert failed.status_code==200 and 'Source comparison is unavailable' in failed.text
    monkeypatch.setenv('COPILOT_FEATURE_CLAIMS','false')
    off=client.get(f"/disputes/{did}/packages/{corrected}")
    assert off.status_code==200 and 'data-state="adversarial-review"' not in off.text
