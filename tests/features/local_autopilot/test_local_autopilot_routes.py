import html,json,os,re,uuid
from fastapi.testclient import TestClient

def test_simulation_route_flag_off_on_and_durable_inbox(monkeypatch):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.delenv('COPILOT_HOSTED_DEMO',raising=False)
 from copilot.app import create_app
 from copilot.features.local_autopilot import routes,service
 base='http://127.0.0.1';args={'base_url':base,'client':('127.0.0.1',12345)};path='/features/local_autopilot'
 assert TestClient(create_app(),**args).get(path).status_code==404
 folder=service.config.ROOT/'data'/'mejoras10'/'09'/('route-'+uuid.uuid4().hex);folder.mkdir(parents=True)
 monkeypatch.setattr(routes,'STORE_PATH',folder/'simulation.json')
 monkeypatch.setenv('COPILOT_FEATURE_LOCAL_AUTOPILOT','true')
 client=TestClient(create_app(),**args);page=client.get(path)
 token=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
 response=client.post(path,data={'csrf_token':html.unescape(token),'data':json.dumps([service.EXAMPLES[1],{**service.EXAMPLES[2],'case_id':'SYNTH-review-route'}])})
 assert response.status_code==200 and 'Simulation outcomes' in response.text and 'Durable simulation and exception inbox' in response.text
 assert 'SIMULATED' in response.text and 'exception' in response.text
 routes.STORE_PATH.unlink(missing_ok=True);routes.STORE_PATH.with_suffix('.json.lock').unlink(missing_ok=True);folder.rmdir()
