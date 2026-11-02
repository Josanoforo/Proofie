import copy
from copilot.features.local_autopilot.gap_resolution import resolve_inputs
from copilot.features.negotiation_drafts.incident_binding import incident_fingerprint


def setup():
    did='PP-R-DEMO-0000001'; capture='DEMOTXN0000000001'
    return dict(dispute_id=did, package={'id':7,'dispute_id':did}, capture_id=capture,
        norm={'dispute_id':did,'seller_transaction_ids':[capture],'amount_cents':2500,'currency':'JPY'},
        manual_fields={},editable=True,readiness_record=None,shipment_record={'capture_id':capture,'prefill_allowed':True,'tracker':{'tracking_number':'TRACK-1','carrier':'FEDEX'}})


def test_projection_conflict_binding_prefill_and_unknown():
    data=setup(); result=resolve_inputs(**data)
    assert result['ok'] and result['fields']['tracking_number']['status']=='suggested'
    assert '?use_tracking=1#field-tracking_number' in result['fields']['tracking_number']['href']
    assert result['fields']['locale']['status']=='missing' and result['fields']['country']['value'] is None
    assert result['fields']['amount']['value']=={'minor_units':2500,'currency':'JPY'}
    data['manual_fields']={'tracking_number':{'value':'TRACK-2','confidence':'read'},'city':{'value':'Puebla','confidence':'read'}}
    conflict=resolve_inputs(**data); row=conflict['fields']['tracking_number']
    assert row['status']=='confirmed_by_seller' and row['value']=='TRACK-2' and row['conflict'] and len(row['candidates'])==2
    assert '?use_tracking' not in row['href'] and data['manual_fields']['city']['value']=='Puebla'
    assert conflict['fingerprint'] != result['fingerprint']
    readonly=resolve_inputs(**{**data,'editable':False})
    assert readonly['fields']['tracking_number']['href'] is None
    foreign=resolve_inputs(**{**data,'package':{'id':7,'dispute_id':'PP-R-DEMO-0000002'}})
    assert not foreign['ok'] and not foreign['fields']
    assert not resolve_inputs(**{**data,'capture_id':'OTHER'})['ok']


def test_incident_binding_and_human_draft_preserved(tmp_path):
    import pytest
    from copilot.features.negotiation_drafts import service
    context=resolve_inputs(**setup())
    kwargs={'gap_context':context,'incident':'delay','locale':'es','seller_text':'Texto editado por vendedor.', 'readiness_record':{'expected_date':'2026-11-01'},'shipment_record':{'tracking':'TRACK-1'}}
    fp=incident_fingerprint(**kwargs)
    assert incident_fingerprint(**kwargs)==fp
    for change in ({'locale':'en'}, {'seller_text':'Otro texto'}, {'shipment_record':{'tracking':'TRACK-2'}}, {'readiness_record':{'expected_date':'2026-11-02'}}):
        assert incident_fingerprint(**{**kwargs,**change})!=fp
    with pytest.raises(ValueError): incident_fingerprint(**{**kwargs,'locale':''})
    row={'locale':'es','recipient':'Buyer','currency':'USD','allowed_actions':['message'],'facts':[{'id':'seller','text':'Recorded source','source':'seller'}],
         'proposals':[{'id':'one','kind':'followup','amount_cents':0,'conditions':'human note','seller_text':kwargs['seller_text'],'source_incident':{'fingerprint':fp,'incident':'delay','locale':'es'}}], 'responses':[]}
    path=tmp_path/'history.jsonl'; first=service.build(copy.deepcopy(row),path); second=service.build(copy.deepcopy(row),path)
    assert first['proposals'][0]['draft']==kwargs['seller_text'] and len(second['history_versions'])==1
    assert first['proposals'][0]['source_incident']['fingerprint']==fp


def test_existing_complete_form_prefill_and_save_same_package(monkeypatch,tmp_path):
    import json, os, re
    from pathlib import Path
    from datetime import datetime, timezone
    import httpx
    from _hosted import authenticated_client
    from copilot import config
    from copilot.app import create_app
    from copilot.core.disputes import normalize
    from copilot.db import repo
    from copilot.features.fulfilment import store
    from copilot.features.local_autopilot.gap_resolution import current_case
    from copilot.web.routes_disputes import get_client
    def forbidden(*args, **kwargs): raise AssertionError('external transport forbidden')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    monkeypatch.setattr(config,'load_env_file',lambda:None)
    monkeypatch.setattr(config,'_db_path_override',tmp_path/'case.sqlite')
    monkeypatch.setattr(config,'_uploads_dir_override',tmp_path/'uploads')
    monkeypatch.setenv('COPILOT_DATA_DIR',str(tmp_path/'data'))
    for key in list(os.environ):
        if key.startswith('COPILOT_FEATURE_'): monkeypatch.delenv(key,raising=False)
    for flag in ('LOCAL_AUTOPILOT','GAP_RESOLUTION','FULFILMENT'): monkeypatch.setenv('COPILOT_FEATURE_'+flag,'true')
    did='PP-R-DEMO-0000001'; cap='DEMOTXN0000000001'
    raw=json.loads(Path('pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json').read_text(encoding='utf-8'))['body']
    class Fake:
        def call(self, method, path, operation, **kwargs):
            assert method=='GET'
            if operation=='show_dispute': return raw
            if operation=='context_transaction_search': return {'transaction_details':[]}
            if operation=='context_tracking': return {'trackers':[]}
            if operation=='list_disputes': return {'items':[],'links':[]}
            raise AssertionError(operation)
    package=repo.insert_package(config.db_path(),did,'human_edit',True,[])
    repo.set_manual_fields(config.db_path(),did,{'city':{'value':'Puebla','confidence':'read'},'postal_code':{'value':'72000','confidence':'read'}})
    tracker={'trackers':[{'transaction_id':cap,'tracking_number':'TRACK-1','carrier':'FEDEX'}]}
    store.save_file(cap,tracker,'synthetic-request','synthetic-approval',{},[],status='registered')
    norm=normalize(raw,datetime.now(timezone.utc))
    projection=current_case(config.db_path(),did,package,norm,capture_id=cap,editable=True)
    link=projection['fields']['tracking_number']['href']
    assert '?use_tracking=1#field-tracking_number' in link
    app=create_app(); app.dependency_overrides[get_client]=lambda:Fake()
    client=authenticated_client(app,client=('127.0.0.1',12345))
    response=client.get(link)
    assert response.status_code==200 and 'value="TRACK-1"' in response.text and 'value="Puebla"' in response.text
    token=re.search(r'name="csrf_token" value="([^"]+)"',response.text).group(1)
    posted=client.post(f'/disputes/{did}/manual-fields',data={'csrf_token':token,'package_id':str(package),'tracking_number':'TRACK-1','carrier':'FEDEX','city':'Puebla','postal_code':'72000'},follow_redirects=False)
    assert posted.status_code==303 and f'/packages/{package}' in posted.headers['location']
    fields=repo.get_manual_fields(config.db_path(),did)
    assert fields['tracking_number']['value']=='TRACK-1' and fields['city']['value']=='Puebla' and fields['postal_code']['value']=='72000'
    updated=current_case(config.db_path(),did,package,norm,capture_id=cap,editable=True)
    assert updated['fields']['tracking_number']['status']=='confirmed_by_seller'


def test_source_read_failure_returns_no_values(monkeypatch,tmp_path):
    import sqlite3
    from copilot.db import repo
    from copilot.features.local_autopilot.gap_resolution import current_case
    monkeypatch.setattr(repo,'get_package',lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('unavailable')))
    result=current_case(tmp_path/'db','PP-R-DEMO-0000001',7,{'dispute_id':'PP-R-DEMO-0000001'})
    assert not result['ok'] and result['fields']=={} and 'unavailable' in result['error']


def test_malformed_binding_is_rejected():
    base=setup()
    assert not resolve_inputs(**{**base,'norm':{**base['norm'],'seller_transaction_ids':base['capture_id']}})['ok']
    assert not resolve_inputs(**{**base,'readiness_record':'invalid'})['ok']
    assert not resolve_inputs(**{**base,'shipment_record':'invalid'})['ok']


def test_preparation_router_lazy_flag(monkeypatch):
    import importlib
    import copilot.features.local_autopilot.routes as routes
    monkeypatch.setenv('COPILOT_FEATURE_LOCAL_AUTOPILOT','true')
    monkeypatch.delenv('COPILOT_FEATURE_LOCAL_PREPARATION',raising=False)
    routes=importlib.reload(routes)
    assert not any(getattr(r,'path','').endswith('/local-preparation') for r in routes.router.routes)
    monkeypatch.setenv('COPILOT_FEATURE_LOCAL_PREPARATION','true')
    routes=importlib.reload(routes)
    included = [r.original_router for r in routes.router.routes if hasattr(r, 'original_router')]
    assert sum(getattr(r, 'path', '').endswith('/local-preparation') for router in included for r in router.routes) == 1


def test_confirmed_correction_full_form_propagates_with_warning(monkeypatch,tmp_path):
    import json, os, re
    from datetime import datetime, timezone
    from pathlib import Path
    import httpx
    from _hosted import authenticated_client
    from copilot import config
    from copilot.app import create_app
    from copilot.core.disputes import normalize
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.db import repo
    from copilot.features.fulfilment import store
    from copilot.features.local_autopilot.gap_resolution import current_case
    from copilot.features.sale_readiness import service
    from copilot.web.routes_disputes import get_client
    def forbidden(*args, **kwargs): raise AssertionError('external transport forbidden')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    monkeypatch.setattr(config,'load_env_file',lambda:None)
    monkeypatch.setattr(config,'_db_path_override',tmp_path/'case.sqlite')
    monkeypatch.setattr(config,'_uploads_dir_override',tmp_path/'uploads')
    monkeypatch.setenv('COPILOT_DATA_DIR',str(tmp_path/'data'))
    for key in list(os.environ):
        if key.startswith('COPILOT_FEATURE_'): monkeypatch.delenv(key,raising=False)
    for flag in ('LOCAL_AUTOPILOT','GAP_RESOLUTION','FULFILMENT','SALE_READINESS'): monkeypatch.setenv('COPILOT_FEATURE_'+flag,'true')
    did='PP-R-DEMO-0000001'; cap='DEMOTXN0000000001'
    raw=json.loads(Path('pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json').read_text(encoding='utf-8'))['body']
    class Fake:
        def call(self, method, path, operation, **kwargs):
            assert method=='GET'
            if operation=='show_dispute': return raw
            if operation=='context_transaction_search': return {'transaction_details':[]}
            if operation=='context_tracking': return {'trackers':[]}
            if operation=='list_disputes': return {'items':[],'links':[]}
            raise AssertionError(operation)
    package=repo.insert_package(config.db_path(),did,'human_edit',True,[])
    repo.set_manual_fields(config.db_path(),did,{'city':{'value':'Puebla','confidence':'read'},'postal_code':{'value':'72000','confidence':'read'}})
    store.save_file(cap,{'trackers':[{'transaction_id':cap,'tracking_number':'OLD-SHIP','carrier':'FEDEX'}]},'synthetic-request','synthetic-approval',{},[],status='registered')
    terms={k:v for k,v in service.EXAMPLES['physical'].items() if k not in ('evidence','transaction_ref')}
    terms.update(tracking_number='OLD-TERMS',carrier='FEDEX',evidence_kinds={'1':'order','2':'tracking'})
    record=service.save_case_terms(did,cap,terms)
    norm=normalize(raw,datetime.now(timezone.utc))
    before=current_case(config.db_path(),did,package,norm,capture_id=cap,editable=True)
    assert before['fields']['tracking_number']['status']=='conflict' and before['fields']['tracking_number']['value'] is None
    bad=resolve_inputs(dispute_id=did,package={'id':package,'dispute_id':did},capture_id=cap,norm=norm,manual_fields={'tracking_number':{'value':'GUESS','confidence':'illegible'}},readiness_record=record,shipment_record={'capture_id':cap,'tracker':{'tracking_number':'OLD-SHIP'}})
    assert bad['fields']['tracking_number']['value'] is None and bad['fields']['tracking_number']['status']=='conflict'
    assert before['fields']['locale']['status']=='missing'
    app=create_app(); app.dependency_overrides[get_client]=lambda:Fake()
    client=authenticated_client(app,client=('127.0.0.1',12345))
    response=client.get(f'/disputes/{did}/packages/{package}')
    assert response.status_code==200 and 'value="Puebla"' in response.text
    token=re.search(r'name="csrf_token" value="([^"]+)"',response.text).group(1)
    posted=client.post(f'/disputes/{did}/manual-fields',data={'csrf_token':token,'package_id':str(package),'tracking_number':'NEW-TRACK-2','carrier':'FEDEX','city':'Puebla','postal_code':'72000'},follow_redirects=False)
    assert posted.status_code==303 and f'/packages/{package}' in posted.headers['location']
    page=client.get(posted.headers['location']); assert page.status_code==200 and 'NEW-TRACK-2' in page.text and 'Puebla' in page.text
    fields=repo.get_manual_fields(config.db_path(),did)
    assert fields['city']['value']=='Puebla' and fields['postal_code']['value']=='72000'
    docs=extracted_docs_for(config.db_path(),did)
    manual=next(doc for doc in docs if doc.doc_id=='manual')
    assert manual.fields['tracking_number']=={'value':'NEW-TRACK-2','confidence':'read'}
    after=current_case(config.db_path(),did,package,norm,capture_id=cap,editable=True); row=after['fields']['tracking_number']
    assert row['value']=='NEW-TRACK-2' and row['status']=='confirmed_by_seller' and row['source_kind']=='manual_fields' and row['source_ref']==f'case:{did}' and row['conflict']
    assert {c['value'] for c in row['candidates']}=={'NEW-TRACK-2','OLD-SHIP','OLD-TERMS'}
    assert after['fingerprint']!=before['fingerprint'] and current_case(config.db_path(),did,package,norm,capture_id=cap,editable=True)['fingerprint']==after['fingerprint']
    view=service.case_view(did,cap,{},[{'id':1,'filename':'order.txt'},{'id':2,'filename':'tracking.txt'}],[],record,{'tracking_number':'OLD-SHIP','carrier':'FEDEX'})['result']
    assert {x['other_value'] for x in view['source_conflicts']}=={'OLD-SHIP','OLD-TERMS'} and view['source_warnings']
    assert view['tracking_payload']['tracking_number']=='NEW-TRACK-2'
    assert service.load_case_terms(did,cap)['terms']['tracking_number']=='OLD-TERMS'
    assert json.loads(store.get_file(cap)['request_json'])['trackers'][0]['tracking_number']=='OLD-SHIP'
    changed=resolve_inputs(dispute_id=did,package={'id':package,'dispute_id':did},capture_id=cap,norm=norm,manual_fields=fields,readiness_record=record,shipment_record={'capture_id':cap,'tracker':{'tracking_number':'DIFFERENT','carrier':'FEDEX'}},editable=True)
    assert changed['fingerprint']!=after['fingerprint']


def test_current_case_derives_local_link_guard(monkeypatch, tmp_path):
    import sqlite3
    from copilot import config
    from copilot.db import repo
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.features.local_autopilot.gap_resolution import current_case
    from copilot.web import status
    monkeypatch.setattr(config,'_db_path_override',tmp_path/'case.sqlite')
    monkeypatch.delenv('COPILOT_FEATURE_SALE_READINESS',raising=False)
    monkeypatch.delenv('COPILOT_FEATURE_FULFILMENT',raising=False)
    did='PP-R-DEMO-0000001'; cap='DEMOTXN0000000001'
    norm={'dispute_id':did,'seller_transaction_ids':[cap],'amount_cents':2500,'currency':'USD'}
    pkg=repo.insert_package(config.db_path(),did,'human_edit',True,[])
    def read(**kwargs): return current_case(config.db_path(),did,pkg,norm,capture_id=cap,**kwargs)
    def link(row): return row['fields']['tracking_number']['href']
    draft=read(); assert link(draft)==f'/disputes/{did}/packages/{pkg}#field-tracking_number'
    assert link(read(editable=False)) is None and read(editable=False)['fingerprint']==draft['fingerprint']
    assert resolve_inputs(dispute_id=did,package={'id':pkg,'dispute_id':did},capture_id=cap,norm=norm,manual_fields={})['fields']['tracking_number']['href'] is None
    store=SQLiteAttemptStore(config.db_path())
    try:
        store.write(did,'a'*64,{'state':'pending','request_id':'local-test'})
        repo.log_action(config.db_path(),'vendor','send_attempt_registered',did,{'package_id':pkg,'request_id':'local-test'})
        assert link(read()) is None
        store.write(did,'a'*64,{'state':'confirmed','request_id':'local-test'})
        assert link(read()) is None
    finally: store.close()
    assert not current_case(config.db_path(),'PP-R-DEMO-0000002',pkg,{**norm,'dispute_id':'PP-R-DEMO-0000002'},capture_id=cap)['ok']
    def unavailable(*a,**kw): raise sqlite3.OperationalError('unavailable')
    monkeypatch.setattr(status,'send_record',unavailable)
    assert link(read()) is None
    assert link(read(editable=True)) is not None
