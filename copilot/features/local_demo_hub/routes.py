from fastapi import APIRouter,Request
from fastapi.responses import HTMLResponse
from copilot import config
from copilot.features.local_demo_hub import service
from copilot.web.routes_disputes import templates
from copilot.web.security import validate_dispute_id
router=APIRouter()
@router.get('/features/local_demo_hub',response_class=HTMLResponse)
def page(request:Request):
 case_id=request.query_params.get('case_id','')
 case_data=None;error=None;case_outputs=[];package_id=None
 if case_id:
  problem=validate_dispute_id(case_id)
  if problem:error=problem;case_id=''
  else:
   case_data=service.case_context(case_id)
   package_id=case_data['latest_package_id']
   if config.feature_enabled('local_autopilot'):
    from copilot.features.local_autopilot.routes import _store_path
    from copilot.features.local_autopilot.service import load_inbox
    for row in load_inbox(_store_path()):
     if row.get('case_id')==case_id:case_outputs.append({'kind':'local policy simulation','status':row.get('decision'),'reference':row.get('logical_id'),'detail':row.get('result')})
   if config.feature_enabled('mcp_local_contracts'):
    from copilot.features.mcp_local_contracts.service import notes_for_case
    notes=notes_for_case(case_id)
    for note in notes:case_outputs.append({'kind':'local MCP note','status':note.get('status'),'reference':note.get('created_at'),'detail':'Prepared note stored for this case; text omitted from hub index.'})
 pages=service.page_entries(case_id or None,package_id)
 return templates.TemplateResponse(request,'features/local_demo_hub/index.html',
  {'pages':pages,'claims':service.evidence_index(),'storyboard':service.STORYBOARD,'version':service.VERSION,
   'case':case_data,'case_outputs':case_outputs,'error':error,
   'judge_as_buyer_enabled':config.feature_enabled('judge_as_buyer')})
