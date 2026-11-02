import html,json,os,re
from fastapi.testclient import TestClient

def test_flag_off_on_and_case_report_post(monkeypatch):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.delenv('COPILOT_HOSTED_DEMO',raising=False)
 from copilot.app import create_app
 path='/features/evaluation_workbench';base='http://127.0.0.1'
 assert TestClient(create_app(),base_url=base,client=('127.0.0.1',12345)).get(path).status_code==404
 monkeypatch.setenv('COPILOT_FEATURE_EVALUATION_WORKBENCH','true')
 client=TestClient(create_app(),base_url=base,client=('127.0.0.1',12345));page=client.get(path)
 token=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
 from copilot.features.evaluation_workbench.service import EXAMPLE
 response=client.post(path,data={'csrf_token':html.unescape(token),'data':json.dumps(EXAMPLE)})
 assert response.status_code==200 and 'Case report' in response.text and 'NOTRUN' in response.text and 'CUT72 R04' in response.text
 assert 'does not open or authenticate the referenced bytes' in response.text


def test_page_shows_the_last_held_out_run_and_which_path_ran(monkeypatch, tmp_path):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.delenv('COPILOT_HOSTED_DEMO',raising=False)
 monkeypatch.setenv('COPILOT_FEATURE_EVALUATION_WORKBENCH','true')
 monkeypatch.setenv('COPILOT_DATA_DIR',str(tmp_path))
 from copilot.app import create_app
 client=TestClient(create_app(),base_url='http://127.0.0.1',client=('127.0.0.1',12345))
 assert 'None yet' in client.get('/features/evaluation_workbench').text
 import sys
 sys.path.append(str(config.ROOT/'tools'))
 import eval_draft
 eval_draft.run_heldout(tmp_path/'eval',use_model=False)
 text=html.unescape(client.get('/features/evaluation_workbench').text)
 assert 'No model ran' in text and 'Path: replay' in text and 'injection_heldout' in text and 'manual fallback' in text.lower()
