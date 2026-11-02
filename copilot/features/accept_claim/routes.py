"""/disputes/{dispute_id}/resolution/accept-claim routes. Registered in copilot/app.py behind COPILOT_FEATURE_ACCEPT_CLAIM (lazy
import inside that flag block). The one PayPal write (accept-claim) is reachable only from `register`, after the seller read the
consequence, typed the amount phrase and approved the exact request by hash; the check page is a separate step from the send.

  preview  POST /resolution/accept-claim/preview     fresh GET, stores the exact request, 303 to the check page
  check    GET  /resolution/accept-claim/check/{t}   consequence, amount from PayPal's record, phrase field, hash
  send     POST /resolution/accept-claim/register    hash, phrase, fresh GET, once-guard, one POST, one read-back GET
  sent     GET  /resolution/accept-claim/sent/{id}   what PayPal shows (one GET per load)"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core.disputes import SandboxClient
from copilot.features.accept_claim import service as accept
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
BASE = "/disputes/{dispute_id}/resolution/accept-claim"
SPENT_TEXT = "This approval was already used or replaced. Nothing was sent. Go back to the resolution options and review it again."


def _session(request: Request) -> str | None:
    return accept.session_of(request.cookies) if config.hosted_demo() else None


def _hub(dispute_id: str) -> str:
    return f"/disputes/{dispute_id}/resolution"


def _refuse(request: Request, dispute_id: str, status: int, title: str, text: str):
    return refusal_page(request, status, title, text, back_url=_hub(dispute_id), back_label="Back to resolution options")


def _page(request: Request, template: str, dispute_id: str, status_code: int = 200, **extra):
    token = new_csrf_token()
    ctx = {"dispute_id": dispute_id, "csrf_token": token, "hub": _hub(dispute_id), "consequence": accept.CONSEQUENCE, "refusal": "",
           "phrase": "", "check": None, "token": "", "sent": None, **extra}
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


def _guard(request: Request, dispute_id: str, csrf_token: str = "", write: bool = True):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if write and not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    if not config.feature_enabled("accept_claim"):
        return _refuse(request, dispute_id, 403, "Accepting a claim is switched off",
                       "Accepting a claim is switched off in this installation. Nothing was sent to PayPal.")
    if config.hosted_demo():
        reason = accept.hosted_refusal(dispute_id, _session(request))
        if reason:
            return _refuse(request, dispute_id, 403, "Not available for this case", reason)
    return None


@router.post(BASE + "/preview", response_class=HTMLResponse)
def preview(request: Request, dispute_id: str, csrf_token: str = Form(default=""), client: SandboxClient = Depends(get_client)):
    refused = _guard(request, dispute_id, csrf_token)
    if refused:
        return refused
    if config.hosted_demo() and not accept.was_accepted(dispute_id):
        reason = accept.cap_refusal(_session(request))
        if reason:
            return _refuse(request, dispute_id, 403, "Not available now", reason)
    prepared = accept.prepare(client, dispute_id, _session(request))
    if not prepared.ok:
        return _refuse(request, dispute_id, 409, "Not offered by PayPal", prepared.reason)
    token = accept.save_check(dispute_id, prepared.body, prepared.sha256)
    return RedirectResponse(f"{_hub(dispute_id)}/accept-claim/check/{token}", status_code=303)


@router.get(BASE + "/check/{token}", response_class=HTMLResponse)
def check_page(request: Request, dispute_id: str, token: str):
    refused = _guard(request, dispute_id, write=False)
    if refused:
        return refused
    check = accept.get_check(token, dispute_id)
    if check is None:
        spent = accept.get_check(token, dispute_id, include_spent=True)
        return _page(request, "features/accept_claim/check.html", dispute_id, 409 if spent else 410,
                     refusal=SPENT_TEXT if spent else "This check has expired. Nothing was sent.")
    body = check["body"]
    return _page(request, "features/accept_claim/check.html", dispute_id, check=check, token=token,
                 phrase=accept.phrase_for(body["amount"], body["currency"]))


@router.post(BASE + "/register", response_class=HTMLResponse)
def register(request: Request, dispute_id: str, csrf_token: str = Form(default=""), token: str = Form(default=""),
             approval_sha256: str = Form(default=""), phrase: str = Form(default=""), client: SandboxClient = Depends(get_client)):
    refused = _guard(request, dispute_id, csrf_token)
    if refused:
        return refused
    check = accept.get_check(token, dispute_id)
    if check is None:
        spent = accept.get_check(token, dispute_id, include_spent=True)
        return _page(request, "features/accept_claim/check.html", dispute_id, 409 if spent else 410,
                     refusal=SPENT_TEXT if spent else "This check has expired. Nothing was sent.")
    shown = accept.phrase_for(check["body"]["amount"], check["body"]["currency"])
    if approval_sha256 != check["approval_sha256"]:
        outcome = accept.Outcome("rejected_hash", "The approval does not match this request. Nothing was sent.")
    else:
        outcome = accept.register(client, dispute_id, check["body"], check["approval_sha256"], phrase, _session(request))
    if outcome.status in accept.SENT_STATUSES and outcome.row_id:
        accept.spend_check(token, dispute_id)
        return RedirectResponse(f"{_hub(dispute_id)}/accept-claim/sent/{outcome.row_id}", status_code=303)
    if outcome.status == "already_sent":
        accept.spend_check(token, dispute_id)
        target = f"{_hub(dispute_id)}/accept-claim/sent/{outcome.row_id}" if outcome.row_id else _hub(dispute_id)
        return RedirectResponse(target, status_code=303)
    if outcome.status == "refused_cap":
        return _refuse(request, dispute_id, 403, "Not available now", outcome.reason)
    return _page(request, "features/accept_claim/check.html", dispute_id, 502 if outcome.status in ("read_failed", "failed") else 409,
                 check=check, token=token, phrase=shown, refusal=outcome.reason or "PayPal did not accept this. Nothing changed.")


@router.get(BASE + "/sent/{row_id}", response_class=HTMLResponse)
def sent_page(request: Request, dispute_id: str, row_id: str, client: SandboxClient = Depends(get_client)):
    refused = _guard(request, dispute_id, write=False)
    if refused:
        return refused
    row = accept.recheck(client, dispute_id, row_id)
    if row is None:
        return _refuse(request, dispute_id, 404, "Not found", "There is no such record for this dispute.")
    return _page(request, "features/accept_claim/sent.html", dispute_id, sent=row)
