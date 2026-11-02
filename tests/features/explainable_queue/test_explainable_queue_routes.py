import html, json, os, re
from fastapi.testclient import TestClient

def test_flag_off_on_and_human_readable_queue_post(monkeypatch):
 from copilot import config
 config.load_env_file=lambda:None
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.delenv('COPILOT_HOSTED_DEMO',raising=False)
 from copilot.app import create_app
 path='/features/explainable_queue'; base='http://127.0.0.1'
 assert TestClient(create_app(),base_url=base,client=('127.0.0.1',12345)).get(path).status_code==404
 monkeypatch.setenv('COPILOT_FEATURE_EXPLAINABLE_QUEUE','true')
 client=TestClient(create_app(),base_url=base,client=('127.0.0.1',12345));page=client.get(path)
 token=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
 from copilot.features.explainable_queue.service import EXAMPLE
 response=client.post(path,data={'csrf_token':html.unescape(token),'data':json.dumps(EXAMPLE)})
 assert response.status_code==200 and 'Prioritized work' in response.text and 'Recorded timeline' in response.text and 'CASE-NEAR' in response.text


def test_explainable_queue_alone_does_not_turn_on_the_dispute_page_timeline(monkeypatch):
 """S3: the PayPal timeline on the dispute page follows the `timeline` flag alone."""
 from datetime import datetime, timezone
 from pathlib import Path
 for key in list(os.environ):
  if key.startswith('COPILOT_FEATURE_'):monkeypatch.delenv(key,raising=False)
 monkeypatch.setenv('COPILOT_FEATURE_EXPLAINABLE_QUEUE','true')
 from copilot.web import routes_disputes
 raw=json.loads((Path(__file__).resolve().parents[3]/'pilot'/'fixtures'/'synthetic'/'dispute_PP-R-DEMO-0000001_waiting_for_seller.json').read_text(encoding='utf-8'))['body']
 now=datetime.now(timezone.utc)
 assert routes_disputes._paypal_timeline(raw,now,'PP-R-DEMO-0000001') is None
 monkeypatch.setenv('COPILOT_FEATURE_TIMELINE','true')
 assert routes_disputes._paypal_timeline(raw,now,'PP-R-DEMO-0000001') is not None
