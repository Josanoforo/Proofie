from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.features.outcome.read import read_outcome
from copilot.features.sale_lesson import service
from copilot.web.routes_disputes import get_client, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()


def _validate_link(client, dispute_id: str, capture_id: str):
    view = read_outcome(client, dispute_id)
    norm = view.norm if isinstance(view.norm, dict) else {}
    ids = norm.get("seller_transaction_ids") or []
    if capture_id not in ids:
        return view, "The sale reference was not returned for this case by PayPal."
    if not view.decided or not view.outcome_code or view.outcome_code == "NONE":
        return view, "PayPal has not recorded a closed outcome for this case yet."
    return view, ""


def _render(request, view, dispute_id, capture_id, record=None, error="", status=200,
            sale_type_value="", lesson_value="", form_disabled=False):
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/sale_lesson/close.html",
        {"view": view, "dispute_id": dispute_id, "capture_id": capture_id, "record": record,
         "error": error, "csrf_token": token, "kind_labels": (("physical", "Physical goods"),
         ("digital", "Digital goods"), ("service", "Service")),
         "sale_type_value": sale_type_value, "lesson_value": lesson_value,
         "form_disabled": form_disabled}, status_code=status)
    set_csrf_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/disputes/{dispute_id}/sale-lesson", response_class=HTMLResponse)
def close_page(request: Request, dispute_id: str, capture_id: str, client=Depends(get_client)):
    if rejected_dispute_id(dispute_id):
        return HTMLResponse("Invalid case ID", status_code=400)
    view, error = _validate_link(client, dispute_id, capture_id)
    record = next((r for r in service.list_lessons() if r.get("dispute_id") == dispute_id), None)
    return _render(request, view, dispute_id, capture_id, record, error, 409 if error else 200)


@router.post("/disputes/{dispute_id}/sale-lesson", response_class=HTMLResponse)
def save_page(request: Request, dispute_id: str, capture_id: str = Form(default=""),
              sale_type: str = Form(default=""), lesson: str = Form(default=""),
              outcome_code: str = Form(default=""), csrf_token: str = Form(default=""),
              client=Depends(get_client)):
    if rejected_dispute_id(dispute_id):
        return HTMLResponse("Invalid case ID", status_code=400)
    if not verify_csrf(request, csrf_token):
        if not config.feature_enabled("closure_summary"):
            return HTMLResponse("This form expired. Reload it before saving.", status_code=403)
        view, _link_error = _validate_link(client, dispute_id, capture_id)
        return _render(request, view, dispute_id, capture_id,
                       error="This form expired; your text was not saved. Copy it before reloading and review the current case.",
                       status=403, sale_type_value=sale_type, lesson_value=lesson, form_disabled=True)
    view, error = _validate_link(client, dispute_id, capture_id)
    if not error and view.outcome_code != outcome_code:
        error = "PayPal's recorded outcome changed. Reopen the page and review it again."
    if error:
        if config.feature_enabled("closure_summary"):
            return _render(request, view, dispute_id, capture_id, error=error, status=409,
                           sale_type_value=sale_type, lesson_value=lesson)
        return _render(request, view, dispute_id, capture_id, error=error, status=409)
    try:
        service.record_lesson(dispute_id, capture_id, sale_type, view.outcome_code, lesson)
    except (OSError, ValueError) as exc:
        if config.feature_enabled("closure_summary"):
            record = next((r for r in service.list_lessons() if r.get("dispute_id") == dispute_id), None)
            return _render(request, view, dispute_id, capture_id, record=record, error=str(exc), status=400,
                           sale_type_value=sale_type, lesson_value=lesson)
        return _render(request, view, dispute_id, capture_id, error=str(exc), status=400)
    return RedirectResponse(f"/disputes/{dispute_id}/sale-lesson?capture_id={capture_id}", status_code=303)


@router.get("/sale-lessons", response_class=HTMLResponse)
def future_lessons(request: Request, kind: str = ""):
    if kind and kind not in service.KINDS:
        return HTMLResponse("Unknown sale type", status_code=400)
    return templates.TemplateResponse(request, "features/sale_lesson/list.html",
                                      {"lessons": service.list_lessons(kind or None), "kind": kind})
