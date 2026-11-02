import json
from pathlib import Path
from copilot import config
from fastapi import APIRouter,Depends,Form,Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response
from copilot.features.negotiation_drafts import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token,rejected_dispute_id,set_csrf_cookie,verify_csrf
from copilot.web.routes_disputes import csrf_expired_page,get_client
router=APIRouter()
HISTORY_PATH=Path(__file__).resolve().parents[3]/'data'/'mejoras10'/'03'/'history.jsonl'   # default location; follows COPILOT_DATA_DIR when set
def _history_path():
 return HISTORY_PATH if config.data_dir()==config.DATA_DIR else config.data_dir()/'mejoras10'/'03'/'history.jsonl'
@router.get('/features/negotiation_drafts',response_class=HTMLResponse)
def page(request:Request):
 t=new_csrf_token();r=templates.TemplateResponse(request,'features/negotiation_drafts/index.html',{'raw':json.dumps(service.EXAMPLE,ensure_ascii=False,indent=2),'result':None,'error':None,'csrf':t});set_csrf_cookie(r,t);return r
@router.post('/features/negotiation_drafts',response_class=HTMLResponse)
def run(request:Request,data:str=Form(...),csrf_token:str=Form(default='')):
 if not verify_csrf(request,csrf_token):return Response('Reload this form.',403)
 try:result=service.build(json.loads(data),_history_path());error=None
 except Exception as e:result=None;error=str(e)[:500]
 t=new_csrf_token();r=templates.TemplateResponse(request,'features/negotiation_drafts/index.html',{'raw':data[:30000],'result':result,'error':error,'csrf':t},status_code=400 if error else 200);set_csrf_cookie(r,t);return r


@router.post('/disputes/{dispute_id}/resolution/draft',response_class=HTMLResponse)
def prepare_case_draft(request:Request,dispute_id:str,csrf_token:str=Form(default=''),kind:str=Form(default=''),
                       locale:str=Form(default='en'),recipient:str=Form(default=''),amount:str=Form(default=''),
                       conditions:str=Form(default=''),autopilot_logical_id:str=Form(default=''),client=Depends(get_client)):
 if not verify_csrf(request,csrf_token):return csrf_expired_page(request)
 bad=rejected_dispute_id(dispute_id)
 if bad:return bad
 from copilot.features.resolution import routes as resolution_routes
 from copilot.features.resolution import service as resolution_service
 state=resolution_service.page_state(client,dispute_id)
 error=None;result=None
 try:
  if state.norm is None:raise ValueError('Proofie could not read this case; no draft was prepared.')
  allowed=set()
  if state.actions.offer:allowed.add('offer')
  if state.actions.message:allowed.add('message')
  source_simulation=None
  if autopilot_logical_id:
   from copilot.features.local_autopilot.routes import handoff_for_case
   handoff=handoff_for_case(dispute_id,autopilot_logical_id,state.norm,allowed)
   if handoff is None:raise ValueError('The imported simulation is stale, conflicted or no longer allowed for this case.')
   expected_kind='partial_offer' if handoff['action']=='offer' else 'followup'
   if kind != expected_kind:raise ValueError('The draft action must match the current action recorded by the imported simulation.')
   source_simulation={key:handoff[key] for key in ('logical_id','action','amount_cents','currency','policy_version','policy_sha256','state_sha256','dossier_sha256')}
  cents=0
  if kind=='partial_offer':
   cents=resolution_routes.parse_amount_cents(amount,state.norm.get('currency'))
   if cents is None:raise ValueError('Enter a valid amount in the exact currency shown for this case.')
   if cents>state.norm.get('amount_cents',-1):raise ValueError('The offer cannot exceed the recorded dispute amount.')
   if source_simulation and cents != source_simulation['amount_cents']:
    raise ValueError('The amount differs from the imported simulation. Run a new practice check before importing the changed amount.')
  result=service.build_case_draft(state.norm,allowed,kind=kind,locale=locale,recipient=recipient,
                                  amount_cents=cents,conditions=conditions,
                                  history_path=service.case_history_path(dispute_id),
                                  source_simulation=source_simulation)
 except (ValueError,OSError) as exc:
  error=str(exc)[:500]
 history=[]
 if result:
  history=result.get('history_versions',[])
 elif state.norm:
  try:
   history=service.read_case_history(dispute_id)
  except (OSError,ValueError) as exc:
   error=error or f'Local proposal history could not be read safely: {str(exc)[:400]}'
 return resolution_routes._render(request,dispute_id,state,409 if error else 200,
                                  local_draft=result['proposals'][0] if result else None,
                                  local_draft_error=error,
                                  autopilot_handoff_error=error if autopilot_logical_id else '',
                                  case_draft_history=history)
