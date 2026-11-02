import json

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from copilot import config
from copilot.features.lessons_exchange import service
from copilot.features.trade_profiles.service import rules
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, verify_csrf

router = APIRouter()


def _render(request, *, error=None, status_code=200):
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/lessons_exchange/index.html", {
        "csrf_token": token, "error": error, "profiles": rules(),
        "imported": service.imported_reminders(), "lesson_count": service.seller_lesson_count(),
        "country": config.RULES_COUNTRY if config.RULES_COUNTRY in {"MX", "US"} else "unknown"})
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.get("/lessons-exchange", response_class=HTMLResponse)
def exchange_page(request: Request):
    return _render(request)


@router.post("/lessons-exchange/export")
def export_reminder(request: Request, csrf_token: str = Form(default=""),
                    amount_band: str = Form(default="unknown"), trade_profile: str = Form(default=""),
                    reported_topic: str = Form(default=""), preparation_reminder: str = Form(default="")):
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("This form expired. Reload this page before saving.", status_code=403)
    if service.seller_lesson_count() < 1:
        return _render(request, error="Record a seller lesson first; no export was created.", status_code=409)
    try:
        payload = service.export_payload(country=config.RULES_COUNTRY if config.RULES_COUNTRY in {"MX", "US"} else "unknown",
            amount_band=amount_band, trade_profile=trade_profile, reported_topic=reported_topic,
            preparation_reminder=preparation_reminder)
    except ValueError as exc:
        return _render(request, error=str(exc), status_code=400)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="proofie-seller-reminder.json"',
                             "Cache-Control": "no-store"})


@router.post("/lessons-exchange/import")
async def import_reminder(request: Request, csrf_token: str = Form(default=""),
                          reminder_file: UploadFile = File(...)):
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("This form expired. Reload this page before importing.", status_code=403)
    try:
        raw = await reminder_file.read(8193)
        service.import_payload(raw)
    except (OSError, ValueError) as exc:
        return _render(request, error=str(exc), status_code=400)
    finally:
        await reminder_file.close()
    return RedirectResponse("/lessons-exchange", status_code=303)
