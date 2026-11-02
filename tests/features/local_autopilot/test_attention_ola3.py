import copy
import hashlib
from datetime import datetime, timezone

from copilot.features.local_autopilot import assistance


def model_record(source="doc_1", claim="Recorded source"):
    return {"validation_ok": True, "_output": {"missing_evidence": [], "conflicts": [],
        "proposed_evidence": [{"evidence_type": "OTHER", "tracking_info": [], "notes": "",
            "claims": [{"text": claim, "source_ids": [source], "basis": "seller_statement"}]}],
        "recommended_next_step": "need_more_information", "reviewer_note_draft": ""}}


def test_recompute_invalidation_and_optional_model(monkeypatch, tmp_path):
    from copilot import config
    from copilot.db import repo
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "case.sqlite")
    db = config.db_path(); repo.ensure_schema(db)
    case = {"dispute_id":"PP-R-DEMO-0000001", "status":"WAITING_FOR_SELLER_RESPONSE",
        "reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "update_time":"2026-10-31T00:00:00Z",
        "deadline_parse_ok":True, "seller_response_due_date":"2099-01-01T00:00:00Z", "amount_cents":1500,"currency":"USD"}
    sha=hashlib.sha256(b"source").hexdigest()
    repo.insert_document(db, case["dispute_id"], "source.pdf", "application/pdf", 6, sha, "Recorded source", True)
    history=tmp_path / "human-note.txt"; history.write_text("human decision: wait", encoding="utf-8")
    now=datetime(2026,10,31,tzinfo=timezone.utc)
    def build(raw=case, actions=("message","offer"), **kwargs):
        return assistance.case_context(db, raw, actions, now=now, **kwargs)
    first=build(); again=build()
    assert first["attention_plan"] == again["attention_plan"]
    assert first["attention_plan"]["sources"][0]["sha256"] == sha
    initial=first["attention_plan"]["fingerprint"]
    for change in ({"amount_cents":1499}, {"currency":"MXN"}, {"status":"RESOLVED"},
                   {"seller_response_due_date":"2099-02-01T00:00:00Z"}, {"update_time":"new-version"}):
        assert build({**case, **change})["attention_plan"]["fingerprint"] != initial
    assert build(actions=("offer",))["attention_plan"]["fingerprint"] != initial
    repo.insert_document(db, case["dispute_id"], "other.pdf", "application/pdf", 5, hashlib.sha256(b"other").hexdigest(), "Other record", True)
    assert build()["attention_plan"]["fingerprint"] != initial
    assert history.read_text(encoding="utf-8") == "human decision: wait"
    calls=[]
    def stub(dispute, docs, names):
        calls.append(sorted(docs)); return model_record()
    hint=build(model_backend=stub)["attention_plan"]["model_hint"]
    assert calls and hint["source_ids"] == ["doc_1"] and "Model suggestion" in hint["label"]
    no_sources = model_record()
    no_sources["_output"]["proposed_evidence"][0]["claims"][0]["source_ids"] = []
    for record in (no_sources, model_record("unknown"), model_record("doc_1","Resolved"), model_record("doc_1","Paid 1500 USD"),
                   {"validation_ok":False,"_output":{}}, {"validation_ok":True,"_output":{}}):
        out=build(model_backend=lambda *args, record=record: copy.deepcopy(record))
        assert out["attention_plan"]["model_hint"] is None and out["recommendation"] == "message"
    fallback=build(model_backend=lambda *args: (_ for _ in ()).throw(RuntimeError("offline")))
    assert fallback["attention_plan"]["model_hint"] is None and fallback["attention_plan"]["tasks"]
    assert history.read_text(encoding="utf-8") == "human decision: wait"


def test_prepare_fallback_flows_to_draft_preview_without_network(monkeypatch, tmp_path):
    import html, json, os, re
    from pathlib import Path
    from datetime import timedelta
    import httpx
    from _hosted import authenticated_client
    from copilot import config
    from copilot.app import create_app
    from copilot.core import auto_prep
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client
    from copilot.features.local_autopilot import assistance as module
    def forbidden(*args, **kwargs): raise AssertionError("real transport/model forbidden")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(auto_prep, "should_prepare", lambda *args: False)
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "case.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(key, raising=False)
    for key in ("LOCAL_AUTOPILOT", "RESOLUTION", "RESOLUTION_WRITES", "NEGOTIATION_DRAFTS"):
        monkeypatch.setenv("COPILOT_FEATURE_"+key,"true")
    did="PP-R-DEMO-0000001"
    raw=json.loads(Path("pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
    raw["seller_response_due_date"]=(datetime.now(timezone.utc)+timedelta(days=3)).isoformat()
    class Fake:
        def call(self, method, path, operation, **kwargs):
            assert method == "GET"
            if operation in {"show_dispute","resolution_read"}: return raw
            if operation == "context_transaction_search": return {"transaction_details": []}
            if operation == "context_tracking": return {"trackers": []}
            if operation == "list_disputes": return {"items": [], "links": []}
            raise AssertionError(operation)
    repo.insert_document(config.db_path(),did,"case.pdf","application/pdf",6,hashlib.sha256(b"source").hexdigest(),"Recorded source",True)
    original=module.case_context
    monkeypatch.setattr(module,"case_context",lambda *a,**kw: original(*a,**kw,model_backend=lambda *unused: model_record("doc_1","Paid 9000 USD")))
    app=create_app(); app.dependency_overrides[get_client]=lambda:Fake()
    client=authenticated_client(app,client=("127.0.0.1",12345))
    prepare=client.get(f"/disputes/{did}/workspace/prepare")
    assert prepare.status_code==200 and 'data-state="attention-plan"' in prepare.text
    assert "Paid 9000 USD" not in prepare.text and "Model suggestion" not in prepare.text
    page=client.get(f"/disputes/{did}/resolution")
    token=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
    draft=client.post(f"/disputes/{did}/resolution/draft",data={"csrf_token":token,"kind":"partial_offer","locale":"en","recipient":"Buyer","amount":"15.00","conditions":"Review terms"})
    assert draft.status_code==200 and "USD 15.00" in draft.text
    match=re.search(r'<form method="post" action="/disputes/'+did+r'/resolution/preview#preview">(.*?)</form>',draft.text,re.S)
    assert match
    form={n:html.unescape(v) for n,v in re.findall(r'name="([^"]+)" value="([^"]*)"',match.group(1))}
    preview=client.post(f"/disputes/{did}/resolution/preview",data=form,follow_redirects=False)
    assert preview.status_code==303
    checked=client.get(preview.headers["location"])
    assert checked.status_code==200 and "15.00 USD" in checked.text


def test_saved_human_draft_survives_recomputation(monkeypatch, tmp_path):
    from copilot import config
    from copilot.db import repo
    from copilot.features.negotiation_drafts import service as drafts
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "case.sqlite")
    db=config.db_path(); repo.ensure_schema(db)
    norm={"dispute_id":"PP-R-DEMO-0000001", "reason":"UNAUTHORISED", "status":"WAITING_FOR_SELLER_RESPONSE", "stage":"INQUIRY", "amount_cents":1500, "currency":"USD", "update_time":"v1", "deadline_parse_ok":True, "seller_response_due_date":"2099-01-01T00:00:00Z"}
    path=drafts.case_history_path(norm["dispute_id"])
    kwargs={"kind":"partial_offer","locale":"en","recipient":"Buyer", "amount_cents":1200, "conditions":"Human chooses these terms", "history_path":path}
    one=drafts.build_case_draft(norm,{"offer"},**kwargs)
    two=drafts.build_case_draft(norm,{"offer"},**kwargs)
    before=path.read_bytes()
    assert len(one["history_versions"]) == len(two["history_versions"]) == 1
    old=assistance.case_context(db,norm,["offer"])["attention_plan"]["fingerprint"]
    new=assistance.case_context(db,{**norm,"amount_cents":1400},["offer"])["attention_plan"]["fingerprint"]
    assert old != new and path.read_bytes() == before
    assert "Human chooses these terms" in drafts.read_case_history(norm["dispute_id"])[0]["conditions"]
