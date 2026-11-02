import hashlib, html, json, os, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from _hosted import authenticated_client


def test_optin_plan_binding_and_fallback(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.core import auto_prep
    from copilot.db import repo
    from copilot.features.local_autopilot import resources, model_adapter
    from copilot.web.routes_disputes import get_client
    def forbidden(*args, **kwargs): raise AssertionError('network or model forbidden')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    monkeypatch.setattr(config,'load_env_file',lambda:None)
    monkeypatch.setattr(config,'_db_path_override',tmp_path/'case.sqlite')
    monkeypatch.setattr(config,'_uploads_dir_override',tmp_path/'uploads')
    monkeypatch.setenv('COPILOT_DATA_DIR',str(tmp_path/'data'))
    for key in list(os.environ):
        if key.startswith('COPILOT_FEATURE_'): monkeypatch.delenv(key,raising=False)
    for flag in ('LOCAL_AUTOPILOT','RESOLUTION','RESOLUTION_WRITES','NEGOTIATION_DRAFTS'):
        monkeypatch.setenv('COPILOT_FEATURE_'+flag,'true')
    monkeypatch.setattr(auto_prep,'should_prepare',lambda _:False)
    did='PP-R-DEMO-0000001'
    raw=json.loads(Path('pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json').read_text(encoding='utf-8'))['body']
    raw['seller_response_due_date']=(datetime.now(timezone.utc)+timedelta(days=3)).isoformat()
    class Fake:
        def call(self, method, path, operation, **kwargs):
            assert method=='GET'
            if operation in ('show_dispute','resolution_read'): return raw
            if operation=='context_transaction_search': return {'transaction_details':[]}
            if operation=='context_tracking': return {'trackers':[]}
            if operation=='list_disputes': return {'items':[], 'links':[]}
            raise AssertionError(operation)
    repo.insert_document(config.db_path(),did,'case.pdf','application/pdf',6,hashlib.sha256(b'source').hexdigest(),'Recorded source',True)
    app=create_app(); app.dependency_overrides[get_client]=lambda:Fake()
    client=authenticated_client(app,client=('127.0.0.1',12345))
    def get_plan():
        p=client.get(f'/disputes/{did}/workspace/prepare'); assert p.status_code==200
        panel=p.text.split('data-state="attention-plan"',1)[1].split('</details>',1)[0]
        fp=re.search(r'name="fingerprint" value="([a-f0-9]+)"',panel).group(1)
        csrf=re.search(r'name="csrf_token" value="([^"]+)"',panel).group(1)
        return p,fp,csrf
    page,fp,csrf=get_plan()
    assert 'model-review' not in page.text
    assert client.post(f'/disputes/{did}/local-autopilot/model-review',data={'csrf_token':csrf,'fingerprint':fp},follow_redirects=False).status_code==404
    monkeypatch.setenv('COPILOT_FEATURE_ATTENTION_MODEL','true')
    page,fp,csrf=get_plan()
    assert client.post(f'/disputes/{did}/local-autopilot/model-review',data={'csrf_token':'bad','fingerprint':fp}).status_code==403
    calls=[]
    monkeypatch.setattr(resources,'resources_available',lambda:False)
    monkeypatch.setattr(model_adapter,'existing_model',lambda *a: calls.append(1))
    busy=client.post(f'/disputes/{did}/local-autopilot/model-review',data={'csrf_token':csrf,'fingerprint':fp},follow_redirects=False)
    assert 'attention_model=busy' in busy.headers['location'] and not calls
    monkeypatch.setattr(resources,'resources_available',lambda:True)
    from test_attention_ola3 import model_record
    monkeypatch.setattr(model_adapter,'existing_model',lambda *a:model_record())
    _,fp,csrf=get_plan()
    good=client.post(f'/disputes/{did}/local-autopilot/model-review',data={'csrf_token':csrf,'fingerprint':fp},follow_redirects=False)
    assert 'attention_model=ready' in good.headers['location']
    page,fp,csrf=get_plan(); assert 'Model suggestion:' in page.text and 'doc_1' in page.text
    monkeypatch.setattr(model_adapter,'existing_model',lambda *a:model_record('doc_1','Paid 9000 USD'))
    invalid=client.post(f'/disputes/{did}/local-autopilot/model-review',data={'csrf_token':csrf,'fingerprint':fp},follow_redirects=False)
    assert 'attention_model=unavailable' in invalid.headers['location']
    page,fp,csrf=get_plan(); assert 'Model suggestion:' not in page.text and 'Paid 9000 USD' not in page.text
    monkeypatch.setattr(model_adapter,'existing_model',lambda *a:(_ for _ in ()).throw(RuntimeError('offline')))
    failed=client.post(f'/disputes/{did}/local-autopilot/model-review',data={'csrf_token':csrf,'fingerprint':fp},follow_redirects=False)
    assert 'attention_model=unavailable' in failed.headers['location']
    page,fp,csrf=get_plan(); assert 'Model suggestion:' not in page.text
    monkeypatch.setattr(model_adapter,'existing_model',lambda *a:model_record())
    assert 'attention_model=ready' in client.post(f'/disputes/{did}/local-autopilot/model-review',data={'csrf_token':csrf,'fingerprint':fp},follow_redirects=False).headers['location']
    page,fp,csrf=get_plan(); assert 'Model suggestion:' in page.text
    raw['update_time']='2026-10-31T20:20:00Z'
    page2,newfp,csrf2=get_plan(); assert newfp!=fp and 'Model suggestion:' not in page2.text
    stale=client.post(f'/disputes/{did}/local-autopilot/plan-draft',data={'csrf_token':csrf2,'fingerprint':fp,'kind':'followup','recipient':'Buyer','conditions':'Human note'},follow_redirects=False)
    assert stale.status_code==409
    _,newfp,csrf2=get_plan()
    draft=client.post(f'/disputes/{did}/local-autopilot/plan-draft',data={'csrf_token':csrf2,'fingerprint':newfp,'kind':'followup','recipient':'Buyer','conditions':'Human note'})
    assert draft.status_code==200 and 'Review this local draft' in draft.text
    token=re.search(r'name="csrf_token" value="([^"]+)"',draft.text).group(1)
    logical=re.search(r'name="logical_id" value="([^"]+)"',draft.text).group(1)
    raw['update_time']='2026-10-31T20:21:00Z'
    stale_preview=client.post(f'/disputes/{did}/local-autopilot/plan-preview',data={'csrf_token':token,'logical_id':logical})
    assert stale_preview.status_code==409 and 'saved draft remains' in stale_preview.text
    from copilot.features.negotiation_drafts.service import read_case_history
    assert len(read_case_history(did))==1 and read_case_history(did)[0]['conditions']=='Human note'
    _,fp3,csrf3=get_plan()
    fresh=client.post(f'/disputes/{did}/local-autopilot/plan-draft',data={'csrf_token':csrf3,'fingerprint':fp3,'kind':'followup','recipient':'Buyer','conditions':'Human note'})
    assert fresh.status_code==200
    token=re.search(r'name="csrf_token" value="([^"]+)"',fresh.text).group(1)
    logical=re.search(r'name="logical_id" value="([^"]+)"',fresh.text).group(1)
    preview=client.post(f'/disputes/{did}/local-autopilot/plan-preview',data={'csrf_token':token,'logical_id':logical},follow_redirects=False)
    assert preview.status_code==303 and '/resolution/check/' in preview.headers['location']
    assert client.get(preview.headers['location']).status_code==200


def test_display_currency_exponents(monkeypatch,tmp_path):
    from copilot import config
    from copilot.db import repo
    from copilot.features.local_autopilot.assistance import case_context
    monkeypatch.setattr(config,'_db_path_override',tmp_path/'db.sqlite')
    repo.ensure_schema(config.db_path())
    n={'dispute_id':'PP-R-DEMO-0000001','status':'OPEN','amount_cents':2500}
    assert case_context(config.db_path(),{**n,'currency':'JPY'},[])['amount_display']=='2500 JPY'
    assert case_context(config.db_path(),{**n,'currency':'USD'},[])['amount_display']=='25.00 USD'
    assert case_context(config.db_path(),{**n,'currency':'ZZZ'},[])['amount_display'] is None


def test_resource_preflight_fail_closed(monkeypatch):
    from types import SimpleNamespace
    from copilot.features.local_autopilot import resources
    class Reply:
        def __init__(self, value): self.value=value
        def raise_for_status(self): pass
        def json(self): return self.value
    monkeypatch.setattr(resources.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='200\n'))
    monkeypatch.setattr(resources.httpx,'get',lambda *a,**k: (_ for _ in ()).throw(AssertionError('must not read Ollama when GPU busy')))
    assert not resources.resources_available()
    monkeypatch.setattr(resources.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=''))
    for payload in ({}, {'models':[{'name':'foreign'}]}, {'models':'bad'}):
        monkeypatch.setattr(resources.httpx,'get',lambda *a, payload=payload, **k:Reply(payload))
        assert not resources.resources_available()
    monkeypatch.setattr(resources.httpx,'get',lambda *a,**k:Reply({'models':[]}))
    assert resources.resources_available()


def test_prepare_fragment_uses_currency_aware_display(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from jinja2 import Environment, FileSystemLoader
    from copilot import config
    from copilot.db import repo
    from copilot.features.local_autopilot.assistance import case_context
    monkeypatch.setattr(config,'_db_path_override',tmp_path/'db.sqlite'); repo.ensure_schema(config.db_path())
    env=Environment(loader=FileSystemLoader('copilot/web/templates'),autoescape=True)
    env.globals['feature_enabled']=lambda flag: flag=='local_autopilot'
    env.filters['when']=lambda value:value
    base={'dispute_id':'PP-R-DEMO-0000001','status':'OPEN','amount_cents':2500}
    for currency,expected in [('JPY','2500 JPY'),('USD','25.00 USD'),('ZZZ','unavailable for this currency')]:
        norm={**base,'currency':currency}
        ctx=case_context(config.db_path(),norm,[])
        markup=env.get_template('features/local_autopilot/_assistance.html').render(assistance_context=ctx,norm=norm,view='prepare',request=SimpleNamespace(query_params={}),status_view={'label':'Open'},due=None,local_autopilot_actions=[],local_autopilot_evidence_count=0,local_autopilot_currency_supported=False,local_autopilot_error=False,local_autopilot_amount_error=False,local_autopilot_history=[])
        assert expected in markup
