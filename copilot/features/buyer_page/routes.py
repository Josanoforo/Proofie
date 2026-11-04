from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.db import repo
from copilot.features.buyer_page import service
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()


def _page(request, view, *, token="", state="ready", action_result=None, csrf_token=None, status_code=200, demo=False):
    buyer_token = token
    csrf = csrf_token or new_csrf_token()
    response = templates.TemplateResponse(request, "features/buyer_page/public.html",
        {"view": view, "token": buyer_token if not demo else "", "state": state, "action_result": action_result,
         "csrf_token": csrf, "demo": demo}, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, csrf)
    return response


@router.get("/buyer/demo", response_class=HTMLResponse, name="buyer_demo")
def buyer_demo(request: Request):
    view = {"description": "Cotton desk runner", "tracking_status": "In transit",
            "expected_delivery": (date.today() + timedelta(days=3)).isoformat(),   # a sample, so a date a few days ahead
             "locale": "en", "photo_data_uri": None}
    return _page(request, view, demo=True)


@router.get("/buyer/{token}", response_class=HTMLResponse, name="buyer_read")
def buyer_read(request: Request, token: str):
    try:
        result = service.read_link(token)
    except (ValueError, OSError):
        result = {"state": "invalid"}
    if result["state"] != "ready":
        return _page(request, {"locale": result.get("locale", "en")}, token=token, state=result["state"], status_code={"invalid": 404, "expired": 410, "used": 409}.get(result["state"], 404))
    return _page(request, result["view"], token=token)


def _buyer_action(request: Request, token: str, action: str, csrf_token: str):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    try:
        result = service.consume_link(token, action)
    except (ValueError, OSError):
        result = {"state": "invalid"}
    if result["state"] != "recorded":
        code = {"invalid": 404, "expired": 410, "used": 409}.get(result["state"], 404)
        return _page(request, {"locale": result.get("locale", "en")}, token=token, state=result["state"], status_code=code)
    return _page(request, {}, token=token, state="recorded", action_result=result)


@router.post("/buyer/{token}/delivered", response_class=HTMLResponse)
def buyer_confirmed(request: Request, token: str, csrf_token: str = Form(default="")):
    return _buyer_action(request, token, "delivery_confirmed", csrf_token)


@router.post("/buyer/{token}/talk", response_class=HTMLResponse)
def buyer_talk(request: Request, token: str, csrf_token: str = Form(default="")):
    return _buyer_action(request, token, "talk_requested", csrf_token)


@router.get("/disputes/{dispute_id}/buyer-ledger", response_class=HTMLResponse)
def buyer_ledger(request: Request, dispute_id: str):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    try:
        events = service.buyer_events(config.db_path(), dispute_id)
    except (ValueError, OSError):
        return refusal_page(request, 400, "Buyer responses unavailable", "The local audit ledger could not be read safely.")
    return templates.TemplateResponse(request, "features/buyer_page/ledger.html",
                                      {"dispute_id": dispute_id, "events": events})


@router.post("/disputes/{dispute_id}/buyer-page", response_class=HTMLResponse)
async def create_buyer_page(request: Request, dispute_id: str, client=Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    form = await request.form(max_files=1, max_fields=8, max_part_size=service.MAX_PHOTO_BYTES + 1024)
    if not verify_csrf(request, str(form.get("csrf_token", ""))):
        return csrf_expired_page(request)
    if config.hosted_demo():
        return refusal_page(request, 403, "Demo buyer page", "Hosted demo pages use fictitious sample data only; they are not created from account cases.")
    try:
        description = str(form.get("description", "")).strip()
        tracking_status = str(form.get("tracking_status", ""))
        locale = str(form.get("locale", "en"))
        eta = str(form.get("expected_delivery", "")).strip()
        if eta:
            date.fromisoformat(eta)
        photo = service.image_data_uri(form.get("packing_photo"))
        view = {"description": description, "tracking_status": tracking_status,
                "expected_delivery": eta, "locale": locale, "photo_data_uri": photo}
        token = service.create_link(dispute_id, view)
    except (ValueError, OSError) as exc:
        return refusal_page(request, 400, "Buyer page not created", str(exc), back_url=f"/disputes/{dispute_id}", back_label="Return to this case")

    buyer_url = str(request.url_for("buyer_read", token=token))
    draft_result, draft_error = None, None
    try:
        from copilot.features.resolution import order, service as resolution_service
        _raw, norm, _limits = resolution_service._read(client, dispute_id)
        allowed_actions = order.allowed_actions(norm) if norm else None
        allowed = {action for action, yes in (("message", getattr(allowed_actions, "message", False)),
                                               ("offer", getattr(allowed_actions, "offer", False))) if yes}
        if "message" not in allowed:
            draft_error = "The fresh PayPal case read does not currently allow a seller message; no message draft was created. The buyer page link remains local and unsent."
        else:
            from copilot.features.negotiation_drafts import service as draft_service
            facts = draft_service.case_facts(norm)
            facts.append({"id": "buyer_page_proposal", "text": f"Buyer page (proposal): {buyer_url}",
                          "source": "Proofie local buyer-page link created by the seller; not sent"})
            data = {"locale": locale, "recipient": "Buyer", "currency": norm.get("currency"),
                    "allowed_actions": sorted(allowed), "facts": facts,
                    "proposals": [{"id": "buyer-page-proposal", "kind": "followup", "amount_cents": 0,
                                   "conditions": "Buyer page link is a proposal only; review the exact draft before any separate send.",
                                   "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}], "responses": []}
            draft_result = draft_service.build(data, draft_service.case_history_path(dispute_id))
    except Exception as exc:  # a read/draft failure must not invalidate the local bearer link
        draft_error = f"No message draft was created ({type(exc).__name__}); the buyer page link remains available locally."
    if draft_result:
        csrf = new_csrf_token()
        response = templates.TemplateResponse(request, "features/negotiation_drafts/index.html",
            {"raw": json.dumps(data, ensure_ascii=False, indent=2), "result": draft_result, "error": None,
             "csrf": csrf, "buyer_page_link": buyer_url})
        response.headers["Cache-Control"] = "no-store"
        set_csrf_cookie(response, csrf)
        return response
    return templates.TemplateResponse(request, "features/buyer_page/seller_result.html",
        {"buyer_url": buyer_url, "error": draft_error, "dispute_id": dispute_id})
