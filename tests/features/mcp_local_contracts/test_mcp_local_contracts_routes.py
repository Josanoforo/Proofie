import os
from fastapi.testclient import TestClient

def test_catalog_route_flag_off_on(monkeypatch):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.delenv('COPILOT_HOSTED_DEMO',raising=False)
 from copilot.app import create_app
 base='http://127.0.0.1';args={'base_url':base,'client':('127.0.0.1',12345)}
 assert TestClient(create_app(),**args).get('/features/mcp_local_contracts').status_code==404
 monkeypatch.setenv('COPILOT_FEATURE_MCP_LOCAL_CONTRACTS','true')
 response=TestClient(create_app(),**args).get('/features/mcp_local_contracts')
 assert response.status_code==200 and 'Protocol catalog' in response.text and 'prepare_local_note' in response.text
 assert 'accept-claim' in response.text
 from copilot.features.mcp_local_contracts import service
 assert 'accept-claim' not in service.TOOLS
