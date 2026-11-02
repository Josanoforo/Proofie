"""/disputes/{dispute_id}/resolution routes. Registered in copilot/app.py behind COPILOT_FEATURE_RESOLUTION (lazy import
inside that flag block). The two PayPal writes (make-offer, send-message) are reachable only from register_action,
after the seller approved the exact body on the check page (its sha256 travels in the confirm form; the body itself is
kept on the server under a random token and is never put in a URL or a form field). COPILOT_FEATURE_RESOLUTION_WRITES
(default off) switches the writing routes off: the page then shows the order and the economics read-only.
"Defend" is a plain link to the existing dispute page (package and evidence flow); no evidence POST and no
escalation exist here.

Flow:
  hub                 GET  /resolution                      the options and the history (never a preview)
  review (preview)    POST /resolution/preview              validates, stores the exact body, 303 to the check page
  check page          GET  /resolution/check/{token}        what PayPal will receive; the one Send button
  send                POST /resolution/register             stored body, hash check, send-once guard, 303 to the sent page
  sent page           GET  /resolution/sent/{row_id}        what was sent and what PayPal shows (the stored record)
  check PayPal again  POST /resolution/check                reads only; confirms only from PayPal's response"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core.disputes import SandboxClient
from copilot.core import paypal_words, write_guard
from copilot.features.resolution import actions as act
from copilot.features.resolution import order
from copilot.features.resolution import service, store
from copilot.web import links, status, timefmt
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
AMOUNT_RE = re.compile(r"^(\d{1,7})(?:[.,](\d{1,2}))?$")
CODE_SUFFIX_RE = re.compile(r"^(.*?)\s*([A-Za-z]{3})$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CAPTURE_RE = re.compile(r"[A-Za-z0-9]{17}")
EXPIRED_TEXT = "This check has expired. Nothing was sent."
SPENT_TEXT = ("This check was already used or replaced, so it cannot send again. Nothing was sent. "
              "Go back to the resolution options and review it again to get a new check.")
CHECKED_NOTES = {"failed": "Proofie could not read the dispute from PayPal, so nothing changed. Try again in a minute.",
                 "settling": "PayPal is still updating this dispute, so nothing changed. Try again in a minute.",
                 "offered": "PayPal answered and still offers the refund option on this dispute, so it does not show your "
                            "offer. Nothing was changed."}
# status.message_status itself says "PayPal lists your message" only when the row's read-back has message_listed
# true; a row confirmed before that check reads status.LEGACY_CONFIRMED_LABEL. The resolution pages use it directly.
message_status_view = status.message_status


def _writes_enabled() -> bool:
    return config.feature_enabled("resolution_writes")


def _row_number(text: str) -> int | None:
    return int(text) if text and text.isascii() and text.isdigit() and len(text) < 12 else None


def parse_amount_cents(text: str, currency: str | None = None) -> int | None:
    """"10.00" -> 1000, "10" -> 1000, "10.5" -> 1050; None when it is not a plain amount. Integer arithmetic only. A trailing
    currency code ("10 USD") is accepted only when it equals `currency`."""
    value = (text or "").strip()
    if currency:
        found = CODE_SUFFIX_RE.match(value)
        if found and found.group(2).upper() == currency.upper():
            value = found.group(1).strip()
    found = AMOUNT_RE.match(value)
    if not found:
        return None
    whole, frac = found.group(1), (found.group(2) or "").ljust(2, "0")
    return int(whole) * 100 + int(frac)


def offer_errors(amount: str, note: str, currency: str | None, max_cents: int | None) -> dict:
    """Every problem of the offer form at once, keyed by the field name (amount, note); {} when it is fine."""
    errors: dict = {}
    value = (amount or "").strip()
    cur = currency or ""
    shown_max = f"{act.cents_to_str(max_cents, cur)} {cur}" if isinstance(max_cents, int) and cur else ""
    if not value:
        errors["amount"] = "Enter the offer amount"
    elif "$" in value:
        errors["amount"] = "Enter only the number, without $"
    else:
        suffix = CODE_SUFFIX_RE.match(value)
        if suffix and cur and suffix.group(2).upper() != cur.upper() and suffix.group(1).strip():
            errors["amount"] = f"This dispute is in {cur}. Enter the amount in {cur}."
        else:
            cents = parse_amount_cents(value, cur or None)
            if cents is None:
                errors["amount"] = "Enter the offer amount as a number, for example 10.00"
            elif cents <= 0:
                errors["amount"] = f"The offer must be more than 0.00 {cur}".rstrip()
            elif isinstance(max_cents, int) and cents > max_cents:
                errors["amount"] = f"The offer must be {shown_max} or less, the disputed amount"
    text = (note or "").strip()
    if not text:
        errors["note"] = "Enter a note to the buyer"
    elif len(text) > act.MAX_TEXT_LEN:
        errors["note"] = f"Note must be {act.MAX_TEXT_LEN} characters or fewer"
    return errors


def already_sent_view(dispute_id: str, action: str, sha: str) -> dict | None:
    """What the page says when this exact request was already sent: the stored row (time, PayPal's
    confirmation state) or, while a send of it is in flight or its end is unknown, the reservation. None when the
    request is free to send. Read from the app's own records only; no PayPal call."""
    if action not in service.WRITE or not SHA_RE.match(sha or ""):
        return None
    row = store.find_sent(dispute_id, action, sha)
    reservation = None if row else write_guard.lookup(dispute_id, action, sha)
    if row is None and not write_guard.is_blocking(reservation):
        return None
    return {"action": action, "noun": "offer" if action == "offer" else "message", "row": row, "sha256": sha}


def _hub_url(dispute_id: str) -> str:
    return f"/disputes/{dispute_id}/resolution"


def _sent_url(dispute_id: str, row_id) -> str:
    return f"{_hub_url(dispute_id)}/sent/{row_id}#sent"


def _capture_ids(norm: dict | None) -> list:
    return list(dict.fromkeys(capture for capture in ((norm or {}).get("seller_transaction_ids") or [])
                              if isinstance(capture, str) and CAPTURE_RE.fullmatch(capture)))


def _delivery_fact_ids(norm: dict | None) -> list:
    """Capture ids with a link to the local delivery facts, only when a local tracking file exists for them.
    The facts route takes a capture/transaction id, never the dispute id. Reads the fulfilment store, never writes."""
    if not (config.feature_enabled("delivery_facts") and norm):
        return []
    try:
        from copilot.features.fulfilment import store as fulfilment_store      # lazy: read only
        return [capture for capture in _capture_ids(norm) if fulfilment_store.get_file(capture)]
    except Exception:  # noqa: BLE001 - a display aid; a failed lookup only hides the link
        return []


def _last_checked(history: list) -> str | None:
    stamps = [store.readback_of(h).get("checked_at") for h in history if h.get("id")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def _accept_claim_offer(state: service.PageState, dispute_id: str = "", app=None, cookies=None) -> bool:
    """The last-resort control: the setting is on and PayPal's fresh read of this page has the link. On the hosted demo it is the
    judge's own pool case only (judge mode on, held by this session, never a record dispute)."""
    if not (config.feature_enabled("accept_claim") and bool(state.norm) and order.has_link(state.norm, "accept-claim")):
        return False
    if not config.hosted_demo():
        return True
    hook = getattr(getattr(app, "state", None), "accept_claim_allowed", None)      # set by the accept_claim block in create_app
    return bool(hook and hook(dispute_id, cookies))


def _context(dispute_id: str, state: service.PageState, **extra) -> dict:
    economics = state.economics
    messages = [h for h in state.history if h["action"] == "message" and h["status"] in store.SENT_STATUSES]
    captures = _capture_ids(state.norm)
    drafts_enabled = config.feature_enabled("negotiation_drafts")
    draft_history = []
    draft_history_error = ""
    if drafts_enabled and state.norm:
        try:
            from copilot.features.negotiation_drafts.service import read_case_history
            draft_history = read_case_history(dispute_id)
        except (OSError, ValueError) as exc:
            draft_history_error = str(exc)
    return {"delivery_fact_ids": _delivery_fact_ids(state.norm), "tracking_capture": captures[0] if captures else None,
            "capture_ids": captures, "message_status": message_status_view,
            "max_amount": (act.cents_to_str(state.norm["amount_cents"], state.norm.get("currency") or "")
                           if state.norm and isinstance(state.norm.get("amount_cents"), int) else ""),
            "dispute_id": dispute_id, "state": state, "norm": state.norm, "writes_enabled": _writes_enabled(),
            "economics": economics, "csrf_token": new_csrf_token(), "errors": {}, "refusal": "", "form_values": {},
            "refund_pointer": bool(state.norm) and order.has_link(state.norm, "accept-claim"),
            "accept_claim_offer": _accept_claim_offer(state),
            "RESOLUTION_CENTER_NAME": links.RESOLUTION_CENTER_NAME,
            "already": None, "latest_message": messages[-1] if messages else None,
            "last_checked": _last_checked(state.history), "checked_note": "", "preview_refused": False,
            "negotiation_drafts_enabled": drafts_enabled,
            "draft_actions": {"offer": bool(state.actions.offer), "message": bool(state.actions.message)},
            "case_draft_history": draft_history, "case_draft_history_error": draft_history_error, **extra}


def _render(request: Request, dispute_id: str, state: service.PageState, status_code: int = 200, **extra):
    ctx = _context(dispute_id, state, **extra)
    ctx["accept_claim_offer"] = _accept_claim_offer(state, dispute_id, request.app, request.cookies)
    response = templates.TemplateResponse(request, "features/resolution/resolution.html", ctx, status_code=status_code)
    set_csrf_cookie(response, ctx["csrf_token"])
    return response


def _render_check(request: Request, dispute_id: str, check: dict | None, status_code: int = 200, **extra):
    """The check page (or its expired form). Never cached: it holds the exact text and a send control."""
    token = new_csrf_token()
    ctx = {"dispute_id": dispute_id, "check": check, "csrf_token": token, "expired_text": EXPIRED_TEXT, "refusal": "",
           "already": None, "norm": None, "token": "", "origin_labels": service.ORIGIN_LABELS,
           "message_status": message_status_view, "disputed_text": "", "expired_title": "Check expired", **extra}
    norm = ctx.get("norm")
    if norm and isinstance(norm.get("amount_cents"), int) and norm.get("currency"):
        ctx["disputed_text"] = f"{act.cents_to_str(norm['amount_cents'], norm['currency'])} {norm['currency']}"
    if check is not None:
        ctx["body"] = check["body"]
        ctx["body_text"] = json.dumps(check["body"], sort_keys=True, ensure_ascii=False)
    response = templates.TemplateResponse(request, "features/resolution/check.html", ctx, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


def _guard(request: Request, dispute_id: str, csrf_token: str):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not _writes_enabled():
        return refusal_page(request, 403, "Writes are switched off",
                            "Offer and message are switched off in this installation, so nothing was sent to PayPal.",
                            back_url=_hub_url(dispute_id), back_label="Back to resolution options")
    return None


def _stored_check(dispute_id: str, token: str, action: str | None = None, include_spent: bool = False) -> dict | None:
    """The stored check of this token when it is still valid, belongs to this dispute and its body still hashes to the stored
    fingerprint (a record that no longer matches is treated as unusable, never repaired). A spent check (its send may have
    reached PayPal, or a release replaced it) is returned only with `include_spent`."""
    check = store.get_check(token, dispute_id, include_spent=include_spent)
    if check is None or (action is not None and check["action"] != action):
        return None
    if act.request_sha256(check["body"]) != check["request_sha256"]:
        return None
    return check


def proposed_offer(state) -> dict | None:
    """The offer form's proposal (COPILOT_FEATURE_SELLER_SHORTCUTS): the amount the buyer asked for, as PayPal's own record
    gives it, when it is in the disputed currency and within the disputed amount, and a short note built by code. The seller
    edits both; the request is still shown exactly on the check page and nothing is sent until the seller sends it."""
    norm, asked = state.norm, state.requested
    if not (norm and isinstance(asked, dict) and state.actions.offer):
        return None
    currency, max_cents = norm.get("currency"), norm.get("amount_cents")
    if not currency or asked.get("currency_code") != currency or not isinstance(max_cents, int):
        return None
    cents = parse_amount_cents(str(asked.get("value") or ""), currency)
    if cents is None or cents <= 0 or cents > max_cents:
        return None
    amount = act.cents_to_str(cents, currency)
    return {"amount": amount, "note": f"I am offering a refund of {amount} {currency} to settle this dispute. Please accept the offer in PayPal."}


@router.get("/disputes/{dispute_id}/resolution", response_class=HTMLResponse)
def resolution_page(request: Request, dispute_id: str, already: str = "", sha: str = "", checked: str = "",
                    client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    service.settle_pending(client, dispute_id)         # one GET per pending row per refresh, never a loop
    values = {}
    from_token = request.query_params.get("from", "")
    if from_token and _writes_enabled():
        check = _stored_check(dispute_id, from_token)
        if check is not None:                          # "Change the message / offer": refilled from the server record
            if check["action"] == "message":
                values = {"text": check["body"].get("message", "")}
            else:
                amount = check["body"].get("offer_amount") or {}
                values = {"amount": amount.get("value", ""), "note": check["body"].get("note", "")}
    state = service.page_state(client, dispute_id)
    offer_proposed = False
    if not values and config.feature_enabled("seller_shortcuts"):
        proposal = proposed_offer(state)
        if proposal:
            values, offer_proposed = dict(proposal), True
    handoff_id = request.query_params.get("local_autopilot_id", "")
    handoff = None
    handoff_error = ""
    if handoff_id:
        if config.feature_enabled("local_autopilot") and config.feature_enabled("negotiation_drafts") and state.norm:
            try:
                from copilot.features.local_autopilot.routes import handoff_for_case
                allowed = {name for name, enabled in (("offer", state.actions.offer), ("message", state.actions.message)) if enabled}
                handoff = handoff_for_case(dispute_id, handoff_id, state.norm, allowed)
            except (OSError, PermissionError, ValueError, TypeError):
                handoff = None
        if handoff:
            values.update({"draft_kind": "partial_offer" if handoff["action"] == "offer" else "followup",
                           "draft_amount": handoff["amount_text"] if handoff["action"] == "offer" else ""})
        else:
            handoff_error = "This simulation is unavailable, conflicted, or stale for the current case. No terms were imported."
    return _render(request, dispute_id, state,
                   already=already_sent_view(dispute_id, already, sha), form_values=values, offer_proposed=offer_proposed,
                   checked_note=CHECKED_NOTES.get(checked, ""), autopilot_handoff=handoff,
                   autopilot_handoff_error=handoff_error)


@router.post("/disputes/{dispute_id}/resolution/preview", response_class=HTMLResponse)
def preview(request: Request, dispute_id: str, csrf_token: str = Form(default=""), action: str = Form(default=""),
            amount: str = Form(default=""), note: str = Form(default=""), text: str = Form(default=""),
            proposed_from_record: str = Form(default=""), client: SandboxClient = Depends(get_client)):
    refused = _guard(request, dispute_id, csrf_token)
    if refused:
        return refused
    values = {"amount": amount, "note": note, "text": text}
    if action == "offer":
        state = service.page_state(client, dispute_id)
        norm = state.norm or {}
        errors = offer_errors(amount, note, norm.get("currency"), norm.get("amount_cents")) if state.norm else {}
        if errors:
            return _render(request, dispute_id, state, 400, errors=errors, form_values=values)
        cents = parse_amount_cents(amount, norm.get("currency"))
        if cents is None:
            return _render(request, dispute_id, state, 400,
                           errors={"amount": "Enter the offer amount as a number, for example 10.00"}, form_values=values)
        prepared = service.prepare_offer(client, dispute_id, cents, note)
    elif action == "message":
        problem = service.message_text_problem(text)
        if problem:
            state = service.page_state(client, dispute_id)
            return _render(request, dispute_id, state, 400, errors={"text": problem}, form_values=values)
        prepared = service.prepare_message(client, dispute_id, text or None)
    else:
        return refusal_page(request, 400, "Unknown action", "Choose offer or message.",
                            back_url=_hub_url(dispute_id), back_label="Back to resolution options")
    if not prepared.allowed or not prepared.body or not prepared.sha256:
        state = service.page_state(client, dispute_id)
        field = "amount" if action == "offer" else "text"
        offered = state.actions.offer if action == "offer" else state.actions.message
        if offered and prepared.reason:                # PayPal still offers it: the input is what is wrong
            return _render(request, dispute_id, state, 400, errors={field: prepared.reason}, form_values=values)
        return _render(request, dispute_id, state, 409, refusal=prepared.reason or state.next_step_text,
                       form_values=values, preview_refused=True)
    origin = prepared.origin
    if action == "offer" and proposed_from_record == "1" and config.feature_enabled("seller_shortcuts"):
        proposal = proposed_offer(state)          # the label stays only while the amount is still the one PayPal's record gives
        if proposal and proposal["amount"] == amount.strip():
            origin = "paypal_record"
    token = store.save_check(dispute_id, action, prepared.body, prepared.sha256, origin)
    return RedirectResponse(f"{_hub_url(dispute_id)}/check/{token}#preview", status_code=303)


@router.get("/disputes/{dispute_id}/resolution/check/{token}", response_class=HTMLResponse)
def check_page(request: Request, dispute_id: str, token: str, client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not _writes_enabled():
        return refusal_page(request, 403, "Writes are switched off",
                            "Offer and message are switched off in this installation, so there is nothing to review or send.",
                            back_url=_hub_url(dispute_id), back_label="Back to resolution options")
    check = _stored_check(dispute_id, token)
    if check is None:
        spent = _stored_check(dispute_id, token, include_spent=True)
        if spent is None:
            return _render_check(request, dispute_id, None, 410)
        if already_sent_view(dispute_id, spent["action"], spent["request_sha256"]) is None:
            return _render_check(request, dispute_id, None, 409, expired_text=SPENT_TEXT, expired_title="Check already used")
        check = spent                                    # the notice about the send, never a send control
    already = already_sent_view(dispute_id, check["action"], check["request_sha256"])
    norm = None
    if already is None and check["action"] == "offer":      # the disputed amount next to the offer: read from PayPal
        _raw, norm, _limits = service._read(client, dispute_id)
    return _render_check(request, dispute_id, check, 200, token=token, norm=norm, already=already)


def refusal_text(outcome: service.Outcome, action: str) -> str:
    """What the seller reads when nothing was sent. Plain sentences chosen from the outcome code."""
    noun = "refund" if action == "offer" else "message"
    if outcome.status == "read_failed":
        return ("Nothing was sent. Proofie could not read the dispute from PayPal. Your text is kept. "
                "Press Send when PayPal answers again.")
    if outcome.status == "rejected_link" and outcome.settling:
        return ("Nothing was sent. PayPal is updating this dispute, so it does not offer this option right now. "
                "Try again in a minute; your text is kept.")
    if outcome.status == "rejected_link":
        when = timefmt.display(datetime.now(timezone.utc))
        return (f"Nothing was sent. PayPal no longer offers the {noun} option on this dispute (checked {when}). "
                f"Your text is kept below.")
    if outcome.status == "refused_422":
        return ("Nothing was sent. PayPal was still updating this dispute. Wait a minute, then review and send again; "
                "your text is kept.")
    if outcome.status == "rejected_hash":
        return "Nothing was sent. The approval did not match the request you reviewed. Review it again below."
    if outcome.status == "rejected_body":
        return f"Nothing was sent. {outcome.reason}"
    if outcome.status == "already_listed":
        return ("Nothing was sent. PayPal already lists a message from you with this exact text. Change the text to say "
                "something different.")
    reason = outcome.reason or "PayPal returned an error."
    if outcome.status == "failed" and not paypal_words.cause_in_technical(outcome.technical):
        reason += " " + paypal_words.NOT_SAID_ABOVE
    return (f"Nothing was sent. {reason} Your text is kept. Change it and send again, or check the dispute in "
            f"PayPal's Resolution Center.")


@router.post("/disputes/{dispute_id}/resolution/register", response_class=HTMLResponse)
def register_action(request: Request, dispute_id: str, csrf_token: str = Form(default=""),
                    action: str = Form(default=""), token: str = Form(default=""),
                    approval_sha256: str = Form(default=""), client: SandboxClient = Depends(get_client)):
    refused = _guard(request, dispute_id, csrf_token)
    if refused:
        return refused
    back = _hub_url(dispute_id)
    if action not in service.WRITE:
        return refusal_page(request, 400, "Unknown action", "Choose offer or message.", back_url=back, back_label="Back to resolution options")
    check = _stored_check(dispute_id, token, action)
    if check is None:
        spent = _stored_check(dispute_id, token, action, include_spent=True)
        if spent is None:
            return _render_check(request, dispute_id, None, 410)
        blocked = already_sent_view(dispute_id, action, spent["request_sha256"])
        if blocked is None:
            # the approval was used (or replaced by a release): a repeat needs a new check page. Nothing reaches PayPal.
            return _render_check(request, dispute_id, None, 409, expired_text=SPENT_TEXT, expired_title="Check already used")
        # a spent check never reaches service.register. A release landing after the read above could free the key
        # in between, and the spent token would send. It is answered from the record that blocked it, and nothing more.
        if blocked["row"]:
            return RedirectResponse(_sent_url(dispute_id, blocked["row"]["id"]), status_code=303)
        return RedirectResponse(f"{back}?{urlencode({'already': action, 'sha': blocked['sha256']})}", status_code=303)
    if act.request_sha256(check["body"]) != approval_sha256:
        # The stored record is shown again, never a submitted body; nothing reaches PayPal.
        return _render_check(request, dispute_id, check, 409, token=token,
                             refusal=refusal_text(service.Outcome("rejected_hash", None, None, None), action))
    outcome = service.register(client, dispute_id, action, check["body"], approval_sha256, origin=check["origin"])
    if outcome.status == "already_sent":
        if outcome.existing:
            return RedirectResponse(_sent_url(dispute_id, outcome.existing["id"]), status_code=303)
        return RedirectResponse(f"{back}?{urlencode({'already': action, 'sha': outcome.request_sha256})}", status_code=303)
    if outcome.status in (store.STATUS_PENDING, "sent_unconfirmed") and outcome.row_id:
        store.spend_check(token, dispute_id)             # the POST may have reached PayPal: this approval is used up
        return RedirectResponse(_sent_url(dispute_id, outcome.row_id), status_code=303)
    norm = service._read(client, dispute_id)[1] if (action == "offer" and outcome.status in ("rejected_link", "rejected_body")) else None
    return _render_check(request, dispute_id, check, 502 if outcome.status in ("read_failed", "failed") else 409, token=token,
                         refusal=refusal_text(outcome, action), outcome=outcome, norm=norm)


def _stored_offer(row: dict) -> dict | None:
    if row["action"] != "offer":
        return None
    try:
        body = json.loads(row.get("request_json") or "{}")
    except ValueError:
        return None
    amount = body.get("offer_amount") if isinstance(body, dict) else None
    if not isinstance(amount, dict):
        return None
    return {"value": amount.get("value"), "currency": amount.get("currency_code"), "note": body.get("note", "")}


@router.get("/disputes/{dispute_id}/resolution/sent/{row_id}", response_class=HTMLResponse)
def sent_page(request: Request, dispute_id: str, row_id: str, checked: str = "",
              client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    number = _row_number(row_id)
    row = store.get_action(dispute_id, number) if number is not None else None
    if row is None:
        return refusal_page(request, 404, "Nothing sent here", "Proofie has no record of this send for this dispute.",
                            back_url=_hub_url(dispute_id), back_label="Back to resolution options")
    service.settle_pending(client, dispute_id)            # one GET per pending row, never a loop; reads only
    row = store.get_action(dispute_id, row["id"]) or row
    readback = store.readback_of(row)
    token = new_csrf_token()
    ctx = {"dispute_id": dispute_id, "row": row, "readback": readback, "csrf_token": token,
           "message_status": message_status_view,
           "text": service._stored_message_text(row) if row["action"] == "message" else "",
           "offer": _stored_offer(row), "origin": service.ORIGIN_LABELS.get(readback.get("origin")),
           "checked_note": CHECKED_NOTES.get(checked, "")}
    response = templates.TemplateResponse(request, "features/resolution/sent.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.post("/disputes/{dispute_id}/resolution/check", response_class=HTMLResponse)
def check_paypal_again(request: Request, dispute_id: str, csrf_token: str = Form(default=""), row_id: str = Form(default=""),
                       client: SandboxClient = Depends(get_client)):
    """Reads only: one GET of the dispute per pending or unconfirmed row; a row changes only from PayPal's response and
    nothing is ever sent. Answers 303 back to the sent page (or the hub's record)."""
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    chosen = _row_number(row_id)
    if chosen is not None and store.get_action(dispute_id, chosen) is None:
        return refusal_page(request, 404, "Nothing sent here", "Proofie has no record of this send for this dispute.",
                            back_url=_hub_url(dispute_id), back_label="Back to resolution options")
    result = service.check_rows(client, dispute_id, chosen)
    quiet = not (result.confirmed or result.absent)
    if result.error:
        note = "failed"
    elif result.offered and quiet:
        note = "offered"                 # PayPal answered and still offers the refund option: worded from that answer
    else:
        note = "settling" if result.unchanged and quiet else ""
    query = f"?{urlencode({'checked': note})}" if note else ""
    if chosen is not None:
        return RedirectResponse(f"{_hub_url(dispute_id)}/sent/{chosen}{query}#sent", status_code=303)
    return RedirectResponse(f"{_hub_url(dispute_id)}{query}#recorded", status_code=303)
