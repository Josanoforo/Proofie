import json
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response
from copilot import config
from copilot.features.evidence_dossier import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, verify_csrf

router=APIRouter()
@router.get("/features/evidence_dossier", response_class=HTMLResponse)
def page(request:Request, sample:str="physical"):
    key=sample if sample in service.EXAMPLES else "physical"
    token=new_csrf_token()
    response=templates.TemplateResponse(request,"features/evidence_dossier/index.html",{"raw":json.dumps(service.EXAMPLES[key],ensure_ascii=False,indent=2),"report":None,"error":None,"samples":service.EXAMPLES,"selected":key,"csrf":token})
    response.headers["Cache-Control"]="no-store"
    set_csrf_cookie(response,token)
    return response
@router.post("/features/evidence_dossier", response_class=HTMLResponse)
def build(request:Request, data:str=Form(...), csrf_token:str=Form(default="")):
    if not verify_csrf(request,csrf_token):
        return Response("Form expired. Reload the page.",status_code=403)
    result=None; error=None
    try: result=service.assemble(json.loads(data))
    except (ValueError,TypeError) as exc: error=str(exc)
    token=new_csrf_token()
    response=templates.TemplateResponse(request,"features/evidence_dossier/index.html",{"raw":data[:50000],"report":result,"error":error,"samples":service.EXAMPLES,"selected":"","csrf":token},status_code=400 if error else 200)
    response.headers["Cache-Control"]="no-store"
    set_csrf_cookie(response,token)
    return response
