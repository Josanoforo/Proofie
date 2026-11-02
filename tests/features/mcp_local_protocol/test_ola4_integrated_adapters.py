import html, json, os, re, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def env(monkeypatch,tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.core import auto_prep
    from copilot.web.routes_disputes import get_client
    def forbidden(*a,**kw): raise AssertionError("external operation prohibited")
    monkeypatch.setattr(httpx.HTTPTransport,"handle_request",forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,"handle_async_request",forbidden)
    monkeypatch.setattr(config,"load_env_file",lambda:None)
    monkeypatch.setattr(config,"_db_path_override",tmp_path/"test.sqlite")
    monkeypatch.setattr(config,"_uploads_dir_override",tmp_path/"uploads")
    monkeypatch.setattr(config,"data_dir",lambda:tmp_path/"data")
    for k in list(os.environ):
        if k.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(k,raising=False)
    for k in ("local_autopilot","local_preparation","explainable_queue","selected_preparation","gap_resolution","case_reentry"):
        monkeypatch.setenv("COPILOT_FEATURE_"+k.upper(),"true")
    monkeypatch.setattr(auto_prep,"fetch_context",forbidden)
    monkeypatch.setattr(auto_prep,"propose_package",forbidden)
    monkeypatch.setattr(auto_prep,"claims_for_dispute_images",forbidden)
    did="PP-R-DEMO-0000001"
    raw=json.loads(Path("pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
    raw["seller_response_due_date"]=(datetime.now(timezone.utc)+timedelta(days=3)).isoformat()
    class Fake:
        def __init__(self): self.calls=[]
        def call(self,method,path,op,**kwargs):
            assert method=="GET"; self.calls.append(op)
            if op in ("show_dispute","resolution_read"): return raw
            if op=="list_disputes": return {"items":[raw],"links":[]}
            if op=="context_transaction_search": return {"transaction_details":[]}
            if op=="context_tracking": return {"trackers":[]}
            raise AssertionError(op)
    fake=Fake(); app=create_app(); app.dependency_overrides[get_client]=lambda:fake
    with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",12345)) as client:
        yield client,repo,config,auto_prep,raw,did,fake


def token(page,name):
    match=re.search(r'name="'+name+r'" value="([^"]+)"',page.text); assert match,(name,page.status_code); return html.unescape(match.group(1))


def test_prepare_to_local_core_and_reentry(env):
    client,repo,config,core,raw,did,fake=env
    repo.set_manual_fields(config.db_path(),did,{"tracking_number":{"value":"SYNTH-TRACK","confidence":"read"}})
    page=client.get(f"/disputes/{did}/workspace/prepare"); assert page.status_code==200
    assert "Pick up this case" in page.text
    csrf=token(page,"csrf_token"); obs=token(page,"observation_token")
    bad=client.post(f"/disputes/{did}/local-preparation",data={"csrf_token":"bad","observation_token":obs},follow_redirects=False); assert bad.status_code==403
    start=client.post(f"/disputes/{did}/local-preparation",data={"csrf_token":csrf,"observation_token":obs},follow_redirects=False)
    assert start.status_code==303 and "local_preparation=ready" in start.headers["location"],start.headers.get("location")
    packages=repo.list_packages(config.db_path(),did);assert len(packages)==1 and packages[0]["source"]=="auto_manual"
    repeat=client.post(f"/disputes/{did}/local-preparation",data={"csrf_token":csrf,"observation_token":obs},follow_redirects=False);assert "local_preparation=ready" in repeat.headers["location"] and len(repo.list_packages(config.db_path(),did))==1
    page2=client.get(f"/disputes/{did}/workspace/prepare"); assert page2.status_code==200 and str(packages[0]["id"]) in page2.text


def test_sqlite_issuer_failure_preserves_prepare(env,monkeypatch):
    client,repo,config,core,raw,did,fake=env
    from copilot.features.local_autopilot import observations
    def error(*a,**kw): raise sqlite3.OperationalError("fixture failure")
    monkeypatch.setattr(observations,"issue",error)
    page=client.get(f"/disputes/{did}/workspace/prepare"); assert page.status_code==200
    assert 'name="observation_token"' not in page.text


def test_selected_inbox_uses_server_observation_and_claims(env):
    import copy, time
    from copilot.core.disputes import normalize
    client,repo,config,core,raw,did,fake=env
    cases=[]
    for index in range(3):
        row=copy.deepcopy(raw); row["dispute_id"]=f"PP-R-DEMO-000000{index+1}"; cases.append(row)
    original=fake.call
    def call(method,path,op,**kwargs):
        if op=="list_disputes": fake.calls.append(op);return {"items":cases,"links":[]}
        return original(method,path,op,**kwargs)
    fake.call=call
    # Preserve pre-existing inbox behavior: claims prevent background dispatch in this fixture.
    for index,row in enumerate(cases):
        cid=row["dispute_id"]; norm=normalize(row,datetime.now(timezone.utc))
        if index==0: repo.set_manual_fields(config.db_path(),cid,{"tracking_number":{"value":"TRACK-1","confidence":"read"}})
        version=core.compute_version_hash(config.db_path(),norm); claim=repo.claim_auto_prep(config.db_path(),cid,version); assert claim
        if index==0: core.prepare_dispute_local(config.db_path(),norm,version,claim)
    start=time.perf_counter();page=client.get("/disputes"); assert page.status_code==200
    csrf=token(page,"selected_preparation_csrf") if 'name="selected_preparation_csrf"' in page.text else token(page,"csrf_token")
    obs=token(page,"observation_token")
    assert client.post("/features/explainable_queue/prepare-selected",data={"csrf_token":"invalid","observation_token":obs,"case_ids":[x["dispute_id"] for x in cases]}).status_code==403
    result=client.post("/features/explainable_queue/prepare-selected",data={"csrf_token":csrf,"observation_token":obs,"case_ids":[x["dispute_id"] for x in cases]})
    assert result.status_code==200 and "ready" in result.text and "needs input" in result.text, result.text
    assert len(repo.list_packages(config.db_path(),cases[0]["dispute_id"]))==1
    print("synthetic three-case local fixture seconds",round(time.perf_counter()-start,3))


def test_human_package_preserved_and_gap_sql_error_falls_back(env,monkeypatch):
    client,repo,config,core,raw,did,fake=env
    from copilot.features.local_autopilot import gap_resolution
    cap="DEMOTXN0000000001"
    raw["disputed_transactions"]=[{"seller_transaction_id":cap}]
    pid=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    def error(*a,**kw): raise sqlite3.OperationalError("synthetic failure")
    monkeypatch.setattr(gap_resolution,"current_case",error)
    page=client.get(f"/disputes/{did}/workspace/prepare");assert page.status_code==200
    csrf=token(page,"csrf_token");obs=token(page,"observation_token")
    response=client.post(f"/disputes/{did}/local-preparation",data={"csrf_token":csrf,"observation_token":obs},follow_redirects=False)
    assert "local_preparation=human_review" in response.headers["location"]
    assert [x["id"] for x in repo.list_packages(config.db_path(),did)]==[pid]


def test_integrated_captures_opt_in(env,monkeypatch):
    if os.environ.get("OLA4_CAPTURE")!="1": pytest.skip("explicit capture only")
    from copilot.core.disputes import normalize
    from playwright.sync_api import sync_playwright
    client,repo,config,core,raw,did,fake=env
    repo.set_manual_fields(config.db_path(),did,{"tracking_number":{"value":"SYNTH-CAPTURE","confidence":"read"}})
    norm=normalize(raw,datetime.now(timezone.utc)); version=core.compute_version_hash(config.db_path(),norm);assert repo.claim_auto_prep(config.db_path(),did,version)
    css=client.get("/static/app.css");assert css.status_code==200
    out=Path("internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola4/terra-d")
    with sync_playwright() as play:
        browser=play.chromium.launch(headless=True); page=browser.new_page(viewport={"width":1440,"height":900},device_scale_factor=1)
        page.route("**/*",lambda route:route.fulfill(status=200,body=css.text,content_type="text/css") if route.request.url.endswith("/static/app.css") else route.abort())
        for label,active in (("before",False),("after",True)):
            for feature in ("LOCAL_PREPARATION","SELECTED_PREPARATION","GAP_RESOLUTION","CASE_REENTRY"):
                monkeypatch.setenv("COPILOT_FEATURE_"+feature,"true" if active else "false")
            for name,url in (("prepare",f"/disputes/{did}/workspace/prepare"),("inbox","/disputes")):
                response=client.get(url);assert response.status_code==200
                if active and name=="prepare": assert "Pick up this case" in response.text and 'name="observation_token"' in response.text
                if active and name=="inbox": assert "selected-preparation" in response.text
                markup=response.text.replace("/static/app.css","http://fixture.local/static/app.css")
                page.set_content(markup,wait_until="load");page.screenshot(path=str(out/f"integrated-{name}-{label}.png"),full_page=True)
        browser.close()


def test_prepare_gap_links_to_existing_full_form(env):
    client,repo,config,core,raw,did,fake=env
    cap="DEMOTXN0000000001";raw["disputed_transactions"]=[{"seller_transaction_id":cap}]
    pid=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    page=client.get(f"/disputes/{did}/workspace/prepare");assert page.status_code==200
    assert f"/disputes/{did}/packages/{pid}#field-tracking_number" in page.text


def test_batch_scope_and_foreign_token(env):
    import copy
    client,repo,config,core,raw,did,fake=env
    rows=[]
    for index in range(51):
        row=copy.deepcopy(raw);row["dispute_id"]=f"PP-R-SCOPE-{index:05d}";row["reason"]="SIGNIFICANTLY_NOT_AS_DESCRIBED";rows.append(row)
    original=fake.call
    def call(method,path,op,**kwargs):
        if op=="list_disputes": return {"items":rows,"links":[]}
        return original(method,path,op,**kwargs)
    fake.call=call
    page=client.get("/disputes");assert page.status_code==200
    assert "first 50 of 51" in page.text
    obs=token(page,"observation_token");csrf=token(page,"csrf_token")
    foreign=client.post("/features/explainable_queue/prepare-selected",data={"csrf_token":csrf,"observation_token":obs,"case_ids":["PP-R-FOREIGN-1"]})
    assert foreign.status_code==400
    too_many=client.post("/features/explainable_queue/prepare-selected",data={"csrf_token":client.cookies.get("csrf_token"),"observation_token":obs,"case_ids":[row["dispute_id"] for row in rows[:4]]})
    assert too_many.status_code==400


def test_prepare_missing_original_and_expired_observation(env,monkeypatch):
    import hashlib
    from copilot.features.local_autopilot import observations
    client,repo,config,core,raw,did,fake=env
    clock=[100.0]; store=observations.ObservationStore(clock=lambda:clock[0])
    monkeypatch.setattr(observations,"issue",store.issue);monkeypatch.setattr(observations,"get",store.get)
    repo.insert_document(config.db_path(),did,"missing.txt","text/plain",5,hashlib.sha256(b"absent").hexdigest(),"synthetic",True)
    page=client.get(f"/disputes/{did}/workspace/prepare");assert page.status_code==200
    csrf=token(page,"csrf_token");obs=token(page,"observation_token")
    missing=client.post(f"/disputes/{did}/local-preparation",data={"csrf_token":csrf,"observation_token":obs},follow_redirects=False)
    assert "local_preparation=needs_input" in missing.headers["location"] and not repo.list_packages(config.db_path(),did)
    clock[0]+=301
    expired=client.post(f"/disputes/{did}/local-preparation",data={"csrf_token":csrf,"observation_token":obs},follow_redirects=False)
    assert "local_preparation=unavailable" in expired.headers["location"]


def test_reentry_versions_do_not_inherit_approval(env):
    client,repo,config,core,raw,did,fake=env
    first=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    repo.insert_approval(config.db_path(),first,"synthetic-hash","vendor")
    second=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    page=client.get(f"/disputes/{did}/workspace/prepare");assert page.status_code==200
    marker=page.text.split('data-state="case-reentry-versions"',1)[1].split("</details>",1)[0]
    assert f"/packages/{first}" in marker and f"/packages/{second}" in marker
    v2=marker.split("Package v2",1)[1];assert "No current approval is recorded for this package version" in v2
    assert "An older approval does not approve another package version" in page.text


def test_prepare_flags_add_no_fake_provider_reads(env,monkeypatch):
    client,repo,config,core,raw,did,fake=env
    for flag in ("LOCAL_PREPARATION","GAP_RESOLUTION","CASE_REENTRY"):monkeypatch.setenv("COPILOT_FEATURE_"+flag,"false")
    fake.calls.clear();before=client.get(f"/disputes/{did}/workspace/prepare");base=list(fake.calls)
    for flag in ("LOCAL_PREPARATION","GAP_RESOLUTION","CASE_REENTRY"):monkeypatch.setenv("COPILOT_FEATURE_"+flag,"true")
    fake.calls.clear();after=client.get(f"/disputes/{did}/workspace/prepare");active=list(fake.calls)
    assert before.status_code==after.status_code==200 and base==active
    assert "Pick up this case" not in before.text and "Pick up this case" in after.text


def test_human_edit_during_actual_local_core_preserved(env,monkeypatch):
    client,repo,config,core,raw,did,fake=env
    repo.set_manual_fields(config.db_path(),did,{"tracking_number":{"value":"SYNTHETIC","confidence":"read"}})
    import hashlib
    from copilot.core.raw_storage import write_raw
    payload=b"synthetic original"; digest=hashlib.sha256(payload).hexdigest()
    doc=repo.insert_document(config.db_path(),did,"original.txt","text/plain",len(payload),digest,"synthetic original",True)
    write_raw(config.uploads_dir(),digest,payload)
    original=core.prepare_dispute_local; created={}
    def concurrent_edit(db,norm,version,claim):
        original(db,norm,version,claim)
        human=repo.insert_package(db,did,"human_edit",True,[])
        repo.insert_claims(db,human,[{"evidence_index":0,"claim_index":0,"text":"Human text stays","source_ids":[],"basis":"seller_statement"}])
        repo.attach_document(db,human,doc)
        approval=repo.insert_approval(db,human,"synthetic-approval","vendor")
        created.update(human=human,approval=approval)
    monkeypatch.setattr(core,"prepare_dispute_local",concurrent_edit)
    page=client.get(f"/disputes/{did}/workspace/prepare");obs=token(page,"observation_token");csrf=token(page,"csrf_token")
    result=client.post(f"/disputes/{did}/local-preparation",data={"csrf_token":csrf,"observation_token":obs},follow_redirects=False)
    assert "local_preparation=human_review" in result.headers["location"]
    assert repo.list_claims(config.db_path(),created["human"])[0]["text"]=="Human text stays"
    assert repo.list_approvals(config.db_path(),created["human"])[0]["invalidated_at"] is None
    assert repo.list_attachments(config.db_path(),created["human"])[0]["document_id"]==doc
    assert len(repo.list_packages(config.db_path(),did))==2  # separate auto shell may exist


def test_selected_mixed_failure_and_retry_cooldown(env,monkeypatch):
    import copy,time
    from copilot.core.disputes import normalize
    client,repo,config,core,raw,did,fake=env
    rows=[]
    for index in range(3):
        row=copy.deepcopy(raw);row["dispute_id"]=f"PP-R-MIXED-{index:05d}";rows.append(row)
    original_call=fake.call
    def call(method,path,op,**kwargs):
        if op=="list_disputes":return {"items":rows,"links":[]}
        return original_call(method,path,op,**kwargs)
    fake.call=call
    for index,row in enumerate(rows):
        cid=row["dispute_id"];norm=normalize(row,datetime.now(timezone.utc))
        if index!=1:repo.set_manual_fields(config.db_path(),cid,{"tracking_number":{"value":"SYNTH-"+str(index),"confidence":"read"}})
        version=core.compute_version_hash(config.db_path(),norm);assert repo.claim_auto_prep(config.db_path(),cid,version)
    page=client.get("/disputes");assert page.status_code==200
    csrf=token(page,"csrf_token");obs=token(page,"observation_token")
    with sqlite3.connect(config.db_path()) as db:
        db.execute("UPDATE auto_prep SET updated_at=?",("2000-01-01T00:00:00+00:00",))
    actual=core.prepare_dispute_local
    def failing_one(db,norm,version,claim):
        if norm["dispute_id"]==rows[2]["dispute_id"]:raise RuntimeError("synthetic local failure")
        return actual(db,norm,version,claim)
    monkeypatch.setattr(core,"prepare_dispute_local",failing_one)
    start=time.perf_counter()
    batch=client.post("/features/explainable_queue/prepare-selected",data={"csrf_token":csrf,"observation_token":obs,"case_ids":[x["dispute_id"] for x in rows]})
    elapsed=time.perf_counter()-start
    assert batch.status_code==200 and "ready" in batch.text and "needs input" in batch.text and "unavailable" in batch.text
    assert len(repo.list_packages(config.db_path(),rows[0]["dispute_id"]))==1 and not repo.list_packages(config.db_path(),rows[1]["dispute_id"])
    csrf2=token(batch,"csrf_token")
    retry=client.post("/features/explainable_queue/prepare-selected",data={"csrf_token":csrf2,"observation_token":obs,"case_ids":[rows[2]["dispute_id"]]})
    assert retry.status_code==200 and "cooldown" in retry.text and not repo.list_packages(config.db_path(),rows[2]["dispute_id"])
    print("synthetic selected-three POST seconds",round(elapsed,3))


def test_prepare_gap_link_capture_opt_in(env,monkeypatch):
    if os.environ.get("OLA4_CAPTURE")!="1":pytest.skip("explicit capture only")
    from playwright.sync_api import sync_playwright
    client,repo,config,core,raw,did,fake=env
    raw["disputed_transactions"]=[{"seller_transaction_id":"DEMOTXN0000000001"}]
    pid=repo.insert_package(config.db_path(),did,"human_edit",True,[])
    css=client.get("/static/app.css");assert css.status_code==200
    out=Path("internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola4/terra-d")
    with sync_playwright() as play:
        browser=play.chromium.launch(headless=True);page=browser.new_page(viewport={"width":1440,"height":900},device_scale_factor=1)
        page.route("**/*",lambda route:route.fulfill(status=200,body=css.text,content_type="text/css") if route.request.url.endswith("/static/app.css") else route.abort())
        for label,enabled in (("before",False),("after",True)):
            monkeypatch.setenv("COPILOT_FEATURE_GAP_RESOLUTION","true" if enabled else "false")
            response=client.get(f"/disputes/{did}/workspace/prepare");assert response.status_code==200
            href=f"/disputes/{did}/packages/{pid}#field-tracking_number"
            assert (href in response.text)==enabled
            page.set_content(response.text.replace("/static/app.css","http://fixture.local/static/app.css"),wait_until="load")
            page.screenshot(path=str(out/f"prepare-gap-{label}.png"),full_page=True)
        browser.close()


def test_prepare_navigation_follows_local_preparation_flag(env,monkeypatch):
    import hashlib
    from copilot.core.raw_storage import write_raw
    client,repo,config,core,raw,did,fake=env
    payload=b"readable synthetic source";sha=hashlib.sha256(payload).hexdigest()
    repo.insert_document(config.db_path(),did,"source.txt","text/plain",len(payload),sha,"Readable recorded source",True)
    write_raw(config.uploads_dir(),sha,payload)
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_PREPARATION","true")
    local=client.get(f"/disputes/{did}/workspace/prepare");assert local.status_code==200
    assert 'id="local-preparation"' in local.text and 'href="#local-preparation"' in local.text
    assert "Prepare this case locally, then review the resulting draft." in local.text
    assert "not started; open the inbox to prepare" not in local.text and "Open inbox" not in local.text
    assert repo.get_auto_prep(config.db_path(),did) is None
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_PREPARATION","false")
    legacy=client.get(f"/disputes/{did}/workspace/prepare");assert legacy.status_code==200
    assert "not started; open the inbox to prepare" in legacy.text and "Open inbox" in legacy.text
    assert 'id="local-preparation"' not in legacy.text and repo.get_auto_prep(config.db_path(),did) is None
