from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot.features.judge_as_buyer import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, verify_csrf

router = APIRouter()
templates.env.globals["judge_as_buyer_marker"] = service.marker_for_norm


@router.get("/features/judge_as_buyer", response_class=HTMLResponse)
def page(request: Request):
    token = new_csrf_token()
    try:
        captures, error = service.load_capture_ids(), None
    except (OSError, ValueError) as exc:
        captures, error = [], str(exc)
    response = templates.TemplateResponse(request, "features/judge_as_buyer/index.html",
                                          {"guide": service.GUIDE, "captures": captures,
                                           "error": error, "csrf_token": token})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.post("/features/judge_as_buyer/seed-captures")
def save_seed_captures(request: Request, csrf_token: str = Form(default=""), capture_ids: str = Form(default="")):
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload this page and try again.", status_code=403)
    try:
        service.save_capture_ids(capture_ids)
    except (OSError, ValueError) as exc:
        token = new_csrf_token()
        response = templates.TemplateResponse(request, "features/judge_as_buyer/index.html",
                                              {"guide": service.GUIDE, "captures": [], "error": str(exc),
                                               "csrf_token": token}, status_code=400)
        set_csrf_cookie(response, token)
        return response
    return RedirectResponse("/features/judge_as_buyer", status_code=303)
