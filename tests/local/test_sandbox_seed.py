"""Synthetic seed lifecycle only: fake client, fake transport, no credentials, no network."""
import ast
import copy
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from tools import sandbox_seed as seed


@pytest.fixture
def outside_checkout(tmp_path, monkeypatch):
    """Keep private-state guards real while pytest's basetemp may live under this checkout."""
    monkeypatch.setattr(seed, "checkout_root", lambda: tmp_path / "synthetic-checkout")

ORDER='DEMOORDER000000001'
ORDER2='DEMOORDER000000002'
CAPTURE='DEMOCAP'+'0'*9+'1'
CAPTURE2='DEMOCAP'+'0'*9+'2'
DISPUTE='PP-R-DEMO-SEED001'
SANDBOX_API='https://'+'api-m.sandbox.'+'paypal.com'


class Missing(RuntimeError):
    status=404


class ListRefused(RuntimeError):
    def __init__(self,status):
        super().__init__('synthetic');self.status=status


class Fake:
    list_items=None;list_status=None;list_after=0

    def __init__(self, *, approved=True, captured=False, capture_unknown=False,
                 tracker_unknown=False, dispute_missing=False, reason=None, status=None, wrong_capture=False,
                 approved2=True, captured2=False, capture2_unknown=False, dispute_capture=CAPTURE2):
        self.base_url=SANDBOX_API
        self.calls=[];self.approved=approved;self.captured=captured
        self.approved2=approved2;self.captured2=captured2;self.capture2_unknown=capture2_unknown
        self.capture_unknown=capture_unknown;self.tracker_unknown=tracker_unknown
        self.tracker=None;self.dispute_missing=dispute_missing;self.amounts=[]
        self.reason=reason or 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED'
        self.status=status or 'WAITING_FOR_SELLER_RESPONSE';self.wrong_capture=wrong_capture
        self.dispute_capture=dispute_capture

    def order(self, which):
        approved,captured,capture=(self.approved,self.captured,CAPTURE) if which==1 else (self.approved2,self.captured2,CAPTURE2)
        oid=ORDER if which==1 else ORDER2
        return {'id':oid,'status':'COMPLETED' if captured else 'APPROVED' if approved else 'CREATED',
                'links':[{'rel':'approve','href':'https://'+'www.sandbox.'+'paypal.com/checkoutnow?token='+oid}],
                'purchase_units':[{'payments':{'captures':[{'id':capture,'status':'COMPLETED'}] if captured else []}}]}

    def call(self, method,path,op,**kwargs):
        self.calls.append((method,path,op,kwargs))
        assert path.startswith('/') and '://' not in path
        if method=='POST':
            assert kwargs.get('request_id')
            if path==seed.ORDER_PATH:
                body=kwargs['json_body']
                value=body['purchase_units'][0]['amount']['value'];self.amounts.append(value)
                assert body['purchase_units'][0]['shipping']['address']['country_code']=='MX'
                assert body['payment_source']['paypal']['experience_context']['brand_name']=='Proofie (DEMO)'
                return {'id':ORDER if value=='25.00' else ORDER2}
            if path.endswith('/capture'):
                if path==seed.ORDER_PATH+'/'+ORDER2+'/capture':
                    if self.capture2_unknown:raise TimeoutError('synthetic')
                    self.captured2=True;return {}
                assert path==seed.ORDER_PATH+'/'+ORDER+'/capture'
                if self.capture_unknown:raise TimeoutError('synthetic')
                self.captured=True;return {}
            assert path==seed.TRACKER_PATH
            if self.tracker_unknown:raise TimeoutError('synthetic')
            self.tracker=kwargs['json_body']['trackers'][0];return {}
        if path==seed.ORDER_PATH+'/'+ORDER:
            return self.order(1)
        if path==seed.ORDER_PATH+'/'+ORDER2:
            return self.order(2)
        if path.startswith(seed.TRACKER_PATH+'?'):
            assert 'transaction_id='+CAPTURE in path
            return {'trackers':[self.tracker] if self.tracker else []}
        if path.startswith(seed.LIST_PATH+'?'):
            self.list_calls=getattr(self,'list_calls',0)+1
            assert path==seed.LIST_PATH+'?disputed_transaction_id='+self.dispute_capture
            if self.list_status:raise ListRefused(self.list_status)
            if self.list_items is None or self.list_calls<=self.list_after:return {'items':[]}
            return {'items':self.list_items}
        assert path==seed.DISPUTE_PATH+DISPUTE
        if self.dispute_missing:raise Missing()
        return {'dispute_id':DISPUTE,'reason':self.reason,'status':self.status,
                'disputed_transactions':[{'seller_transaction_id':'OTHER-CAPTURE' if self.wrong_capture else self.dispute_capture}],
                'links':[{'rel':'provide_evidence','method':'POST',
                          'href':str(self.base_url)+seed.DISPUTE_PATH+DISPUTE+'/provide-evidence'}]}


def run(fake,state,**kwargs):
    return seed.seed(fake,state,dispute_id=DISPUTE,attempts=2,interval=0,sleep=lambda _:None,
                     emit=lambda _:None,**kwargs)


def posts(fake):return [c for c in fake.calls if c[0]=='POST']


@pytest.mark.parametrize('args,sale', [([], 2), (['--one-sale'], 1), (['--dispute-sale', '1'], 1)])
def test_rc4_seed_plan_names_the_selected_dispute_sale(args,sale,capsys):
    assert seed.main(['--dry-run', *args], client_factory=lambda:pytest.fail('client'),
                     ask=lambda _:pytest.fail('prompt')) == 0
    assert f'the buyer opens Item not received on sale {sale};' in capsys.readouterr().out


def test_rc4_default_dispute_matches_untracked_sale_two(tmp_path):
    class SaleTwo(Fake):
        def call(self,method,path,op,**kwargs):
            result=super().call(method,path,op,**kwargs)
            if path==seed.DISPUTE_PATH+DISPUTE:
                result['disputed_transactions']=[{'seller_transaction_id':CAPTURE2}]
            return result
    fake=SaleTwo();state=seed.Checkpoint(tmp_path/'state')
    assert run(fake,state)==DISPUTE
    assert state.data['ready'] is True
    assert len(posts(fake))==5 and fake.tracker['transaction_id']==CAPTURE
    assert all(c[0]=='GET' for c in fake.calls if c[1].startswith(seed.DISPUTE_PATH))
    messages=[]
    resumed=SaleTwo(captured=True,captured2=True);resumed.tracker=fake.tracker
    assert seed.seed(resumed,state,dispute_id=DISPUTE,emit=messages.append)==DISPUTE
    assert not posts(resumed)
    assert any('SECOND payment (USD 15.00, order '+ORDER2 in message for message in messages)


def test_rc4_tracked_sale_dispute_requires_explicit_selection(tmp_path):
    fake=Fake(dispute_capture=CAPTURE)
    assert run(fake,seed.Checkpoint(tmp_path/'state'),dispute_sale=1)==DISPUTE
    assert len(posts(fake))==5


def test_rc4_default_refuses_tracked_sale_dispute(tmp_path):
    fake=Fake(dispute_capture=CAPTURE)
    state=seed.Checkpoint(tmp_path/'state')
    with pytest.raises(seed.Stop,match='does not match this capture'):
        run(fake,state)
    assert state.data['ready'] is False
    assert all(c[0]=='GET' for c in fake.calls if c[1].startswith(seed.DISPUTE_PATH))


def test_rc4_one_sale_cannot_select_missing_second_sale(tmp_path):
    fake=Fake();state=seed.Checkpoint(tmp_path/'state')
    with pytest.raises(ValueError):run(fake,state,one_sale=True,dispute_sale=2)
    assert not fake.calls and not state.path.exists()
    with pytest.raises(SystemExit) as stop:
        seed.main(['--dry-run','--one-sale','--dispute-sale','2'],
                  client_factory=lambda:pytest.fail('client'),ask=lambda _:pytest.fail('prompt'))
    assert stop.value.code==2


def test_happy_path_readbacks_and_five_writes(tmp_path):
    state=seed.Checkpoint(tmp_path/'state.json');fake=Fake()
    assert run(fake,state)==DISPUTE
    assert [c[1] for c in posts(fake)]==[seed.ORDER_PATH,seed.ORDER_PATH+'/'+ORDER+'/capture',seed.TRACKER_PATH,
                                         seed.ORDER_PATH,seed.ORDER_PATH+'/'+ORDER2+'/capture']
    assert fake.amounts==['25.00','15.00']
    for i,c in enumerate(fake.calls):
        if c[0]=='POST':assert fake.calls[i+1][0]=='GET'
    assert state.data['ready'] is True and state.data['order2']==ORDER2 and state.data['capture2']==CAPTURE2
    assert fake.tracker['transaction_id']==CAPTURE  # the tracker is only ever on sale 1
    assert all(c[0]=='GET' for c in fake.calls if c[1].startswith(seed.DISPUTE_PATH))


def test_buyer_timeout_never_captures(tmp_path):
    fake=Fake(approved=False)
    with pytest.raises(seed.Stop,match='approval timed out'):run(fake,seed.Checkpoint(tmp_path/'state'))
    assert [c[1] for c in posts(fake)]==[seed.ORDER_PATH]


def test_uncertain_capture_resume_never_reposts(tmp_path):
    path=tmp_path/'state';fake=Fake(capture_unknown=True)
    with pytest.raises(seed.Stop,match='Capture unconfirmed'):run(fake,seed.Checkpoint(path))
    resumed=Fake()
    with pytest.raises(seed.Stop,match='no second capture POST'):run(resumed,seed.Checkpoint(path),order_id=ORDER)
    assert not posts(resumed)
    assert len([c for c in posts(fake) if c[1].endswith('/capture')])==1


def test_uncertain_tracker_resume_never_reposts(tmp_path):
    path=tmp_path/'state';fake=Fake(tracker_unknown=True)
    with pytest.raises(seed.Stop,match='Tracker unconfirmed'):run(fake,seed.Checkpoint(path))
    resumed=Fake(captured=True)
    with pytest.raises(seed.Stop,match='no second tracker POST'):run(resumed,seed.Checkpoint(path),order_id=ORDER)
    assert not posts(resumed)


def test_dispute_timeout_clear_and_no_dispute_write(tmp_path):
    fake=Fake(dispute_missing=True)
    with pytest.raises(seed.Stop,match='Dispute did not become ready'):run(fake,seed.Checkpoint(tmp_path/'state'))
    assert len([c for c in fake.calls if c[1].startswith(seed.DISPUTE_PATH)])==2
    assert len(posts(fake))==5


@pytest.mark.parametrize('options,message',[
    ({'reason':'UNAUTHORISED'},'reason is not'),
    ({'status':'UNDER_REVIEW'},'did not become ready'),
    ({'wrong_capture':True},'does not match'),
])
def test_wrong_dispute_not_ready(tmp_path,options,message):
    fake=Fake(**options)
    with pytest.raises(seed.Stop,match=message):run(fake,seed.Checkpoint(tmp_path/'state'))


def test_resume_completed_order_no_create_or_capture(tmp_path):
    fake=Fake(captured=True,dispute_capture=CAPTURE)
    assert run(fake,seed.Checkpoint(tmp_path/'state'),order_id=ORDER,one_sale=True)==DISPUTE
    assert [c[1] for c in posts(fake)]==[seed.TRACKER_PATH]


def test_resume_ready_is_read_only(tmp_path):
    path=tmp_path/'state';fake=Fake();run(fake,seed.Checkpoint(path))
    resumed=Fake(captured=True,captured2=True);resumed.tracker=fake.tracker
    assert run(resumed,seed.Checkpoint(path),order_id=ORDER)==DISPUTE
    assert not posts(resumed)


def test_live_origin_refused_before_any_call(tmp_path):
    fake=Fake();fake.base_url='https://'+'api-m.'+'paypal.com'
    with pytest.raises(seed.Stop,match='sandbox origin'):run(fake,seed.Checkpoint(tmp_path/'state'))
    assert not fake.calls


def test_dry_run_never_initializes_client_or_prompts():
    def forbidden():raise AssertionError('client constructed')
    assert seed.main(['--dry-run'],client_factory=forbidden,ask=lambda _:pytest.fail('prompt'))==0


def test_create_unknown_cannot_create_again(tmp_path):
    state=seed.Checkpoint(tmp_path/'state');state.save(create_attempted=True)
    fake=Fake()
    with pytest.raises(seed.Stop,match='Create outcome unknown'):run(fake,state)
    assert not fake.calls


WRITE_KEYS={'create':('create_attempted','create_request'),'capture':('capture_attempted','capture_request'),
            'tracker':('tracker_attempted','tracker_request'),'create2':('create2_attempted','create2_request'),
            'capture2':('capture2_attempted','capture2_request')}


def observe_checkpoints(fake,path):
    seen=[];original=fake.call
    def observed(method,p,op,**kwargs):
        if method=='POST':
            if p==seed.ORDER_PATH:name='create' if not any(n=='create' for n in seen) else 'create2'
            elif p.endswith('/capture'):name='capture2' if ORDER2 in p else 'capture'
            else:name='tracker'
            disk=seed.Checkpoint(path).data
            attempted,request=WRITE_KEYS[name]
            assert disk[attempted] and disk[request]==kwargs['request_id'], name
            seen.append(name)
        return original(method,p,op,**kwargs)
    fake.call=observed
    return seen


def test_checkpoint_precedes_each_write(tmp_path):
    path=tmp_path/'state';fake=Fake();seen=observe_checkpoints(fake,path)
    assert run(fake,seed.Checkpoint(path))==DISPUTE
    assert seen==['create','capture','tracker','create2','capture2']


def test_checkpoint_precedes_second_sale_writes(tmp_path):
    path=tmp_path/'state';fake=Fake();seen=observe_checkpoints(fake,path)
    run(fake,seed.Checkpoint(path))
    assert {'create2','capture2'}<=set(seen)
    disk=seed.Checkpoint(path).data
    assert disk['create2_request'] and disk['capture2_request'] and disk['create2_request']!=disk['capture2_request']


def test_checkpoint_is_loaded_only_after_exclusive_lock(tmp_path,monkeypatch,outside_checkout):
    monkeypatch.setenv('PROOFIE_EVIDENCE_DIR',str(tmp_path/'original-evidence'))
    state_path=tmp_path/'state';original=seed.Checkpoint
    def checked(path):
        assert path.with_name(path.name+'.lock').exists(), 'checkpoint read before lock'
        return original(path)
    monkeypatch.setattr(seed,'Checkpoint',checked)
    result=seed.main(['--state',str(state_path),'--evidence-dir',str(tmp_path/'evidence'),
                      '--dispute',DISPUTE,'--attempts','2','--interval','0'],
                     ask=lambda _:'SANDBOX',client_factory=Fake)
    assert result==0
    assert not (tmp_path/'state.lock').exists()


def test_unsafe_provide_link_cannot_claim_readiness():
    dispute=Fake().call('GET',seed.DISPUTE_PATH+DISPUTE,'fake')
    dispute['links'][0]['href']='https://unsafe.invalid/provide-evidence'
    assert not seed.assess(CAPTURE2,dispute)


# ---- second sale -------------------------------------------------------------------------------------

def test_second_sale_uncertain_capture_resume_never_reposts(tmp_path):
    path=tmp_path/'state';fake=Fake(capture2_unknown=True)
    with pytest.raises(seed.Stop,match='Capture unconfirmed'):run(fake,seed.Checkpoint(path))
    assert len([c for c in posts(fake) if c[1]==seed.ORDER_PATH+'/'+ORDER2+'/capture'])==1
    resumed=Fake(captured=True);resumed.tracker=fake.tracker
    with pytest.raises(seed.Stop,match='no second capture POST'):run(resumed,seed.Checkpoint(path),order_id=ORDER)
    assert not posts(resumed)


def test_second_sale_resume_after_capture_confirmed_no_create_or_capture(tmp_path):
    path=tmp_path/'state';fake=Fake(capture2_unknown=True)
    with pytest.raises(seed.Stop):run(fake,seed.Checkpoint(path))
    resumed=Fake(captured=True,captured2=True);resumed.tracker=fake.tracker
    assert run(resumed,seed.Checkpoint(path),order_id=ORDER)==DISPUTE
    assert not posts(resumed)
    assert seed.Checkpoint(path).data['capture2']==CAPTURE2


def test_second_sale_create_unknown_cannot_create_again(tmp_path):
    path=tmp_path/'state';fake=Fake(captured=True)
    state=seed.Checkpoint(path);state.save(order=ORDER,capture=CAPTURE,tracker_confirmed=True,create2_attempted=True)
    fake.tracker={'transaction_id':CAPTURE,'tracking_number':'x'};state.save(tracking='x')
    with pytest.raises(seed.Stop,match='Create outcome unknown'):run(fake,state)
    assert not posts(fake)


def test_second_sale_has_no_tracker_and_uses_second_amount(tmp_path):
    fake=Fake();run(fake,seed.Checkpoint(tmp_path/'state'),second_amount_cents=2000)
    assert fake.amounts==['25.00','20.00']
    assert [c[3]['json_body']['trackers'][0]['transaction_id'] for c in posts(fake) if c[1]==seed.TRACKER_PATH]==[CAPTURE]


def test_one_sale_skips_second_sale_and_still_checks_dispute(tmp_path):
    state=seed.Checkpoint(tmp_path/'state');fake=Fake(dispute_capture=CAPTURE)
    assert run(fake,state,one_sale=True)==DISPUTE
    assert [c[1] for c in posts(fake)]==[seed.ORDER_PATH,seed.ORDER_PATH+'/'+ORDER+'/capture',seed.TRACKER_PATH]
    assert not any(ORDER2 in c[1] for c in fake.calls)
    assert 'order2' not in state.data and state.data['ready'] is True
    assert any(c[1]==seed.DISPUTE_PATH+DISPUTE for c in fake.calls)


def test_main_one_sale_and_second_amount_flags(tmp_path,monkeypatch,outside_checkout):
    made=[]
    def factory():
        made.append(Fake(dispute_capture=CAPTURE if not made else CAPTURE2));return made[-1]
    base=['--state',str(tmp_path/'s1'),'--evidence-dir',str(tmp_path/'e'),'--dispute',DISPUTE,'--attempts','2','--interval','0']
    assert seed.main(base+['--one-sale'],ask=lambda _:'SANDBOX',client_factory=factory)==0
    assert len(posts(made[0]))==3
    base[1]=str(tmp_path/'s2')
    assert seed.main(base+['--second-amount-cents','1234'],ask=lambda _:'SANDBOX',client_factory=factory)==0
    assert made[1].amounts==['25.00','12.34']


def test_cancel_before_client_and_state(tmp_path,monkeypatch,outside_checkout):
    def forbidden():raise AssertionError('client constructed')
    assert seed.main(['--attempts','1','--interval','0','--state',str(tmp_path/'s'),'--evidence-dir',str(tmp_path/'e')],
                     ask=lambda _:'no',client_factory=forbidden)==2
    assert not (tmp_path/'s').exists() and not (tmp_path/'s.lock').exists()


# ---- credentials -------------------------------------------------------------------------------------

def test_env_file_takes_only_the_two_keys(tmp_path):
    env=tmp_path/'seed-creds.env'
    env.write_text('# comment\nOTHER_SECRET=do-not-read\nexport PAYPAL_CLIENT_ID=id-synthetic\n'
                   'PAYPAL_CLIENT_SECRET="secret-synthetic"\nNOISE\n',encoding='utf-8')
    assert seed.read_env_file(env)=={'PAYPAL_CLIENT_ID':'id-synthetic','PAYPAL_CLIENT_SECRET':'secret-synthetic'}
    assert seed.load_credentials(env,environ={})==('id-synthetic','secret-synthetic')


def test_environment_wins_and_file_is_not_needed(tmp_path):
    assert seed.load_credentials(tmp_path/'absent.env',
                                 environ={'PAYPAL_CLIENT_ID':'a','PAYPAL_CLIENT_SECRET':'b'})==('a','b')


def test_default_env_file_is_in_current_folder(tmp_path,monkeypatch):
    (tmp_path/'.env').write_text('PAYPAL_CLIENT_ID=cwd-id\nPAYPAL_CLIENT_SECRET=cwd-secret\n',encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    assert seed.load_credentials(None,environ={})==('cwd-id','cwd-secret')


def test_missing_key_stops_before_any_client_or_network(tmp_path,monkeypatch,capsys,outside_checkout):
    env=tmp_path/'partial.env'
    env.write_text('PAYPAL_CLIENT_ID=only-the-id\n',encoding='utf-8')
    monkeypatch.delenv('PAYPAL_CLIENT_ID',raising=False);monkeypatch.delenv('PAYPAL_CLIENT_SECRET',raising=False)
    monkeypatch.chdir(tmp_path)
    def no_network(*a,**k):raise AssertionError('network used')
    monkeypatch.setattr(urllib.request,'urlopen',no_network)
    monkeypatch.setattr(seed,'SeedClient',no_network)
    with pytest.raises(seed.Stop,match='PAYPAL_CLIENT_SECRET'):seed.load_credentials(env,environ={})
    result=seed.main(['--state',str(tmp_path/'s.json'),'--evidence-dir',str(tmp_path/'ev'),'--env-file',str(env)],
                     ask=lambda _:'SANDBOX')
    assert result==3
    assert 'credentials' in capsys.readouterr().out.lower()
    assert not (tmp_path/'s.json').exists() and not (tmp_path/'ev').exists() and not (tmp_path/'s.json.lock').exists()


def test_no_credentials_anywhere_exits_3_without_network(tmp_path,monkeypatch,capsys,outside_checkout):
    monkeypatch.delenv('PAYPAL_CLIENT_ID',raising=False);monkeypatch.delenv('PAYPAL_CLIENT_SECRET',raising=False)
    monkeypatch.chdir(tmp_path)
    def no_network(*a,**k):raise AssertionError('network used')
    monkeypatch.setattr(urllib.request,'urlopen',no_network)
    assert seed.main(['--attempts','1','--interval','0','--state',str(tmp_path/'s.json'),'--evidence-dir',str(tmp_path/'ev')],ask=lambda _:'SANDBOX')==3
    assert 'PAYPAL_CLIENT_ID' in capsys.readouterr().out


# ---- inlined client ----------------------------------------------------------------------------------

class Resp:
    def __init__(self, payload, status=200, headers=None):
        self.status=status;self.headers=headers or {'PayPal-Debug-Id':'dbg-synthetic'}
        self._raw=json.dumps(payload).encode()
    def read(self):return self._raw
    def close(self):pass


class Net:
    """Replacement for urlopen: records every request, answers by path."""
    def __init__(self, answers):
        self.answers=answers;self.requests=[]
    def __call__(self, request, timeout=None):
        self.requests.append((request,timeout))
        path=request.full_url[len(SANDBOX_API):]
        answer=self.answers[path]
        if isinstance(answer,BaseException):raise answer
        return answer


TOKEN_ANSWER={'access_token':'tok-synthetic','expires_in':3600,'scope':'secret scope list'}


def client(tmp_path,monkeypatch,answers):
    net=Net({'/v1/oauth2/token':Resp(TOKEN_ANSWER),**answers})
    monkeypatch.setattr(urllib.request,'urlopen',net)
    return seed.SeedClient('id-synthetic','secret-synthetic',tmp_path/'evidence'),net


def header(request,name):
    return {k.lower():v for k,v in request.header_items()}.get(name.lower())


def test_client_headers_request_id_token_cache_and_json(tmp_path,monkeypatch):
    path=seed.ORDER_PATH+'/'+ORDER
    c,net=client(tmp_path,monkeypatch,{seed.ORDER_PATH:Resp({'id':ORDER,'status':'CREATED'}),path:Resp({'id':ORDER,'status':'APPROVED'})})
    assert c.base_url==SANDBOX_API
    created=c.call('POST',seed.ORDER_PATH,'seed_create',json_body={'intent':'CAPTURE'},request_id='rid-synthetic-1')
    read=c.call('GET',path,'seed_order_readback')
    assert created['id']==ORDER and read['status']=='APPROVED'
    token_req,order_post,order_get=[r for r,_ in net.requests]
    assert token_req.full_url.endswith('/v1/oauth2/token') and token_req.get_method()=='POST'
    assert header(token_req,'Authorization').startswith('Basic ') and b'grant_type=client_credentials' in token_req.data
    assert len([r for r,_ in net.requests if r.full_url.endswith('/v1/oauth2/token')])==1  # cached
    assert header(order_post,'PayPal-Request-Id')=='rid-synthetic-1'
    assert header(order_get,'PayPal-Request-Id') is None and order_get.data is None
    for r in (order_post,order_get):
        assert header(r,'Authorization')=='Bearer tok-synthetic' and header(r,'Prefer')=='return=representation'
        assert header(r,'Content-Type')=='application/json' and header(r,'Accept')=='application/json'
    assert json.loads(order_post.data)=={'intent':'CAPTURE'}
    assert all(t==30 for _,t in net.requests)


def test_client_refreshes_token_after_expiry(tmp_path,monkeypatch):
    c,net=client(tmp_path,monkeypatch,{'/x':Resp({})})
    c.call('GET','/x','op')
    c._token_expiry=0.0
    c.call('GET','/x','op')
    assert len([r for r,_ in net.requests if r.full_url.endswith('/v1/oauth2/token')])==2


def test_client_maps_404_without_body_text(tmp_path,monkeypatch):
    path=seed.DISPUTE_PATH+DISPUTE
    err=urllib.error.HTTPError(SANDBOX_API+path,404,'Not Found',{'PayPal-Debug-Id':'dbg-404'},
                               io.BytesIO(b'{"name":"RESOURCE_NOT_FOUND","message":"private-detail-xyz"}'))
    c,_=client(tmp_path,monkeypatch,{path:err})
    with pytest.raises(seed.SeedError) as caught:c.call('GET',path,'seed_dispute_read')
    assert caught.value.status==404 and caught.value.issue=='RESOURCE_NOT_FOUND' and caught.value.debug_id=='dbg-404'
    assert 'private-detail-xyz' not in str(caught.value)
    assert 'private-detail-xyz' not in (tmp_path/'evidence'/'sandbox_seed.jsonl').read_text(encoding='utf-8')


def test_client_http_error_returned_as_response_object_also_maps(tmp_path,monkeypatch):
    c,_=client(tmp_path,monkeypatch,{'/x':Resp({'name':'UNPROCESSABLE_ENTITY','details':[{'issue':'ORDER_NOT_APPROVED'}]},status=422)})
    with pytest.raises(seed.SeedError) as caught:c.call('POST','/x','op',json_body={},request_id='r')
    assert caught.value.status==422 and caught.value.issue=='ORDER_NOT_APPROVED'


def test_client_token_failure_is_a_seed_error(tmp_path,monkeypatch):
    net=Net({'/v1/oauth2/token':Resp({'error':'invalid_client','error_description':'private-text'},status=401)})
    monkeypatch.setattr(urllib.request,'urlopen',net)
    c=seed.SeedClient('id-synthetic','secret-synthetic',tmp_path/'evidence')
    with pytest.raises(seed.SeedError) as caught:c.call('GET','/x','op')
    assert caught.value.status==401 and 'private-text' not in str(caught.value)
    assert [r.full_url for r,_ in net.requests]==[SANDBOX_API+'/v1/oauth2/token']


def test_client_transport_error_logged_by_class_only_and_reraised(tmp_path,monkeypatch):
    c,_=client(tmp_path,monkeypatch,{'/x':urllib.error.URLError('private-reason-xyz')})
    with pytest.raises(urllib.error.URLError):c.call('POST','/x','op',json_body={},request_id='rid-t')
    text=(tmp_path/'evidence'/'sandbox_seed.jsonl').read_text(encoding='utf-8')
    last=json.loads(text.splitlines()[-1])
    assert last['transport_error']=='URLError' and last['request_id']=='rid-t' and last['status'] is None
    assert 'private-reason-xyz' not in text


def test_client_refuses_non_path_targets(tmp_path,monkeypatch):
    c,net=client(tmp_path,monkeypatch,{})
    with pytest.raises(seed.Stop):c.call('GET','https://elsewhere.invalid/x','op')
    assert not net.requests


def test_evidence_line_of_order_read_has_ids_and_no_body(tmp_path,monkeypatch):
    path=seed.ORDER_PATH+'/'+ORDER
    order={'id':ORDER,'status':'COMPLETED','payer':{'email_address':'buyer@example.com','name':{'given_name':'Zed'}},
           'links':[{'rel':'self','href':SANDBOX_API+path},{'rel':'approve','href':'https://www.sandbox.paypal.com/checkoutnow?token=TOK-SECRET'}],
           'purchase_units':[{'shipping':{'address':{'address_line_1':seed.SHIPPING_ADDRESS['address_line_1']}},
                              'payments':{'captures':[{'id':CAPTURE,'status':'COMPLETED'}]}}]}
    c,_=client(tmp_path,monkeypatch,{path:Resp(order)})
    c.call('GET',path,'seed_order_readback')
    text=(tmp_path/'evidence'/'sandbox_seed.jsonl').read_text(encoding='utf-8')
    records=[json.loads(l) for l in text.splitlines()]
    token_record,record=records
    assert set(token_record)-{'ts','op','method','path'}=={'status','ms','expires_in'} and token_record['expires_in']==3600
    assert record['order_id']==ORDER and record['order_status']=='COMPLETED'
    assert record['capture_id']==CAPTURE and record['capture_status']=='COMPLETED' and record['link_rels']==['self','approve']
    assert record['op']=='seed_order_readback' and record['method']=='GET' and record['status']==200 and record['path']==path
    assert {'ts','ms','request_id','debug_id','issue'}<=set(record)
    assert 'body' not in record
    for forbidden in ('Calle Falsa','Authorization','Bearer','tok-synthetic','buyer@example.com','Zed','TOK-SECRET','secret scope','secret-synthetic'):
        assert forbidden not in text, forbidden


def test_evidence_line_of_dispute_and_tracker_reads(tmp_path,monkeypatch):
    tracker_path=seed.TRACKER_PATH+'?transaction_id='+CAPTURE
    dispute_path=seed.DISPUTE_PATH+DISPUTE
    c,_=client(tmp_path,monkeypatch,{
        tracker_path:Resp({'trackers':[{'transaction_id':CAPTURE,'tracking_number':'DEMOTRACK1','status':'SHIPPED','carrier_name_other':'x'}]}),
        dispute_path:Resp({'dispute_id':DISPUTE,'reason':'MERCHANDISE_OR_SERVICE_NOT_RECEIVED','status':'WAITING_FOR_SELLER_RESPONSE',
                           'messages':[{'content':'private message text'}],'links':[{'rel':'provide_evidence'}]})})
    c.call('GET',tracker_path,'seed_tracker_readback');c.call('GET',dispute_path,'seed_dispute_read')
    text=(tmp_path/'evidence'/'sandbox_seed.jsonl').read_text(encoding='utf-8')
    _,tracker,dispute=[json.loads(l) for l in text.splitlines()]
    assert (tracker['tracker_transaction_id'],tracker['tracking_number'],tracker['tracker_status'])==(CAPTURE,'DEMOTRACK1','SHIPPED')
    assert (dispute['dispute_id'],dispute['dispute_reason'],dispute['dispute_status'])==(DISPUTE,'MERCHANDISE_OR_SERVICE_NOT_RECEIVED','WAITING_FOR_SELLER_RESPONSE')
    assert dispute['link_rels']==['provide_evidence'] and 'private message text' not in text


def test_seed_runs_through_the_real_client_with_faked_transport(tmp_path,monkeypatch):
    """Whole happy path through SeedClient: only urlopen is replaced; every URL must be the sandbox API."""
    state={'o1':'CREATED','o2':'CREATED','tracker':None}
    def urlopen(request,timeout=None):
        url=request.full_url;assert url.startswith(SANDBOX_API+'/');path=url[len(SANDBOX_API):]
        body=json.loads(request.data) if request.data and header(request,'Content-Type')=='application/json' else None
        if path=='/v1/oauth2/token':return Resp(TOKEN_ANSWER)
        if request.get_method()=='POST' and path==seed.ORDER_PATH:
            value=body['purchase_units'][0]['amount']['value']
            return Resp({'id':ORDER if value=='25.00' else ORDER2})
        if request.get_method()=='POST' and path.endswith('/capture'):
            state['o1' if ORDER+'/capture' in path else 'o2']='COMPLETED';return Resp({})
        if request.get_method()=='POST' and path==seed.TRACKER_PATH:
            state['tracker']=body['trackers'][0];return Resp({})
        if path.startswith(seed.TRACKER_PATH+'?'):return Resp({'trackers':[state['tracker']] if state['tracker'] else []})
        if path.startswith(seed.ORDER_PATH+'/'):
            which=1 if path.endswith(ORDER) else 2
            fake=Fake(approved=True,captured=state['o1']=='COMPLETED',captured2=state['o2']=='COMPLETED')
            return Resp(fake.order(which))
        return Resp(Fake().call('GET',path,'x'))
    monkeypatch.setattr(urllib.request,'urlopen',urlopen)
    c=seed.SeedClient('id-synthetic','secret-synthetic',tmp_path/'evidence')
    st=seed.Checkpoint(tmp_path/'state.json')
    assert run(c,st)==DISPUTE
    lines=[json.loads(l) for l in (tmp_path/'evidence'/'sandbox_seed.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [l['op'] for l in lines if l.get('method')=='POST']==['seed_create','seed_capture','seed_tracker_add','seed_create','seed_capture']
    assert all('body' not in l for l in lines)


# ---- order body, header, imports, help ----------------------------------------------------------------

def test_order_body_matches_the_helper_except_the_brand():
    from tools import sandbox_orders
    mine=seed.build_order_body(2500,'label-1');theirs=sandbox_orders.build_order_body(2500,'label-1')
    assert mine['purchase_units'][0]['amount']=={'currency_code':'USD','value':'25.00'}
    assert mine['payment_source']['paypal']['experience_context']['brand_name']=='Proofie (DEMO)'
    expected=copy.deepcopy(theirs);expected['payment_source']['paypal']['experience_context']['brand_name']='Proofie (DEMO)'
    assert mine==expected
    assert seed.cents_to_str(1500)=='15.00' and seed.cents_to_str(5)=='0.05'
    with pytest.raises(ValueError):seed.cents_to_str(0)


def test_file_is_standard_library_only_and_sandbox_hosts_only():
    source=Path(seed.__file__).read_text(encoding='utf-8')
    allowed=set(sys.stdlib_module_names)|{'__future__'}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node,ast.Import):roots={a.name.split('.')[0] for a in node.names}
        elif isinstance(node,ast.ImportFrom):roots={(node.module or '').split('.')[0]}
        else:continue
        assert roots<=allowed, roots
    for forbidden in ('pilot','httpx','dotenv','tools.'):
        assert 'import '+forbidden not in source and 'from '+forbidden not in source
    import re
    hosts=set(re.findall(r'[A-Za-z0-9.-]*paypal\.com',source))
    assert hosts<={'api-m.sandbox.paypal.com','www.sandbox.paypal.com','sandbox.paypal.com'}, hosts


def test_header_and_help_text(capsys):
    source=Path(seed.__file__).read_text(encoding='utf-8').splitlines()
    doc=ast.get_docstring(ast.parse('\n'.join(source))).splitlines()
    assert doc[0]=='Proofie sandbox seed — one file, standard library only (Python 3.10+).'
    assert doc[1]=='Copyright 2026 Jonatan J. Guadarrama G. Licensed under the Apache License, Version 2.0.'
    with pytest.raises(SystemExit) as stop:seed.main(['--help'])
    assert stop.value.code==0
    text=capsys.readouterr().out
    for needle in ('two sales','--one-sale','--env-file','--second-amount-cents','outside','--dry-run','SAME'):
        assert needle in text, needle


def test_dry_run_prints_plan_with_second_sale_and_touches_nothing(tmp_path,monkeypatch,capsys):
    def no_network(*a,**k):raise AssertionError('network used')
    monkeypatch.setattr(urllib.request,'urlopen',no_network)
    monkeypatch.chdir(tmp_path)
    assert seed.main(['--dry-run'],ask=lambda _:pytest.fail('prompt'))==0
    assert 'second sale' in capsys.readouterr().out
    assert seed.main(['--dry-run','--one-sale'],ask=lambda _:pytest.fail('prompt'))==0
    assert 'skipped' in capsys.readouterr().out
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('packaged', [True, False])
def test_checkout_root_does_not_depend_on_git_metadata(tmp_path, monkeypatch, packaged):
    folder = tmp_path / ('tools' if packaged else 'standalone')
    folder.mkdir()
    monkeypatch.setattr(seed, '__file__', str(folder / 'sandbox_seed.py'))
    assert seed.checkout_root() == (tmp_path if packaged else folder)


def test_state_inside_the_checkout_is_refused(capsys):
    root=Path(seed.__file__).resolve().parent.parent
    with pytest.raises(SystemExit):
        seed.main(['--state',str(root/'tmp-state.json'),'--evidence-dir',str(root/'tmp-ev')],ask=lambda _:pytest.fail('prompt'))
    assert 'outside the checkout' in capsys.readouterr().err


# ---- A pasted command line is not a dispute ID -----------------------------------------------------
NOT_AN_ID='That is not a dispute ID. Paste only the ID (for example PP-R-ABC-12345678), not the command line. Nothing was sent to the dispute.'
STOPPED=NOT_AN_ID+' The sales already created are kept in the state file; run again with the same --state to continue.'
PASTED='python tools/sandbox_seed.py --dispute '+DISPUTE+' --state private.json'


def test_a2_8_a_pasted_command_line_is_asked_again_and_the_run_continues(tmp_path,capsys):
    fake=Fake();state=seed.Checkpoint(tmp_path/'state');asked=[]
    def ask(prompt):
        asked.append(prompt)
        return PASTED if len(asked)==1 else DISPUTE
    got=seed.seed(fake,state,attempts=2,interval=0,sleep=lambda _:None,emit=lambda _:None,ask=ask)
    assert got==DISPUTE and len(asked)==2 and state.data['ready'] is True
    assert capsys.readouterr().out.count(NOT_AN_ID)==1
    assert len(posts(fake))==5 and not [c for c in posts(fake) if c[1].startswith(seed.DISPUTE_PATH)]


def test_a2_8_three_bad_answers_stop_with_exit_3_and_the_same_sentence(tmp_path,capsys,monkeypatch,outside_checkout):
    answers=iter(['SANDBOX',PASTED,'PP R spaced',PASTED+' again'])
    fake=Fake()
    code=seed.main(['--attempts','1','--interval','0','--state',str(tmp_path/'s.json'),'--evidence-dir',str(tmp_path/'ev')],
                   ask=lambda _:next(answers),client_factory=lambda:fake)
    out=capsys.readouterr().out
    assert code==3 and out.count(NOT_AN_ID)==3 and 'ValueError' not in out
    # After the sales exist, the stop says what is true: nothing went to the dispute, the sales are in the
    # state file, resume with the same --state; it never claims that nothing was sent at all
    assert out.count(STOPPED)==1 and out.rstrip().endswith(STOPPED) and 'Nothing was sent.' not in out
    assert len(posts(fake))==5 and not [c for c in posts(fake) if c[1].startswith(seed.DISPUTE_PATH)]
    saved=json.loads((tmp_path/'s.json').read_text(encoding='utf-8'))
    assert saved.get('order') and saved.get('capture') and 'dispute' not in saved
    with pytest.raises(StopIteration):next(answers)


def test_a2_8_other_input_errors_print_the_message_not_the_class_name(tmp_path,capsys,monkeypatch,outside_checkout):
    def refuse(*args,**kwargs):
        raise seed.InputError('amount must be a positive integer number of cents')
    monkeypatch.setattr(seed,'seed',refuse)
    code=seed.main(['--attempts','1','--interval','0','--state',str(tmp_path/'s.json'),'--evidence-dir',str(tmp_path/'ev')],
                   ask=lambda _:'SANDBOX',client_factory=lambda:Fake())
    out=capsys.readouterr().out
    assert code==3 and 'Stopped: amount must be a positive integer number of cents. Nothing was sent. Fix the input and run again.' in out
    assert 'ValueError' not in out and 'InputError' not in out


def test_a2_8_a_failure_that_is_not_an_input_error_keeps_its_class_name_and_never_says_nothing_was_sent(tmp_path,capsys,monkeypatch,outside_checkout):
    def refuse(*args,**kwargs):
        raise json.JSONDecodeError('x','y',0)
    monkeypatch.setattr(seed,'seed',refuse)
    code=seed.main(['--attempts','1','--interval','0','--state',str(tmp_path/'s.json'),'--evidence-dir',str(tmp_path/'ev')],
                   ask=lambda _:'SANDBOX',client_factory=lambda:Fake())
    out=capsys.readouterr().out
    assert code==3 and 'Nothing was sent' not in out and 'Stopped: JSONDecodeError' in out


def test_ss_5_nit_the_stop_after_one_sale_says_sale_not_sales(tmp_path,capsys,monkeypatch,outside_checkout):
    answers=iter(['SANDBOX',PASTED,'PP R spaced',PASTED+' again'])
    fake=Fake(dispute_capture=CAPTURE)
    code=seed.main(['--attempts','1','--interval','0','--state',str(tmp_path/'s.json'),'--evidence-dir',str(tmp_path/'ev'),'--one-sale'],
                   ask=lambda _:next(answers),client_factory=lambda:fake)
    out=capsys.readouterr().out
    one=NOT_AN_ID+' The sale already created is kept in the state file; run again with the same --state to continue.'
    assert code==3 and out.count(NOT_AN_ID)==3 and out.rstrip().endswith(one) and 'sales' not in out.split(NOT_AN_ID)[-1]
    assert len(posts(fake))==3 and not [c for c in posts(fake) if c[1].startswith(seed.DISPUTE_PATH)]


# ---- R1-3 / R1-4: private defaults and the dispute found by code -------------------------------------
def found(fake,state,tmp_path,**kw):
    return seed.seed(fake,state,attempts=3,interval=0,sleep=lambda _:None,emit=kw.pop('emit',lambda _:None),
                     ask=lambda _:pytest.fail('prompt'),**kw)


def test_r1_4_the_dispute_is_found_from_the_list_filtered_by_the_capture_and_never_asked(tmp_path):
    fake=Fake();fake.list_items=[{'dispute_id':DISPUTE,'reason':'MERCHANDISE_OR_SERVICE_NOT_RECEIVED','status':'WAITING_FOR_SELLER_RESPONSE'}]
    lines=[]
    assert found(fake,seed.Checkpoint(tmp_path/'s.json'),tmp_path,emit=lines.append)==DISPUTE
    assert any('Dispute found from capture '+CAPTURE2 in l and 'no paste needed' in l for l in lines)
    assert all(c[0]=='GET' for c in fake.calls if c[1].startswith(seed.LIST_PATH))      # reads only
    assert fake.calls and 'disputed_transaction_id='+CAPTURE2 in [c[1] for c in fake.calls if '?disputed' in c[1]][0]


def test_r1_4_it_keeps_polling_until_the_buyer_has_opened_the_dispute(tmp_path):
    fake=Fake();fake.list_after=2
    fake.list_items=[{'dispute_id':DISPUTE,'reason':'MERCHANDISE_OR_SERVICE_NOT_RECEIVED','status':'WAITING_FOR_SELLER_RESPONSE'}]
    assert found(fake,seed.Checkpoint(tmp_path/'s.json'),tmp_path)==DISPUTE
    assert fake.list_calls==3


def test_r1_4_the_not_received_dispute_waiting_for_the_seller_wins_among_several(tmp_path):
    fake=Fake();fake.list_items=[{'dispute_id':'PP-R-OTHER-0001','reason':'UNAUTHORISED','status':'WAITING_FOR_SELLER_RESPONSE'},
                                 {'dispute_id':'PP-R-OTHER-0002','reason':'MERCHANDISE_OR_SERVICE_NOT_RECEIVED','status':'RESOLVED'},
                                 {'dispute_id':DISPUTE,'reason':'MERCHANDISE_OR_SERVICE_NOT_RECEIVED','status':'WAITING_FOR_SELLER_RESPONSE'}]
    assert found(fake,seed.Checkpoint(tmp_path/'s.json'),tmp_path)==DISPUTE


@pytest.mark.parametrize('status',[400,404])
def test_r1_4_a_400_or_404_from_the_list_means_none_yet_then_the_paste_fallback_is_asked(tmp_path,status):
    fake=Fake();fake.list_status=status;asked=[]
    got=seed.seed(fake,seed.Checkpoint(tmp_path/'s.json'),attempts=2,interval=0,sleep=lambda _:None,emit=lambda _:None,
                  ask=lambda p:asked.append(p) or DISPUTE)
    assert got==DISPUTE and len(asked)==1 and fake.list_calls==2


def test_r1_4_any_other_list_failure_stops_the_run_without_asking(tmp_path):
    fake=Fake();fake.list_status=500
    with pytest.raises(ListRefused):
        seed.seed(fake,seed.Checkpoint(tmp_path/'s.json'),attempts=2,interval=0,sleep=lambda _:None,emit=lambda _:None,
                  ask=lambda _:pytest.fail('prompt'))


def test_r1_4_a_given_dispute_id_skips_the_list_entirely(tmp_path):
    fake=Fake()
    assert run(fake,seed.Checkpoint(tmp_path/'s.json'))==DISPUTE
    assert not any(c[1].startswith(seed.LIST_PATH+'?') for c in fake.calls)


def _git_root(tmp_path, ignore="data/\n"):
    import subprocess
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(ignore, encoding="utf-8")
    return repo


def test_r1_3_the_default_is_data_sandbox_seed_inside_the_checkout_and_git_ignores_it(tmp_path, monkeypatch):
    repo = _git_root(tmp_path)
    monkeypatch.setattr(seed, "checkout_root", lambda: repo)
    assert seed.default_private_dir() == repo / "data" / "sandbox-seed"
    assert seed.git_ignores(repo, repo / "data" / "sandbox-seed" / "state.json") is True
    assert seed.git_ignores(repo, repo / "data" / "sandbox-seed" / "evidence") is True
    real_root = Path(seed.__file__).resolve().parent.parent       # the real repository: data/ is ignored there too
    if seed.git_ignores(real_root, real_root) is None:             # a kit extracted from the ZIP: no repository to ask
        assert "data/" in (real_root / ".gitignore").read_text(encoding="utf-8-sig").splitlines()
    else:
        assert seed.git_ignores(real_root, real_root / "data" / "sandbox-seed" / "state.json") is True


def test_r1_3_a_target_git_would_track_is_refused_with_the_reason(tmp_path):
    repo = _git_root(tmp_path)
    message = seed.refuse_trackable(repo, repo / "state.json")
    assert message and "git would track it" in message and "outside the checkout" in message
    assert seed.refuse_trackable(repo, repo / "notes" / "ev") is not None
    assert seed.refuse_trackable(repo, repo / "data" / "x" / "state.json") is None                # ignored: fine
    assert seed.refuse_trackable(repo, tmp_path / "elsewhere" / "state.json") is None             # outside the checkout: fine


def test_r1_3_when_git_cannot_tell_only_the_default_folder_is_accepted(tmp_path, monkeypatch):
    repo = tmp_path / "plain"
    repo.mkdir()                                                    # not a git repository: git check-ignore answers 128
    default = seed.default_private_dir(repo)
    assert seed.git_ignores(repo, default / "state.json") is None
    assert seed.refuse_trackable(repo, default / "state.json", default) is None
    explicit = seed.refuse_trackable(repo, repo / "mine.json")
    assert explicit and "cannot say whether it is ignored" in explicit
    monkeypatch.setattr(seed, "git_ignores", lambda root, path: (_ for _ in ()).throw(AssertionError("outside paths need no git")))
    assert seed.refuse_trackable(repo, tmp_path / "away" / "state.json") is None


def test_r1_3_main_uses_the_checkout_defaults_and_an_explicit_flag_wins(tmp_path, monkeypatch, capsys):
    repo = _git_root(tmp_path)
    monkeypatch.setattr(seed, "checkout_root", lambda: repo)
    seen = []
    monkeypatch.setattr(seed, "load_credentials", lambda env_file=None: ("id", "secret"))
    monkeypatch.setattr(seed, "SeedClient", lambda cid, sec, evidence: seen.append(evidence) or Fake())
    monkeypatch.setattr(seed, "seed", lambda client, state, **kw: seen.append(state.path) or DISPUTE)
    assert seed.main([], ask=lambda _: "SANDBOX") == 0
    assert seen[0] == (repo / "data" / "sandbox-seed" / "evidence").resolve()
    assert seen[1] == (repo / "data" / "sandbox-seed" / "state.json").resolve()
    assert "defaults in the checkout, ignored by git" in capsys.readouterr().out
    seen.clear()
    assert seed.main(["--state", str(tmp_path / "mine.json"), "--evidence-dir", str(tmp_path / "ev")], ask=lambda _: "SANDBOX") == 0
    assert seen[1] == (tmp_path / "mine.json").resolve() and seen[0] == (tmp_path / "ev").resolve()


def test_r1_3_main_refuses_an_explicit_target_git_would_track(tmp_path, monkeypatch, capsys):
    repo = _git_root(tmp_path)
    monkeypatch.setattr(seed, "checkout_root", lambda: repo)
    with pytest.raises(SystemExit):
        seed.main(["--state", str(repo / "state.json"), "--evidence-dir", str(repo / "data" / "ev")],
                  ask=lambda _: pytest.fail("prompt"), client_factory=lambda: pytest.fail("client"))
    assert "git would track it" in capsys.readouterr().err


def test_r1_3_the_direct_link_is_printed_once_the_dispute_is_found(tmp_path):
    fake = Fake()
    fake.list_items = [{"dispute_id": DISPUTE, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "status": "WAITING_FOR_SELLER_RESPONSE"}]
    lines = []
    seed.seed(fake, seed.Checkpoint(tmp_path / "s.json"), attempts=3, interval=0, sleep=lambda _: None, emit=lines.append,
              ask=lambda _: pytest.fail("prompt"))
    assert any(l.endswith("http://127.0.0.1:8000/disputes/" + DISPUTE) for l in lines)
