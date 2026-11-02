import html,json,os,re
from fastapi.testclient import TestClient

def test_flag_off_on_and_human_readable_dossier_post(monkeypatch):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.delenv('COPILOT_HOSTED_DEMO',raising=False)
 from copilot.app import create_app
 path='/features/evidence_dossier';base='http://127.0.0.1'
 assert TestClient(create_app(),base_url=base,client=('127.0.0.1',12345)).get(path).status_code==404
 monkeypatch.setenv('COPILOT_FEATURE_EVIDENCE_DOSSIER','true')
 client=TestClient(create_app(),base_url=base,client=('127.0.0.1',12345));page=client.get(path)
 token=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
 from copilot.features.evidence_dossier.service import EXAMPLES
 response=client.post(path,data={'csrf_token':html.unescape(token),'data':json.dumps(EXAMPLES['physical'])})
 assert response.status_code==200 and 'Reason to proof and source matrix' in response.text and 'Chronology' in response.text and 'Annex inventory and preview' in response.text
