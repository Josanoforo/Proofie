import json
from fastapi import APIRouter,Form,Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response
from copilot.features.evaluation_workbench import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token,set_csrf_cookie,verify_csrf
router=APIRouter()
@router.get('/features/evaluation_workbench',response_class=HTMLResponse)
def page(request:Request):
 t=new_csrf_token();r=templates.TemplateResponse(request,'features/evaluation_workbench/index.html',{'raw':json.dumps(service.EXAMPLE,indent=2),'result':None,'error':None,'csrf':t,'historic':service.HISTORIC,'last':service.last_run()});set_csrf_cookie(r,t);return r
@router.post('/features/evaluation_workbench',response_class=HTMLResponse)
def run(request:Request,data:str=Form(...),csrf_token:str=Form(default='')):
 if not verify_csrf(request,csrf_token):return Response('Reload this form.',403)
 try:result=service.validate(json.loads(data));error=None
 except Exception as e:result=None;error=str(e)[:500]
 t=new_csrf_token();r=templates.TemplateResponse(request,'features/evaluation_workbench/index.html',{'raw':data[:40000],'result':result,'error':error,'csrf':t,'historic':service.HISTORIC,'last':service.last_run()},status_code=400 if error else 200);set_csrf_cookie(r,t);return r
