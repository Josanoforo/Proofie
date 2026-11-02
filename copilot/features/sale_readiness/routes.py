import html
import json
from datetime import datetime,timezone
from fastapi import APIRouter,Depends,Form,Request
from fastapi.responses import HTMLResponse
from starlette.responses import RedirectResponse,Response
from urllib.parse import urlencode
from copilot import config
from copilot.db import repo
from copilot.features.sale_readiness import service
from copilot.features.fulfilment.tracker_request import validate_capture_id
from copilot.web.routes_disputes import csrf_expired_page,get_client,templates
from copilot.core.disputes import normalize
from copilot.web.security import new_csrf_token,set_csrf_cookie,validate_dispute_id,verify_csrf
router=APIRouter()
@router.get('/features/sale_readiness',response_class=HTMLResponse)
def page(request:Request,sample:str='physical'):
 k=sample if sample in service.EXAMPLES else 'physical';t=new_csrf_token();r=templates.TemplateResponse(request,'features/sale_readiness/index.html',{'raw':json.dumps(service.EXAMPLES[k],ensure_ascii=False,indent=2),'result':None,'error':None,'csrf':t,'examples':service.EXAMPLES});set_csrf_cookie(r,t);return r
@router.post('/features/sale_readiness',response_class=HTMLResponse)
def run(request:Request,data:str=Form(...),csrf_token:str=Form(default='')):
 if not verify_csrf(request,csrf_token):return Response('Reload this form.',403)
 try:result=service.build(json.loads(data));error=None
 except Exception as e:result=None;error=str(e)[:500]
 t=new_csrf_token();r=templates.TemplateResponse(request,'features/sale_readiness/index.html',{'raw':data[:25000],'result':result,'error':error,'csrf':t,'examples':service.EXAMPLES},status_code=400 if error else 200);set_csrf_cookie(r,t);return r


@router.post('/fulfilment/{capture_id}/readiness',response_class=HTMLResponse)
async def save_case_readiness(request:Request,capture_id:str,client=Depends(get_client)):
 form=await request.form()
 if not verify_csrf(request,str(form.get('csrf_token') or '')):return csrf_expired_page(request)
 dispute_id=str(form.get('dispute_id') or '')
 from_package=str(form.get('from_package') or '')
 if validate_dispute_id(dispute_id) or validate_capture_id(capture_id):
  return Response('The case or sale reference is invalid.',status_code=400)
 if from_package:
  if len(form.getlist('from_package')) != 1:return Response('Invalid package reference.',status_code=400)
  from copilot.features.fulfilment.routes import _readiness_package
  package,refusal=_readiness_package(request,dispute_id,from_package)
  if refusal is not None:return refusal
 try:
  raw=client.call('GET',f'/v1/customer/disputes/{dispute_id}','show_dispute')
  current=normalize(raw,datetime.now(timezone.utc))
  if current.get('dispute_id') != dispute_id or capture_id not in (current.get('seller_transaction_ids') or []):
   raise ValueError('This transaction is not linked to the current recorded case snapshot; seller terms were not saved.')
  price_cents=service.parse_price_cents(str(form.get('price_amount') or ''))
  documents=repo.list_documents(config.db_path(),dispute_id)
  doc_ids={str(row['id']) for row in documents}
  evidence_kinds={}
  allowed_kinds={kind for kinds in service.REQUIRED_EVIDENCE.values() for kind in kinds}
  for key,value in form.multi_items():
   if key.startswith('evidence_kind_'):
    doc_id=key.removeprefix('evidence_kind_')
    if doc_id not in doc_ids:raise ValueError('Evidence row does not belong to this case.')
    if value and value not in allowed_kinds:raise ValueError('Choose an evidence kind from this sale checklist.')
    if value:evidence_kinds[doc_id]=value
  terms={'kind':str(form.get('kind') or ''),'buyer_ref':str(form.get('buyer_ref') or ''),
         'description':str(form.get('description') or ''),'variant':str(form.get('variant') or ''),
         'price_cents':price_cents,'currency':str(form.get('currency') or ''),
         'expected_date':str(form.get('expected_date') or ''),'returns':str(form.get('returns') or ''),
         'contact':str(form.get('contact') or ''),'terms_version':str(form.get('terms_version') or ''),
         'acceptance_ref':str(form.get('acceptance_ref') or ''),'carrier':str(form.get('carrier') or ''),
         'tracking_number':str(form.get('tracking_number') or ''),
         'ship_to':{key:str(form.get('ship_to_'+key) or '') for key in ('name','street','city','region','postal_code','country')},
         'evidence_kinds':evidence_kinds}
  record={'schema_version':1,'dispute_id':dispute_id,'capture_id':capture_id,'terms':terms}
  service.case_view(dispute_id,capture_id,{},documents,[],record)
  service.save_case_terms(dispute_id,capture_id,terms)
 except (OSError,TypeError,ValueError) as exc:
  return HTMLResponse(f'<main><h1>Sale readiness not saved</h1><p role="alert">{html.escape(str(exc))}</p></main>',status_code=400)
 query=urlencode({'from_dispute':dispute_id, **({'from_package':from_package} if from_package else {})})
 return RedirectResponse(f'/fulfilment/{capture_id}?{query}#sale-readiness',status_code=303)
