from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot.features.trade_profiles import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, verify_csrf

router = APIRouter()


@router.get("/trade-profile", response_class=HTMLResponse)
def profile_page(request: Request):
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/trade_profiles/profile.html",
        {"profiles": service.rules(), "current": service.current_profile(), "csrf_token": token})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.post("/trade-profile")
def save_profile(request: Request, profile: str = Form(default=""), csrf_token: str = Form(default="")):
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("This form expired. Reload this page before saving.", status_code=403)
    try:
        service.select_profile(profile)
    except (OSError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    return RedirectResponse("/trade-profile", status_code=303)
