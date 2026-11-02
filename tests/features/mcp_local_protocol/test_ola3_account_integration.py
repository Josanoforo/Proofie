import os, re
from datetime import datetime,timezone
from fastapi.testclient import TestClient


def test_account_consumes_existing_transaction_reads(monkeypatch,tmp_path):
    import httpx
    from copilot import config
    from copilot.app import create_app
    from copilot.core.disputes import PayPalError
    from copilot.features.account_cliff import service as cliff
    from copilot.features.explainable_queue import service as queue
    from copilot.web.routes_disputes import get_client
    def blocked(*a,**k): raise AssertionError('external HTTP forbidden')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',blocked); monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',blocked)
    monkeypatch.setattr(config,'load_env_file',lambda:None); monkeypatch.setattr(config,'_db_path_override',tmp_path/'db.sqlite'); monkeypatch.setenv('COPILOT_DATA_DIR',str(tmp_path/'data'))
    for k in list(os.environ):
        if k.startswith('COPILOT_FEATURE_'): monkeypatch.delenv(k,raising=False)
    for k in ('ACCOUNT_CLIFF','EXPLAINABLE_QUEUE'): monkeypatch.setenv('COPILOT_FEATURE_'+k,'true')
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return cls(2026,10,31,12,tzinfo=timezone.utc)
    monkeypatch.setattr(cliff,'datetime',Clock)
    def tx(ident,value,currency,code='T0006',status='S',day='2026-08-01T10:00:00Z'):
        return {'transaction_info':{'transaction_id':ident,'transaction_event_code':code,'transaction_status':status,'transaction_initiation_date':day,'transaction_amount':{'value':value,'currency_code':currency}}}
    rows=[tx('USD','12.34','USD'),tx('JPY','250','JPY'),tx('MXN','12.99','MXN'),tx('DUP','2.00','USD'),tx('DUP','2.00','USD'),tx('OTHER','1.00','USD',code='T1100'),tx('BAD','1.001','USD')]
    dispute={'dispute_id':'PP-R-FAK-0000001','status':'UNDER_REVIEW','dispute_life_cycle_stage':'INQUIRY','reason':'UNAUTHORISED','create_time':'2026-08-15T10:00:00Z','dispute_amount':{'currency_code':'USD','value':'12.00'},'links':[]}
    class Fake:
        def __init__(self): self.calls=[]; self.partial=False
        def call(self,method,path,op,**kw):
            assert method=='GET'; self.calls.append(op)
            if op=='list_disputes': return {'items':[dispute],'links':[]}
            if op=='account_cliff_search':
                n=self.calls.count(op)
                if self.partial and n==2: raise PayPalError(403,'NOT_AUTHORIZED','synthetic',{})
                return {'transaction_details':rows if n==1 else [],'total_pages':1}
            raise AssertionError(op)
    fake=Fake(); original=queue.account_exposure_context; seen=[]; outputs=[]
    def observe(*args,**kw):
        seen.append(kw); result=original(*args,**kw); outputs.append(result); return result
    monkeypatch.setattr(queue,'account_exposure_context',observe)
    app=create_app(); app.dependency_overrides[get_client]=lambda:fake; client=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345))
    page=client.get('/account'); assert page.status_code==200 and '12.34 USD' in page.text and '250 JPY' in page.text and '12.99 MXN' in page.text
    assert len(seen)==1 and list(seen[0]['transaction_rows'])==rows and seen[0]['transactions_complete'] is True
    movements=outputs[0]['completed_sales']
    assert movements['duplicate_transaction_rows']==1 and movements['unclassified_event_rows']==1 and movements['incomplete_transaction_rows']==1
    amounts={item['currency']:item for item in movements['by_currency']}
    assert amounts['USD']['amount_minor']==1234 and amounts['JPY']['amount_minor']==250 and amounts['MXN']['amount_minor']==1299
    assert amounts['USD']['by_date'][0]['date_utc']=='2026-08-01' and outputs[0]['sales_count']==6
    first=list(fake.calls); assert first.count('list_disputes')==1
    fake.calls.clear(); monkeypatch.setenv('COPILOT_FEATURE_EXPLAINABLE_QUEUE','false'); off=client.get('/account')
    assert off.status_code==200 and 'Recorded completed-sale amounts' not in off.text and fake.calls==first and len(seen)==1
    for label in ('Fee now', 'Fee if high volume', 'Dispute rate'):
        before_fee=re.search(r'<tr><th scope="row">'+re.escape(label)+r'</th><td>(.*?)</td></tr>',page.text,re.S).group(1)
        off_fee=re.search(r'<tr><th scope="row">'+re.escape(label)+r'</th><td>(.*?)</td></tr>',off.text,re.S).group(1)
        assert before_fee==off_fee
    monkeypatch.setenv('COPILOT_FEATURE_EXPLAINABLE_QUEUE','true'); fake.calls.clear(); fake.partial=True
    partial=client.get('/account'); assert partial.status_code==200 and 'Partial Transaction Search read' in partial.text
    assert seen[-1]['transactions_complete'] is False and list(seen[-1]['transaction_rows'])==rows
    assert outputs[-1]['sales_count'] is None and outputs[-1]['completed_sales']['fetch_complete'] is False
