from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.features.fulfilment import store
from copilot.features.fulfilment.tracker_request import validate_capture_id
from copilot.features.package_insert.service import build_insert, selected_physical_lessons
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, verify_csrf

router = APIRouter()


def _profile():
    if not config.feature_enabled("trade_profiles"):
        return None
    from copilot.features.trade_profiles import service
    return service.current_profile()


def _imported_reminders():
    if not config.feature_enabled("lessons_exchange"):
        return []
    from copilot.features.lessons_exchange import service
    return service.imported_reminders()


def _seller_lessons():
    if not (config.feature_enabled("closure_summary") and config.feature_enabled("sale_lesson")):
        return []
    from copilot.features.sale_lesson import service
    return service.lessons_for_preparation("physical")


def _check(capture_id, token):
    if validate_capture_id(capture_id):
        return None
    check = store.get_check(capture_id, token)
    if not check:
        return None
    trackers = check.get("body", {}).get("trackers") or []
    return trackers[0] if len(trackers) == 1 and isinstance(trackers[0], dict) else None


@router.get("/fulfilment/{capture_id}/check/{token}/package-insert", response_class=HTMLResponse)
def insert_form(request: Request, capture_id: str, token: str):
    tracker = _check(capture_id, token)
    if tracker is None:
        return HTMLResponse("This shipment check expired or is unavailable.", status_code=404)
    csrf = new_csrf_token()
    response = templates.TemplateResponse(request, "features/package_insert/form.html",
        {"capture_id": capture_id, "token": token, "tracker": tracker, "csrf_token": csrf,
         "profile": _profile(), "imported_reminders": _imported_reminders(),
         "seller_lessons": _seller_lessons(), "selected_lesson_ids": []})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, csrf)
    return response


@router.post("/fulfilment/{capture_id}/check/{token}/package-insert", response_class=HTMLResponse)
def render_insert(request: Request, capture_id: str, token: str,
                  item_description: str = Form(default=""), seller_contact: str = Form(default=""),
                  selected_lesson_ids: list[str] = Form(default=[]),
                  csrf_token: str = Form(default="")):
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload this page and try again.", status_code=403)
    tracker = _check(capture_id, token)
    if tracker is None:
        return HTMLResponse("This shipment check expired or is unavailable.", status_code=404)
    try:
        available = _seller_lessons()
        selected_lessons = selected_physical_lessons(available, selected_lesson_ids)
        result = build_insert(tracking_number=tracker.get("tracking_number", ""),
                              carrier=tracker.get("carrier", ""), item_description=item_description,
                              seller_contact=seller_contact, selected_lessons=selected_lessons)
        result["profile"] = _profile()
    except ValueError as exc:
        csrf = new_csrf_token()
        response = templates.TemplateResponse(request, "features/package_insert/form.html",
            {"capture_id": capture_id, "token": token, "tracker": tracker, "csrf_token": csrf, "error": str(exc),
             "item_description": item_description, "seller_contact": seller_contact,
             "profile": _profile(), "imported_reminders": _imported_reminders(),
             "seller_lessons": _seller_lessons(), "selected_lesson_ids": selected_lesson_ids}, status_code=400)
        response.headers["Cache-Control"] = "no-store"
        set_csrf_cookie(response, csrf)
        return response
    response = templates.TemplateResponse(request, "features/package_insert/print.html",
        {"capture_id": capture_id, "result": result})
    response.headers["Cache-Control"] = "no-store"
    return response
