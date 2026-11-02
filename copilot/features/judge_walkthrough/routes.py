from fastapi import APIRouter,Request
from fastapi.responses import HTMLResponse
from copilot import config
from copilot.features.judge_walkthrough import service
from copilot.web.routes_disputes import templates
from copilot.web.security import validate_dispute_id
router=APIRouter()
@router.get('/features/judge_walkthrough',response_class=HTMLResponse)
def page(request:Request):
 case_id=request.query_params.get('case_id','')
 if case_id and validate_dispute_id(case_id):case_id=''
 case_url=f'/disputes/{case_id}' if case_id else None
 return templates.TemplateResponse(request,'features/judge_walkthrough/index.html',
  {'manifest':service.MANIFEST,'forms':service.FORMS,'provenance':service.SYNTHETIC_PROVENANCE,'synthetic_identity':service.synthetic_identity(),'route':'/disputes',
   'case_id':case_id,'case_url':case_url,
   'judge_as_buyer_enabled':config.feature_enabled('judge_as_buyer')})
