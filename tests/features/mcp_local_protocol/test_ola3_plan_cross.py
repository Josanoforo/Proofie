import hashlib, html, json, os, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from _hosted import authenticated_client


def test_source_change_invalidates_plan_bound_preview_preserves_human_text(monkeypatch, tmp_path):
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
    document=repo.insert_document(config.db_path(),did,'case.pdf','application/pdf',6,hashlib.sha256(b'source').hexdigest(),'Recorded source',True)
    app=create_app(); app.dependency_overrides[get_client]=lambda:Fake()
    client=authenticated_client(app,client=('127.0.0.1',12345))
    def get_plan():
        p=client.get(f'/disputes/{did}/workspace/prepare'); assert p.status_code==200
        panel=p.text.split('data-state="attention-plan"',1)[1].split('</details>',1)[0]
        fp=re.search(r'name="fingerprint" value="([a-f0-9]+)"',panel).group(1)
        csrf=re.search(r'name="csrf_token" value="([^"]+)"',panel).group(1)
        return p,fp,csrf
    page,fp,csrf=get_plan()
    assert 'Recorded source' in page.text or 'doc_1' in page.text
    draft=client.post(f'/disputes/{did}/local-autopilot/plan-draft',data={'csrf_token':csrf,'fingerprint':fp,'kind':'followup','recipient':'Buyer','conditions':'Human note'})
    assert draft.status_code==200
    token=re.search(r'name="csrf_token" value="([^"]+)"',draft.text).group(1)
    logical=re.search(r'name="logical_id" value="([^"]+)"',draft.text).group(1)
    repo.set_document_excluded(config.db_path(),document,True)
    stale=client.post(f'/disputes/{did}/local-autopilot/plan-preview',data={'csrf_token':token,'logical_id':logical})
    assert stale.status_code==409 and 'saved draft remains' in stale.text
    from copilot.features.negotiation_drafts.service import read_case_history
    assert read_case_history(did)[0]['conditions']=='Human note'
    repo.set_document_excluded(config.db_path(),document,False)
    _,fresh_fp,fresh_csrf=get_plan()
    fresh=client.post(f'/disputes/{did}/local-autopilot/plan-draft',data={'csrf_token':fresh_csrf,'fingerprint':fresh_fp,'kind':'followup','recipient':'Buyer','conditions':'Human note'})
    assert fresh.status_code==200
    token=re.search(r'name="csrf_token" value="([^"]+)"',fresh.text).group(1)
    logical=re.search(r'name="logical_id" value="([^"]+)"',fresh.text).group(1)
    preview=client.post(f'/disputes/{did}/local-autopilot/plan-preview',data={'csrf_token':token,'logical_id':logical},follow_redirects=False)
    assert preview.status_code==303 and client.get(preview.headers['location']).status_code==200
