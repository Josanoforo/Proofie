"""/disputes/{dispute_id}/draft routes (exactly two). Registered in copilot/app.py behind COPILOT_FEATURE_DRAFT (lazy import inside
that flag block). The page shows the facts built by code, the fixed template and the latest draft from the local model, marked as
a suggestion to check. Nothing here writes to PayPal: on a dispute that accepts a message, the seller's reviewed text is handed to
the resolution feature's own preview (a form posting action=message and text), which shows the exact body and its hash and
needs the seller's approval, one POST and a GET after it. At the complaint stage (no dispute) the text is only copied.

The box the seller sends from always starts as the fixed template, written by code. AI text enters it only through a plain
GET (?start=ai), and only when the latest draft is current (service.assess). A model failure answers 303 to ?model=unavailable or
?model=unreadable (only those two values pick a fixed sentence), so a refresh never runs the model again. A failed PayPal read
shows no PayPal state and the model is not called."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core.disputes import SandboxClient
from copilot.features.draft import service
from copilot.web import timefmt
from copilot.web.routes_disputes import csrf_expired_page, get_client, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
DISCLAIMER = "The AI model is an assistant only and can be wrong. You review every word."
MODEL_NOTICES = {
    "unavailable": "The AI model did not answer. Your message still holds the fixed template. You can ask again in a minute.",
    "unreadable": "The AI model's answer could not be read. Your message still holds the fixed template.",
}
# Display labels of the facts table (the prompt keeps the sheet's own labels); currency is part of the amount, the dispute id is the page's h1.
FACT_LABELS = {"order date": "Order date", "amount": "Order amount", "shipping address": "Shipping address",
               "tracking number": "Tracking number", "carrier": "Carrier", "last tracking status": "Last tracking status",
               "delivery estimate": "Delivery estimate", "dispute stage": "Dispute stage"}


def get_model_call():
    """The model call to use; None means the real one (pilot's, redirected to the configured Ollama). Tests override this."""
    return None


def _written_at(draft: dict | None) -> str:
    """The stored creation time in the seller's words (Mexico City time), or '' when it cannot be read."""
    shown = timefmt.display(draft["created_at"]) if draft else ""
    return "" if shown in ("", "Not available") else shown


def _render(request: Request, dispute_id: str, view: service.DraftView, status_code: int = 200, start: str | None = None,
            model_value: str | None = None):
    draft = view.draft
    fresh = view.state == service.STATE_VALID
    use_ai = start == "ai" and fresh
    refused_start = start == "ai" and not fresh
    token = new_csrf_token()
    sentences = service.rejected_sentences(view.issues) if view.state == service.STATE_REJECTED and view.issues else []
    response = templates.TemplateResponse(request, "features/draft/draft.html", {
        "dispute_id": dispute_id, "view": view, "draft": draft, "draft_state": view.state, "stale_labels": view.stale_labels,
        "facts": [(key, FACT_LABELS[key], value) for key, value in view.sheet.labelled() if key in FACT_LABELS],
        "facts_raw": view.sheet.labelled(),
        "box_text": draft["en"] if use_ai else view.template_text, "box_has_ai": use_ai, "start_refused": refused_start,
        "ai_es": draft["es"] if fresh else "", "rejected_sentences": sentences, "written_at": _written_at(draft),
        "disclaimer": DISCLAIMER, "model_notice": MODEL_NOTICES.get(model_value or ""), "csrf_token": token,
        "paypal_channel": view.channel == service.CHANNEL_PAYPAL, "unknown_channel": view.channel == service.CHANNEL_UNKNOWN},
        status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.get("/disputes/{dispute_id}/draft", response_class=HTMLResponse)
def draft_page(request: Request, dispute_id: str, start: str | None = None, model: str | None = None,
               client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    return _render(request, dispute_id, service.draft_view(client, config.db_path(), dispute_id), start=start, model_value=model)


@router.post("/disputes/{dispute_id}/draft/model", response_class=HTMLResponse)
def draft_with_model(request: Request, dispute_id: str, csrf_token: str = Form(default=""),
                     client: SandboxClient = Depends(get_client), call=Depends(get_model_call)):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    page = f"/disputes/{dispute_id}/draft"
    view = service.make_draft(client, config.db_path(), dispute_id, call=call)
    if view.channel == service.CHANNEL_UNKNOWN:
        return RedirectResponse(page, status_code=303)           # PayPal could not be read: the page says so; no model call was made
    if view.error:
        if config.hosted_demo():
            return _render(request, dispute_id, view, 503)       # hosted: the model is off; the page and its status stay as they were
        return RedirectResponse(f"{page}?model={view.error_kind}#ai", status_code=303)
    return RedirectResponse(page, status_code=303)
