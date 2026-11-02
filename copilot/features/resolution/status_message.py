"""D-101 B5: the prepared status message. The seller's shipping facts, written for the buyer, ready to review.

The facts (carrier, tracking number) are built by code from the tracking the seller registered with PayPal: the fulfilment
file of the sale's capture id (status "registered"), or, when this app has none, PayPal's own tracker list for the sale (one GET);
no model writes any of it. Two cases, one text:
  1. a dispute is open and a fresh read offers `send_message`: GET /disputes/{id}/resolution/status-message shows the text in an
     editable box and posts it to the existing resolution preview. The existing check page, hash-bound approval, write guard
     and read-back send it once. This module never sends, and a GET here writes nothing.
  2. no dispute yet (or none that offers a message): the same text as a copy-ready note headed "Not sent by Proofie", saved
     as not-sent-by-proofie-<date>.txt. It is for the seller's own channel; Proofie sends nothing.
Registered in copilot/app.py behind COPILOT_FEATURE_RESOLUTION_STATUS_MESSAGE (inside the resolution flag's block)."""
from __future__ import annotations

import json
import re
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from copilot import config
from copilot.core import carriers
from copilot.core.disputes import SandboxClient
from copilot.features.resolution import service
from copilot.web.routes_disputes import get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie

router = APIRouter()
CAPTURE_RE = re.compile(r"[A-Za-z0-9]{17}")
NOTE_HEADER = "Not sent by Proofie"
NOTE_INTRO = "Proofie did not send this. Copy it into your own email or chat if you want the buyer to have it."


def status_link_for(capture_id: str) -> str | None:
    """The buyer status link (B4) for this sale, or None. B4 is not part of this build, so there is no link; when B4 lands it
    returns its signed link here. service.message_text_problem refuses link markers in a sent message, so that change must
    also declare the exception for B4's own prefix."""
    return None


def status_text(carrier: str, tracking_number: str, link: str | None = None) -> str:
    """The message the seller reviews. Every literal is the seller's own registered fact; an unknown one is left out."""
    sentences = ["Hello, your order has shipped."]
    number = (tracking_number or "").strip()
    name = carriers.display_name(carrier)
    if number:
        sentences.append(f"Tracking number: {number}" + (f" with {name}." if name else "."))
    elif name:
        sentences.append(f"Carrier: {name}.")
    if link:
        sentences.append(f"You can follow it here: {link}")
    sentences.append("If it has not arrived, reply here and I will look into it right away.")
    return " ".join(sentences)


def note_filename(today: date | None = None) -> str:
    return f"not-sent-by-proofie-{(today or date.today()).isoformat()}.txt"


def note_text(text: str) -> str:
    return f"{NOTE_HEADER}\n{NOTE_INTRO}\n\n{text}\n"


def registered_tracking(capture_ids) -> dict | None:
    """{"capture_id", "carrier", "tracking_number"} from the first capture with a registered tracking file, else None.
    Reads the seller's local fulfilment record only (paypal_tracking reads PayPal's own list)."""
    from copilot.features.fulfilment import store           # lazy: read only
    for capture in capture_ids:
        if not isinstance(capture, str) or not CAPTURE_RE.fullmatch(capture):
            continue
        row = store.get_file(capture)
        if not row or store.effective_status(row) != "registered":
            continue
        try:
            tracker = json.loads(row.get("request_json") or "{}")["trackers"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        number = str(tracker.get("tracking_number") or "").strip()
        if not number:
            continue
        carrier = tracker.get("carrier_name_other") if tracker.get("carrier") == "OTHER" else tracker.get("carrier")
        return {"capture_id": capture, "carrier": carrier or "", "tracking_number": number}
    return None


def paypal_tracking(client, capture_ids) -> dict | None:
    """The same facts read from PayPal's own tracker list for the sale (one GET per capture, nothing written), for a sale whose
    tracking PayPal holds but this app's local fulfilment file does not. Same shape as registered_tracking, else None."""
    from urllib.parse import urlencode
    for capture in capture_ids:
        if not isinstance(capture, str) or not CAPTURE_RE.fullmatch(capture):
            continue
        try:
            body = client.call("GET", f"/v1/shipping/trackers?{urlencode({'transaction_id': capture})}", "context_tracking")
        except Exception:                                    # noqa: BLE001  an unreadable list prepares nothing; the page says so
            continue
        items = (body.get("trackers") if isinstance(body, dict) else None) or []
        for tracker in items:
            number = str((tracker or {}).get("tracking_number") or "").strip() if isinstance(tracker, dict) else ""
            if number:
                carrier = tracker.get("carrier_name_other") if tracker.get("carrier") == "OTHER" else tracker.get("carrier")
                return {"capture_id": capture, "carrier": carrier or "", "tracking_number": number}
    return None


def prepared_text(tracking: dict | None) -> str:
    return status_text(tracking["carrier"], tracking["tracking_number"], status_link_for(tracking["capture_id"])) if tracking else ""


def _page(request: Request, template: str, **ctx):
    token = new_csrf_token()
    response = templates.TemplateResponse(request, template, {"csrf_token": token, **ctx})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.get("/disputes/{dispute_id}/resolution/status-message", response_class=HTMLResponse)
def status_message_page(request: Request, dispute_id: str, client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    state = service.page_state(client, dispute_id)               # reads only
    if state.norm is None:
        return refusal_page(request, 502, "PayPal could not be read", "Proofie could not read this dispute, so it prepared nothing.",
                            back_url=f"/disputes/{dispute_id}/resolution", back_label="Back to resolution options")
    captures = state.norm.get("seller_transaction_ids") or []
    tracking = registered_tracking(captures) or paypal_tracking(client, captures)
    text = prepared_text(tracking)
    send_open = bool(text) and state.actions.message and config.feature_enabled("resolution_writes")
    return _page(request, "features/resolution/status_message.html", dispute_id=dispute_id, tracking=tracking, text=text,
                 send_open=send_open, offer_open=bool(state.actions.offer), note_name=note_filename(),
                 settling=state.settling)


@router.get("/status-message/{capture_id}", response_class=HTMLResponse)
def note_page(request: Request, capture_id: str, client: SandboxClient = Depends(get_client)):
    if not CAPTURE_RE.fullmatch(capture_id):
        return refusal_page(request, 404, "Nothing here", "That is not a sale id.", back_url="/disputes", back_label="All disputes")
    tracking = registered_tracking([capture_id]) or paypal_tracking(client, [capture_id])
    text = prepared_text(tracking)
    return _page(request, "features/resolution/status_note.html", capture_id=capture_id, tracking=tracking, text=text,
                 note=note_text(text) if text else "", note_name=note_filename())


@router.get("/status-message/{capture_id}/note.txt")
def note_file(capture_id: str, client: SandboxClient = Depends(get_client)):
    tracking = (registered_tracking([capture_id]) or paypal_tracking(client, [capture_id])) if CAPTURE_RE.fullmatch(capture_id) else None
    if not tracking:
        return PlainTextResponse("Nothing to save: no registered tracking for this sale.\n", status_code=404)
    return PlainTextResponse(note_text(prepared_text(tracking)), media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{note_filename()}"',
                                      "Cache-Control": "no-store"})
