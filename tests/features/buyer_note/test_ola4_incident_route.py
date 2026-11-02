import hashlib, html, json, os, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from fastapi.testclient import TestClient


def test_incident_editor_proposal_preview_and_stale(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.features.fulfilment import store
    from copilot.features.fulfilment.tracker_request import build_tracker_body
    from copilot.features.sale_readiness import service as readiness
    from copilot.web.routes_disputes import get_client
    def forbidden(*a, **kw): raise AssertionError("external network forbidden")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(key, raising=False)
    for key in ("fulfilment", "sale_readiness", "buyer_note", "incident_proposals", "negotiation_drafts", "resolution", "resolution_writes", "gap_resolution"):
        monkeypatch.setenv("COPILOT_FEATURE_" + key.upper(), "true")
    did="PP-R-DEMO-0000001"; cap="5S647034CC1979815"
    raw=json.loads(Path("pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
    raw["seller_response_due_date"]=(datetime.now(timezone.utc)+timedelta(days=3)).isoformat()
    raw["disputed_transactions"]=[{"seller_transaction_id":cap}]
    class Fake:
        def call(self, method, path, operation, **kw):
            assert method=="GET"
            if operation in ("show_dispute", "resolution_read"): return raw
            if operation=="context_transaction_search": return {"transaction_details": []}
            if operation=="context_tracking": return {"trackers": []}
            raise AssertionError(operation)
    app=create_app(); app.dependency_overrides[get_client]=lambda: Fake()
    pid=repo.insert_package(config.db_path(),did,"human_edit",True,[],effective_next_step="provide_evidence")
    terms={"kind":"physical","buyer_ref":"SYNTH","description":"Synthetic lamp","variant":"Blue","price_cents":42000,"currency":"MXN","expected_date":"2026-11-04","returns":"30 days","contact":"support","terms_version":"v1","acceptance_ref":"syn","carrier":"OTHER","tracking_number":"SYNTH-123","ship_to":{"name":"Synthetic Buyer","street":"1 Sample Road","city":"Sample City","region":"CDMX","postal_code":"01000","country":"MX"},"evidence_kinds":{}}
    order_id=repo.insert_document(config.db_path(),did,"order.txt","text/plain",5,hashlib.sha256(b"order").hexdigest(),"order",True)
    tracking_id=repo.insert_document(config.db_path(),did,"tracking.txt","text/plain",8,hashlib.sha256(b"tracking").hexdigest(),"tracking",True)
    terms["evidence_kinds"]={str(order_id):"order",str(tracking_id):"tracking"}
    readiness.save_case_terms(did,cap,terms)
    tracker=build_tracker_body(cap,"SYNTH-123","OTHER","Synthetic carrier"); h=hashlib.sha256(json.dumps(tracker,sort_keys=True).encode()).hexdigest()
    store.save_file(cap,tracker,h,h,None,{},"saved_without_registration","Synthetic record")
    with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",12345)) as client:
        # Actual full existing form, rather than a seeded manual_fields shortcut.
        package_page=client.get(f"/disputes/{did}/packages/{pid}")
        assert package_page.status_code==200
        package_csrf=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',package_page.text).group(1))
        saved=client.post(f"/disputes/{did}/manual-fields",data={"csrf_token":package_csrf,"package_id":str(pid),"tracking_number":"HUMAN-FORM-1","carrier":"FEDEX","city":"Puebla","postal_code":"72000"},follow_redirects=False)
        assert saved.status_code==303
        fields=repo.get_manual_fields(config.db_path(),did)
        assert fields["tracking_number"]["value"]=="HUMAN-FORM-1" and fields["city"]["value"]=="Puebla"
        from copilot.core.checklist_input import extracted_docs_for
        manual=next(doc for doc in extracted_docs_for(config.db_path(),did) if doc.doc_id=="manual")
        assert manual.fields["tracking_number"]["value"]=="HUMAN-FORM-1"
        view=readiness.case_view(did,cap,{},repo.list_documents(config.db_path(),did),[],readiness.load_case_terms(did,cap),{"tracking_number":"SYNTH-123","carrier":"OTHER"})["result"]
        assert view["tracking_payload"]["tracking_number"]=="HUMAN-FORM-1" and view["source_warnings"]
        assert readiness.load_case_terms(did,cap)["terms"]["tracking_number"]=="SYNTH-123"
        url=f"/fulfilment/{cap}/delivery-incident"
        page=client.get(url,params={"from_dispute":did,"from_package":pid,"incident":"delay","locale":"es"}); assert page.status_code==200 and "incident-proposal-editor" in page.text
        assert '<option value="">Choose language</option>' in page.text
        token=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1))
        body={"csrf_token":token,"from_dispute":did,"from_package":str(pid),"incident":"delay","locale":"es","seller_text":"Texto humano revisado"}
        assert client.post(url+"/proposal",data={**body,"csrf_token":"wrong"}).status_code==403
        draft=client.post(url+"/proposal",data=body); assert draft.status_code==200, draft.text
        assert "HUMAN-FORM-1" in draft.text and "SYNTH-123" in draft.text
        logical=re.search(r'name="logical_id" value="([^"]+)"',draft.text).group(1); csrf=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',draft.text).group(1))
        preview=client.post(url+"/preview",data={**body,"csrf_token":csrf,"logical_id":logical},follow_redirects=False); assert preview.status_code==303,preview.text
        terms["tracking_number"]="SYNTH-NEW"; readiness.save_case_terms(did,cap,terms)
        stale=client.post(url+"/preview",data={**body,"csrf_token":csrf,"logical_id":logical},follow_redirects=False)
        assert stale.status_code==409 and "Texto humano revisado" in stale.text

        csrf=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',stale.text).group(1))
        foreign=repo.insert_package(config.db_path(), "PP-R-DEMO-0000002", "human_edit", True, [], effective_next_step="provide_evidence")
        denied=client.post(url+"/proposal",data={**body,"csrf_token":csrf,"from_package":str(foreign)})
        assert denied.status_code==409 and "Texto humano revisado" in denied.text
        csrf=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',denied.text).group(1))
        repo.set_manual_fields(config.db_path(),did,{"tracking_number":{"value":"HUMAN-CORRECTED","confidence":"read"}})
        contextual=client.post(url+"/proposal",data={**body,"csrf_token":csrf,"seller_text":"Estoy revisando los datos registrados de esta venta."})
        assert contextual.status_code==200 and "HUMAN-CORRECTED" in contextual.text and "SYNTH-NEW" in contextual.text
        csrf=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',contextual.text).group(1))
        contextual_id=re.search(r'name="logical_id" value="([^"]+)"',contextual.text).group(1)
        contextual_text=html.unescape(re.search(r'<textarea[^>]*name="seller_text"[^>]*>(.*?)</textarea>',contextual.text,re.S).group(1))
        changed=build_tracker_body(cap,"SHIPMENT-CHANGED","OTHER","Synthetic carrier")
        h2=hashlib.sha256(json.dumps(changed,sort_keys=True).encode()).hexdigest()
        store.save_file(cap,changed,h2,h2,None,{},"saved_without_registration","Changed synthetic record")
        stale_shipment=client.post(url+"/preview",data={**body,"csrf_token":csrf,"seller_text":contextual_text,"logical_id":contextual_id},follow_redirects=False)
        assert stale_shipment.status_code==409 and "HUMAN-CORRECTED" in stale_shipment.text
        csrf=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',stale_shipment.text).group(1))
        raw["links"]=[]
        unavailable=client.post(url+"/proposal",data={**body,"csrf_token":csrf})
        assert unavailable.status_code==409 and "Texto humano revisado" in unavailable.text
        question=client.get(url,params={"from_dispute":did,"from_package":pid,"incident":"buyer_question","locale":"en"})
        assert question.status_code==200 and "Buyer question" in question.text

        monkeypatch.setenv("COPILOT_FEATURE_INCIDENT_PROPOSALS", "false")
        off=client.get(url,params={"from_dispute":did,"from_package":pid,"incident":"delay","locale":"es"})
        assert off.status_code==200 and "incident-proposal-editor" not in off.text
        off_token=html.unescape(re.search(r'name="csrf_token" value="([^"]+)"',off.text).group(1))
        assert client.post(url+"/proposal",data={**body,"csrf_token":off_token}).status_code==404
