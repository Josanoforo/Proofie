from copilot.features.judge_walkthrough import service
def test_walkthrough_is_offline_and_rehearsals_not_run():
 assert not service.MANIFEST['network_required'] and not service.MANIFEST['env_required'];assert all(x['status']=='NOTRUN' for x in service.FORMS.values());assert len(service.MANIFEST['steps'])>=3
 assert service.MANIFEST['entrypoint'].endswith('copilot.features.judge_walkthrough.runner')

def test_actual_habitual_inbox_case_package_journey_and_environment_restore():
 import os
 from copilot import config
 before_env=dict(os.environ);before_loader=config.load_env_file
 from copilot.features.judge_walkthrough.runner import run_journey
 result=run_journey()
 assert result['guide_status']==200 and result['inbox_status']==200 and result['detail_status']==200 and result['package_status']==200
 assert result['provider_state']=='NOT USED' and len(result['visible_sections'])==4
 assert result['execution_mode']=='in-process ASGI TestClient; no listening server'
 assert result['visited'][0]=='/ redirect' and result['visited'][1]=='/disputes inbox'
 assert 'same-case detail' in result['visible_sections'] and 'evaluation provenance' in result['visible_sections']
 assert dict(os.environ)==before_env and config.load_env_file is before_loader

def test_walkthrough_route_is_absent_when_flag_off(monkeypatch):
 import os
 from _hosted import authenticated_client
 from fastapi.testclient import TestClient
 from copilot import config
 monkeypatch.setattr(config,'load_env_file',lambda:None)
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 from copilot.app import create_app
 client=authenticated_client(create_app(),client=('127.0.0.1',12345))
 assert client.get('/features/judge_walkthrough').status_code==404
