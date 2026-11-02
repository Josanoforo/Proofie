import html,json,os,re
from fastapi.testclient import TestClient

def test_flag_off_on_and_human_readable_offer_post(monkeypatch,tmp_path):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.delenv('COPILOT_HOSTED_DEMO',raising=False)
 from copilot.app import create_app
 from copilot.features.negotiation_drafts import routes
 monkeypatch.setattr(routes,'HISTORY_PATH',tmp_path/'history.jsonl')
 path='/features/negotiation_drafts';base='http://127.0.0.1'
 assert TestClient(create_app(),base_url=base,client=('127.0.0.1',12345)).get(path).status_code==404
 monkeypatch.setenv('COPILOT_FEATURE_NEGOTIATION_DRAFTS','true')
 client=TestClient(create_app(),base_url=base,client=('127.0.0.1',12345));page=client.get(path)
 token=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
 from copilot.features.negotiation_drafts.service import EXAMPLE
 response=client.post(path,data={'csrf_token':html.unescape(token),'data':json.dumps(EXAMPLE)})
 assert response.status_code==200 and 'Proposals' in response.text and 'Recorded responses' in response.text and 'partial resolution' in response.text
