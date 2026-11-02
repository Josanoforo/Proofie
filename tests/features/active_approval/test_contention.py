"""Actual cross-process POST reservation versus HTTP edit routes; fake transport only."""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from copilot.db import repo
from copilot.features.active_approval import service
from _hosted import authenticated_client, set_test_csrf
from .test_guarantees import DID, decisions, ready


CHILD_SEND = r'''
import copy,json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests/features/active_approval'))
from conftest import FakeClient
from copilot.core.send_service import send_package
from copilot.features.active_approval import service
import httpx
from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json
delays=[]
original_reconcile=send_service.reconcile_after_send
def reconcile_with_clock(*args,**kwargs):
 kwargs['sleep']=delays.append
 return original_reconcile(*args,**kwargs)
send_service.reconcile_after_send=reconcile_with_clock
db,pid,fail=sys.argv[1:];pid=int(pid)
fake=FakeClient()
fake.raw['evidences']=[{'evidence_type':'PROOF_OF_FULFILLMENT','source':'REQUESTED_FROM_SELLER'}]
review=service.read(fake,db,'PP-R-DEMO-0000001',pid)
class Sender:
 def __init__(self):self.body=copy.deepcopy(fake.raw);self.posts=0
 def call(self,method,path,op,**kwargs):
  if method=='POST':
   self.posts+=1
   print('POST_RESERVED',flush=True)
   assert sys.stdin.readline().strip()=='release'
   if fail=='error':raise httpx.ReadTimeout('Synthetic POST interruption')
   self.body['evidences']=[{**e,'source':'SUBMITTED_BY_SELLER'} for e in review.request_input['evidences']]
   return {}
  if 'transactions' in path or 'trackers' in path:return fake.call(method,path,op,**kwargs)
  assert method=='GET'
  return copy.deepcopy(self.body)
sender=Sender();result=send_package(sender,db,'PP-R-DEMO-0000001',pid)
registry=SQLiteAttemptStore(db)
try:state=registry.read('PP-R-DEMO-0000001',sha256_json(review.request_input))['state']
finally:registry.close()
print(json.dumps({'posts':sender.posts,'sent':result.sent,'http_status':result.http_status,'state':state,'delays':delays}),flush=True)
'''


@pytest.fixture
def mode_prepared(web_mode, prepared):
    return prepared


@pytest.mark.parametrize("edit", ["document", "manual"])
@pytest.mark.parametrize("completion", ["normal", "error"])
def test_busy_edit_during_real_process_post_then_manual_retry(mode_prepared, edit, completion):
    web, fake, db, package, doc = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    web = authenticated_client(web.app)
    set_test_csrf(web, "CONTENTION-CSRF")
    if edit == "document":
        path = f"/disputes/{DID}/documents/{doc}/exclude"
        data = {"csrf_token": "CONTENTION-CSRF"}
        action = "exclude_document"
    else:
        path = f"/disputes/{DID}/manual-fields"
        data = {"csrf_token": "CONTENTION-CSRF", "package_id": str(package), "tracking_number": "DEMO-MANUAL-RETRY-1"}
        action = "set_manual_fields"
    child = subprocess.Popen([sys.executable, "-c", CHILD_SEND, str(db), str(package), completion],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            signal = pool.submit(child.stdout.readline).result(timeout=20)
        assert signal.strip() == "POST_RESERVED", signal
        response = web.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303
        if edit == "document":
            assert repo.get_document(db, doc)["excluded"] is True
        else:
            assert repo.get_manual_fields(db, DID)["tracking_number"]["value"] == "DEMO-MANUAL-RETRY-1"
        assert len([row for row in repo.list_audit(db, DID) if row["action"] == action]) == 1
    finally:
        output, errors = child.communicate("release\n", timeout=30)
    assert child.returncode == 0, errors
    outcome = json.loads(output.strip())
    # When PayPal's read-back confirms the send, that is the result (200, sent) and the change is a note;
    # an unconfirmed send keeps the honest 202 review-required result.
    confirmed = completion == "normal"
    assert outcome["posts"] == 1 and bool(outcome["sent"]) is confirmed
    assert outcome["http_status"] == (200 if confirmed else 202)
    # POST completion now reconciles even when sources changed: PayPal's own
    # matching GET confirms the attempted snapshot, while approval stays cleared.
    assert outcome["state"] == ("confirmed" if completion == "normal" else "sent_unconfirmed")
    assert repo.get_latest_valid_approval(db, package) is None
    assert any(row["action"] == "active_send_sources_changed" for row in repo.list_audit(db, DID))
    assert len([row for row in repo.list_audit(db, DID) if row["action"] == action]) == 1
