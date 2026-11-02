"""Explicit C-C HTTP journeys; other suites' passive scenarios are separate."""
import sqlite3
import time
import pytest
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.features.active_approval import service
from copilot.web.routes_disputes import get_client
from _hosted import authenticated_client, set_test_csrf
from .conftest import FakeClient, DID
from .test_guarantees import SendFake, decisions, ready

CC = {'claims', 'ledger', 'package_print', 'carrier_required', 'account_cliff',
      'events', 'deadlines', 'triage', 'fulfilment', 'resolution', 'outcome', 'active_approval'}


@pytest.fixture
def cc_web(web_mode, monkeypatch):
    # This is an explicit C-C scenario, never an environment-wide flag reset.
    for name in CC:
        monkeypatch.setenv('COPILOT_FEATURE_'+name.upper(), 'true')
    for name in ('fulfilment_register','resolution_writes','outcome_appeal','webhooks_verify',
                 'webhooks','draft','delivery_facts','conformance'):
        monkeypatch.setenv('COPILOT_FEATURE_'+name.upper(), 'false')
    assert all(config.feature_enabled(name) for name in CC)
    fake=FakeClient();app=create_app();app.dependency_overrides[get_client]=lambda:fake
    db=config.db_path();package=repo.insert_package(db,DID,'human_edit',True,[])
    text='DEMO DATA\nTracking number: DEMO-FDX-001\nCarrier: FedEx\nStatus: En tránsito'
    from hashlib import sha256
    doc=repo.insert_document(db,DID,'demo.pdf','application/pdf',len(text),sha256(text.encode()).hexdigest(),text,True)
    repo.insert_claims(db,package,[{'evidence_index':0,'claim_index':0,'text':'Shipment is in transit.',
        'source_ids':[f'doc_{doc}'],'basis':'seller_statement'}])
    web=authenticated_client(app);review=ready(fake,db,package)
    sender=SendFake(fake,review);app.dependency_overrides[get_client]=lambda:sender
    return web,sender,fake,db,package,doc


def post(web,url,data=None):
    token='DEMO-GATE-CSRF';set_test_csrf(web,token)
    # /send answers 303 to the stored result page; follow it so the status is the outcome's own
    return web.post(url,data={'csrf_token':token,**(data or {})},follow_redirects=url.endswith('/send'))


def approve(web,sender,db,package):
    review=service.read(sender,db,DID,package)
    url=f'/disputes/{DID}/packages/{package}/active-approval'
    assert web.get(url).status_code==200
    response=post(web,url,{'review_sha256':review.review_sha256,
                         **{'claim-'+key:value for key,value in decisions(review).items()}})
    assert response.status_code==303,response.text
    assert repo.get_latest_valid_approval(db,package) is not None


def test_cc_without_receipt_cannot_approve_passively_or_send(cc_web):
    web,sender,_,db,package,_=cc_web
    prefix=f'/disputes/{DID}/packages/{package}'
    assert post(web,prefix+'/approve').status_code==409
    assert post(web,prefix+'/send').status_code==400
    assert sender.posts==0
    assert repo.get_latest_valid_approval(db,package) is None


def test_cc_explicit_http_review_authorizes_one_fake_send(cc_web):
    web,sender,_,db,package,_=cc_web
    approve(web,sender,db,package)
    response=post(web,f'/disputes/{DID}/packages/{package}/send')
    assert response.status_code==200,response.text
    assert sender.posts==1
    again=post(web,f'/disputes/{DID}/packages/{package}/send')
    assert again.status_code==409,again.text      # Package already received
    assert 'Nothing new was sent.' in again.text
    assert sender.posts==1
    assert all(config.feature_enabled(name) for name in CC)


def test_cc_source_changed_after_receipt_refuses_without_post(cc_web):
    web,sender,_,db,package,doc=cc_web
    approve(web,sender,db,package)
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?',('\nChanged source',doc))
    response=post(web,f'/disputes/{DID}/packages/{package}/send')
    assert response.status_code==409,response.text
    assert sender.posts==0


def test_cc_expired_reservation_refuses_before_post(cc_web,monkeypatch):
    web,sender,_,db,package,_=cc_web
    approve(web,sender,db,package)
    # A durable reservation older than the real default TTL cannot resume its POST.
    aid=repo.get_latest_valid_approval(db,package)['id']
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO active_send_reservations VALUES (?, 'expired', ?, 'old', 'old', 'reserved')",
                     (aid,time.time()-301))
    response=post(web,f'/disputes/{DID}/packages/{package}/send')
    assert response.status_code==409,response.text
    assert sender.posts==0
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM active_send_reservations').fetchone()[0]==1
