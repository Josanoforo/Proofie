def test_versioned_claim_artifact_dossier_and_status_index():
 import hashlib
 from copilot.features.local_demo_hub import service
 rows=service.evidence_index()
 assert len(service.PAGES)==10 and len(rows)>=12
 assert all(row['sha256'] for row in rows[:10])   # the ten source files are in every checkout, a kit included
 assert all(row['version']==service.VERSION and row['acceptance_log'] for row in rows[:10])
 if service.LANE_RECORDS.exists():
  assert all(row['dossier_sha256'] for row in rows[:10])
  assert all(row['dossier_sha256']==hashlib.sha256((service.ROOT/row['dossier']).read_bytes()).hexdigest() for row in rows[:10])
  assert all(('focused acceptance group exit 0' in row['run_status']) if (service.ROOT/row['acceptance_log']).exists() else ('log unavailable' in row['run_status']) for row in rows[:10])   # a clean checkout has none of the lane's R3 logs: unavailable, never a pass
 else:
  # a kit has no internal/: the lane's records read as unavailable, they never crash the hub and never read as a pass
  assert all(row['dossier_sha256'] is None and 'log unavailable' in row['run_status'] for row in rows[:10])
 assert any('NOTRUN' in row['run_status'] for row in rows)

def test_disabled_pages_have_explicit_flag_instructions(monkeypatch):
 monkeypatch.delenv('COPILOT_FEATURE_LOCAL_AUTOPILOT',raising=False)   # the C-D composition turns every lane flag on
 from copilot.features.local_demo_hub import service
 pages=service.page_entries()
 assert all(page['flag']==f"COPILOT_FEATURE_{page['slug'].upper()}" for page in pages)
 assert any(not page['enabled'] for page in pages)

def test_hub_route_shows_disabled_instructions_and_shareable_sources(monkeypatch):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(__import__('os').environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 from _hosted import authenticated_client
 from fastapi.testclient import TestClient
 from copilot.app import create_app
 base='http://127.0.0.1'; client_args={'base_url':base,'client':('127.0.0.1',12345)}
 assert authenticated_client(create_app(),client=('127.0.0.1',12345)).get('/features/local_demo_hub').status_code==404
 monkeypatch.setenv('COPILOT_FEATURE_LOCAL_DEMO_HUB','true')
 response=authenticated_client(create_app(),client=('127.0.0.1',12345)).get('/features/local_demo_hub')
 assert response.status_code==200
 assert 'disabled' in response.text and 'COPILOT_FEATURE_EVIDENCE_DOSSIER=true' in response.text
 assert 'NOTRUN' in response.text
 assert 'C:\\Users\\' not in response.text

def _hub_and_walkthrough_text(monkeypatch, buyer_flag):
 from copilot import config
 monkeypatch.setattr(config,'load_env_file',lambda:None)
 for key in list(__import__('os').environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.setenv('COPILOT_FEATURE_LOCAL_DEMO_HUB','true')
 monkeypatch.setenv('COPILOT_FEATURE_JUDGE_WALKTHROUGH','true')
 if buyer_flag:monkeypatch.setenv('COPILOT_FEATURE_JUDGE_AS_BUYER','true')
 from _hosted import authenticated_client
 from copilot.app import create_app
 client=authenticated_client(create_app(),client=('127.0.0.1',12345))
 return client.get('/features/local_demo_hub').text,client.get('/features/judge_walkthrough').text

def test_judge_as_buyer_link_follows_its_flag_on_hub_and_walkthrough(monkeypatch):
 hub,walk=_hub_and_walkthrough_text(monkeypatch,True)
 assert 'href="/features/judge_as_buyer"' in hub and 'href="/features/judge_as_buyer"' in walk
 hub,walk=_hub_and_walkthrough_text(monkeypatch,False)
 assert '/features/judge_as_buyer' not in hub and '/features/judge_as_buyer' not in walk


def test_hub_opens_with_a_for_judges_block_and_no_invented_time(monkeypatch):
 monkeypatch.setenv('COPILOT_FEATURE_LOCAL_DEMO_HUB','true')
 from _hosted import authenticated_client
 from copilot.app import create_app
 text=authenticated_client(create_app(),client=('127.0.0.1',12345)).get('/features/local_demo_hub').text
 assert 'For judges' in text and 'tools/judge_check.py' in text and 'Check your setup' in text and 'Next step' in text
 assert text.index('For judges')<text.index('Case-aware destinations')
 assert ' minutes' not in text.split('Case-aware destinations')[0]


def test_for_judges_counts_match_the_guide_and_the_hash_sentence_is_in_the_hub(monkeypatch):
 monkeypatch.setenv('COPILOT_FEATURE_LOCAL_DEMO_HUB','true')
 from _hosted import authenticated_client
 from copilot.app import create_app
 from copilot.features.judge_as_buyer.service import GUIDE
 text=authenticated_client(create_app(),client=('127.0.0.1',12345)).get('/features/local_demo_hub').text
 assert len(GUIDE)==7 and 'seven steps' in text and 'tools/sandbox_seed.py' in text
 assert 'hash of the approved package is written into the evidence line' in text
