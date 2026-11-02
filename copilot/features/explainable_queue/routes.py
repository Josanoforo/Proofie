import json
from fastapi import APIRouter,Form,Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response
from copilot.features.explainable_queue import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token,set_csrf_cookie,verify_csrf
from copilot import config
router=APIRouter()
@router.get('/features/explainable_queue',response_class=HTMLResponse)
def page(request:Request):
 t=new_csrf_token(); r=templates.TemplateResponse(request,'features/explainable_queue/index.html',{'raw':json.dumps(service.EXAMPLE,indent=2),'result':None,'error':None,'csrf':t});set_csrf_cookie(r,t);return r
@router.post('/features/explainable_queue',response_class=HTMLResponse)
def run(request:Request,data:str=Form(...),csrf_token:str=Form(default='')):
 if not verify_csrf(request,csrf_token):return Response('Reload this form.',403)
 try: result=service.build(json.loads(data));error=None
 except Exception as e:result=None;error=str(e)[:500]
 t=new_csrf_token();r=templates.TemplateResponse(request,'features/explainable_queue/index.html',{'raw':data[:50000],'result':result,'error':error,'csrf':t},status_code=400 if error else 200);set_csrf_cookie(r,t);return r


@router.post('/features/explainable_queue/prepare-selected', response_class=HTMLResponse)
def prepare_selected(request: Request, observation_token: str = Form(default=''),
                     case_ids: list[str] = Form(default=[]), csrf_token: str = Form(default='')):
    if not all(config.feature_enabled(flag) for flag in ('explainable_queue', 'local_autopilot', 'local_preparation', 'selected_preparation')):
        return Response('Selected preparation is unavailable.', status_code=404)
    if not verify_csrf(request, csrf_token):
        return Response('Reload the inbox.', status_code=403)
    from copilot.features.explainable_queue.batch import InvalidSelection, run_selected
    from copilot.features.local_autopilot import observations, preparation
    try:
        result = run_selected(config.db_path(), observation_token, case_ids,
                              get_observation=observations.get, prepare=preparation.prepare_known_case)
        error, status = None, 200
    except InvalidSelection as exc:
        result, error, status = None, str(exc), 400
    token = new_csrf_token()
    response = templates.TemplateResponse(request, 'features/explainable_queue/batch_results.html',
        {'batch': result, 'error': error, 'observation_token': observation_token, 'csrf_token': token}, status_code=status)
    set_csrf_cookie(response, token)
    response.headers['Cache-Control'] = 'no-store'
    return response
