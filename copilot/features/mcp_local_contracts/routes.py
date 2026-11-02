import json
from fastapi import APIRouter,Request
from fastapi.responses import HTMLResponse
from copilot.features.mcp_local_contracts import service
from copilot.web.routes_disputes import templates
router=APIRouter()
@router.get('/features/mcp_local_contracts',response_class=HTMLResponse)
def page(request:Request):
 tools=[{'name':n,'schema':{'case_id':'registered local id','note':'string <=1000; prepare only'} if n=='prepare_local_note' else {}} for n in sorted(service.TOOLS)]
 return templates.TemplateResponse(request,'features/mcp_local_contracts/index.html',{'tools':json.dumps(tools,indent=2),'command':'.venv/Scripts/python.exe -m copilot.features.mcp_local_contracts.server'})
