"""Opt-in real SDK/UI comparison, synthetic DB and injected GET-only provider."""
import asyncio, hashlib, json, os, re, sys
from pathlib import Path
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_ui_and_mcp_same_package_sources_and_permissions(tmp_path, monkeypatch):
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
    case='PP-R-DEMO-0000001'
    raw=json.loads(Path('pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json').read_text(encoding='utf-8'))['body']
    content=b'Synthetic original order record'; digest=hashlib.sha256(content).hexdigest()
    doc=repo.insert_document(config.db_path(),case,'synthetic-order.txt','text/plain',len(content),digest,content.decode(),True)
    write_raw(config.uploads_dir(),digest,content)
    package=repo.insert_package(config.db_path(),case,'human_edit',True,[])
    repo.insert_claims(config.db_path(),package,[{'evidence_index':0,'claim_index':0,'text':'Synthetic source-backed statement', 'source_ids':[f'doc_{doc}'],'basis':'seller_statement'}])
    class Fake:
        def call(self,method,path,operation,**kwargs):
            assert method=='GET'
            if operation in ('show_dispute','mcp_show_dispute') and path.endswith('/'+case): return raw
            if operation=='context_transaction_search': return {'transaction_details':[]}
            if operation=='context_tracking': return {'trackers':[]}
            raise AssertionError(operation)
    app=create_app(); app.dependency_overrides[get_client]=lambda: Fake()
    ui=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345)).get(f'/disputes/{case}/packages/{package}')
    assert ui.status_code==200 and 'Synthetic source-backed statement' in ui.text
    assert 'synthetic-order.txt' in ui.text and 'No approval recorded' in ui.text
    dossier_row = next(row for row in re.findall(r'<tr.*?</tr>', ui.text, re.S)
                       if 'Synthetic source-backed statement' in row and 'synthetic-order.txt' in row)
    assert "Open this case&#39;s records" in dossier_row or "Open this case\'s records" in dossier_row
    ui_claim = repo.list_claims(config.db_path(), package)[0]
    ui_source_ids = ui_claim['source_ids']
    assert ui_source_ids == [f'doc_{doc}']
    assert repo.list_documents(config.db_path(), case)[0]['filename'] == 'synthetic-order.txt'
    env={k:v for k,v in os.environ.items() if not k.startswith('COPILOT_FEATURE_') and k!='COPILOT_MCP_ALLOW_SEND'}
    async def run():
        async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','tests.features.mcp_local_protocol.fake_server'],env=env)) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                result=await client.call_tool('get_dispute_file',{'dispute_id':case,'include_text':True})
                assert not result.is_error
                data=result.structured_content or json.loads(result.content[0].text)
                assert data['dispute']['dispute_id']==case and data['draft']['package_id']==package
                assert data['draft']['approval']=='not_approved'
                claim=data['draft']['claims'][0]
                assert claim['text']=='Synthetic source-backed statement' and claim['basis']=='seller_statement'
                assert data['draft']['version']==repo.get_package(config.db_path(),package)['version']
                assert claim['source_ids']==ui_source_ids
                default=await client.call_tool('get_dispute_file',{'dispute_id':case})
                assert not default.is_error
                default_data=default.structured_content or json.loads(default.content[0].text)
                assert default_data['dispute']['dispute_id']==case and default_data['draft']['package_id']==package
                assert default_data['draft']['version']==data['draft']['version']
                assert default_data['draft']['approval']=='not_approved'
                assert 'claims' not in default_data['draft'] and default_data['text_included'] is False
                assert 'Synthetic source-backed statement' not in str(default.content)
                assert f'doc_{doc}' not in str(default.content)
                assert (await client.call_tool('send_approved_package',{'dispute_id':case})).is_error
                assert (await client.call_tool('get_dispute_file',{'dispute_id':'../invalid'})).is_error
    asyncio.run(run())

