"""Opt-in real SDK/UI comparison, synthetic DB and injected GET-only provider."""
import asyncio, hashlib, json, os, re, sys
from pathlib import Path
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_sdk_snapshot_catalog_and_ui(tmp_path, monkeypatch):
    from copilot import config
    from copilot.db import repo
    from copilot.core.raw_storage import write_raw
    from copilot.web.routes_disputes import get_client
    from copilot.app import create_app
    import httpx
    def forbid_network(*args, **kwargs): raise AssertionError('external HTTP forbidden')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbid_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbid_network)
    monkeypatch.setattr(config, 'load_env_file', lambda: None)
    monkeypatch.setattr(config, '_db_path_override', None)
    monkeypatch.setattr(config, '_uploads_dir_override', None)
    monkeypatch.setenv('COPILOT_DATA_DIR', str(tmp_path))
    for key in list(os.environ):
        if key.startswith('COPILOT_FEATURE_'): monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('COPILOT_FEATURE_EVIDENCE_DOSSIER', 'true')
    monkeypatch.setenv('COPILOT_FEATURE_PRESENTATION_MODE', 'true')
    case='PP-R-DEMO-0000001'
    raw=json.loads(Path('pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json').read_text(encoding='utf-8'))['body']
    content=b'Synthetic original order record'; digest=hashlib.sha256(content).hexdigest()
    doc=repo.insert_document(config.db_path(),case,'synthetic-order.txt','text/plain',len(content),digest,content.decode(),True)
    write_raw(config.uploads_dir(),digest,content)
    extra=b'Synthetic attachment'; extra_hash=hashlib.sha256(extra).hexdigest()
    extra_doc=repo.insert_document(config.db_path(),case,'attachment.pdf','application/pdf',len(extra),extra_hash,'Synthetic attachment',True)
    write_raw(config.uploads_dir(),extra_hash,extra)
    package=repo.insert_package(config.db_path(),case,'human_edit',True,[])
    repo.attach_document(config.db_path(),package,extra_doc)
    repo.insert_claims(config.db_path(),package,[{'evidence_index':0,'claim_index':0,'text':'Synthetic source-backed statement', 'source_ids':[f'doc_{doc}'],'basis':'seller_statement'}])
    class Fake:
        def call(self,method,path,operation,**kwargs):
            assert method=='GET'
            if operation in ('show_dispute','mcp_show_dispute') and path.endswith('/'+case): return raw
            if operation=='context_transaction_search': return {'transaction_details':[]}
            if operation=='context_tracking': return {'trackers':[]}
            raise AssertionError(operation)
    app=create_app(); app.dependency_overrides[get_client]=lambda: Fake()
    web=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345))
    ui=web.get(f'/disputes/{case}/packages/{package}')
    assert ui.status_code==200 and 'Synthetic source-backed statement' in ui.text
    assert 'synthetic-order.txt' in ui.text and 'No approval recorded' in ui.text
    dossier_row = next(row for row in re.findall(r'<tr.*?</tr>', ui.text, re.S)
                       if 'Synthetic source-backed statement' in row and 'synthetic-order.txt' in row)
    assert "Open this case&#39;s records" in dossier_row or "Open this case\'s records" in dossier_row
    ui_claim = repo.list_claims(config.db_path(), package)[0]
    ui_source_ids = ui_claim['source_ids']
    assert ui_source_ids == [f'doc_{doc}']
    assert repo.list_documents(config.db_path(), case)[0]['filename'] == 'synthetic-order.txt'
    guide=web.get(f'/presentation-mode?case_id={case}&package_id={package}')
    assert guide.status_code==200 and f'doc_{doc}' in guide.text and 'synthetic-order.txt' in guide.text
    token=re.search(r'data-state="review-snapshot">([0-9a-f]{64})',guide.text).group(1)
    env={k:v for k,v in os.environ.items() if not k.startswith('COPILOT_FEATURE_') and k!='COPILOT_MCP_ALLOW_SEND'}
    async def run():
        params=lambda e: StdioServerParameters(command=sys.executable,args=['-m','tests.features.mcp_local_protocol.fake_server'],env=e)
        async with stdio_client(params(env)) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize(); catalog={t.name for t in (await client.list_tools()).tools}
                assert 'review_case_snapshot' not in catalog and 'send_approved_package' not in catalog
        enabled={**env,'COPILOT_FEATURE_PRESENTATION_MODE':'true'}
        async with stdio_client(params(enabled)) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize(); catalog={t.name for t in (await client.list_tools()).tools}
                assert 'review_case_snapshot' in catalog and 'send_approved_package' not in catalog and not any('approve' in x for x in catalog)
                async def call(pid,tok,case_id=case):
                    response=await client.call_tool('review_case_snapshot',{'dispute_id':case_id,'package_id':pid,'snapshot':tok})
                    assert not response.is_error
                    return response.structured_content or json.loads(response.content[0].text)
                current=await call(package,token)
                assert current['status']=='current' and current['source_ids']==ui_source_ids and current['package_id']==package and current['version']==1
                assert 'not refreshed' in current['limitation'] and 'does not authorize' in current['limitation']
                assert 'synthetic-order.txt' not in str(current) and 'Synthetic source-backed statement' not in str(current) and str(tmp_path) not in str(current)
                assert (await call(package,'bad'))['status']=='invalid'
                assert (await call(package,token,'PP-R-DEMO-0000002'))['status']=='unavailable'
                assert (await call(99999,token))['status']=='unavailable'
                (config.uploads_dir()/extra_hash).write_bytes(b'changed attachment')
                assert (await call(package,token))['status']=='stale'
                write_raw(config.uploads_dir(),extra_hash,extra)
                (config.uploads_dir()/digest).write_bytes(b'changed source')
                assert (await call(package,token))['status']=='stale'
                changed=web.get(f'/presentation-mode?case_id={case}&package_id={package}')
                changed_token=re.search(r'data-state="review-snapshot">([0-9a-f]{64})',changed.text).group(1)
                assert (await call(package,changed_token))['status']=='unavailable'
                (config.uploads_dir()/digest).unlink()
                missing_page=web.get(f'/presentation-mode?case_id={case}&package_id={package}')
                missing_token=re.search(r'data-state="review-snapshot">([0-9a-f]{64})',missing_page.text).group(1)
                assert (await call(package,missing_token))['status']=='unavailable'
                write_raw(config.uploads_dir(),digest,content)
                new=repo.insert_package(config.db_path(),case,'human_edit',True,[])
                assert (await call(package,token))['status']=='stale'
                newer=web.get(f'/presentation-mode?case_id={case}&package_id={new}')
                newer_token=re.search(r'data-state="review-snapshot">([0-9a-f]{64})',newer.text).group(1)
                assert (await call(new,newer_token))['status']=='current'
                provider_pkg=repo.insert_package(config.db_path(),case,'human_edit',True,[])
                repo.insert_claims(config.db_path(),provider_pkg,[{'evidence_index':0,'claim_index':0,'text':'Provider reference','source_ids':['paypal_transaction'],'basis':'seller_statement'}])
                provider_page=web.get(f'/presentation-mode?case_id={case}&package_id={provider_pkg}')
                provider_token=re.search(r'data-state="review-snapshot">([0-9a-f]{64})',provider_page.text).group(1)
                assert (await call(provider_pkg,provider_token))['status']=='unavailable'
                assert (await client.call_tool('send_approved_package',{'dispute_id':case})).is_error
    asyncio.run(run())
