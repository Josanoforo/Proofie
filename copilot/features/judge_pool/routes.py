"""Judge mode pages. Hosted demo only, flag COPILOT_FEATURE_JUDGE_POOL, non-empty allowlist COPILOT_JUDGE_POOL; otherwise 404.
The only PayPal write behind these routes is PayPal's escalate action (service.start_case). No route here, and no import, reaches
adjudicate, require-evidence, win or lose."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core import disputes
from copilot.features.judge_pool import service
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
COOKIE = "proofie_judge"
CHECK_AGAIN = "PayPal takes a few minutes to move the case. Press Check again."
EMPTY = "No case is ready right now. The judge guide describes the local path."


def write_client():
    """The client whose log is the helper's file; tests replace it."""
    return disputes.SandboxClient(service.LOG_NAME)


def _off(request: Request):
    return refusal_page(request, 404, "Page not found", "This page does not exist.")


def _session(request: Request) -> tuple[str, str]:
    """(cookie value, session hash). A visitor without the cookie gets a fresh one on this response."""
    cookie = request.cookies.get(COOKIE) or ""
    fresh = not (20 <= len(cookie) <= 80)
    if fresh:
        cookie = secrets.token_urlsafe(24)
    request.state.judge_cookie_fresh = fresh
    return cookie, service.session_hash(cookie)


def _finish(request: Request, response, cookie: str):
    if getattr(request.state, "judge_cookie_fresh", False):
        response.set_cookie(COOKIE, cookie, httponly=True, samesite="strict", secure=config.hosted_demo(), max_age=7 * 24 * 3600)
    response.headers["Cache-Control"] = "no-store"
    return response


def _page(request: Request, name: str, cookie: str, context: dict, status_code: int = 200):
    token = new_csrf_token()
    response = templates.TemplateResponse(request, f"features/judge_pool/{name}", {**context, "csrf_token": token},
                                          status_code=status_code)
    set_csrf_cookie(response, token)
    return _finish(request, response, cookie)


@router.get("/judge", response_class=HTMLResponse, name="judge_pool")
def judge_list(request: Request, client=Depends(get_client)):
    if not service.enabled():
        return _off(request)
    cookie, session = _session(request)
    held = service.holders()
    rows = []
    for dispute_id in service.pool_ids():
        try:
            _raw, norm = service.read_case(client, dispute_id)
        except Exception:  # noqa: BLE001 - one unreadable case must not hide the others
            continue
        state = service.case_state(norm, holder=held.get(dispute_id), session=session)
        if state == service.UNAVAILABLE and config.feature_enabled("accept_claim"):
            from copilot.features.accept_claim import service as accept
            if accept.was_accepted(dispute_id):          # the claim was accepted from here: PayPal closed the case
                state = service.USED
        if state == service.UNAVAILABLE:
            continue
        rows.append({"dispute_id": dispute_id, "state": state, "label": service.LABELS[state],
                     "pickable": state in (service.START, service.ANSWER)})
    ready = any(r["pickable"] for r in rows)
    return _page(request, "list.html", cookie, {"rows": rows, "ready": ready, "empty": EMPTY})


@router.get("/judge/cases/{dispute_id}", response_class=HTMLResponse, name="judge_pick")
def judge_pick(request: Request, dispute_id: str, client=Depends(get_client)):
    """The pick: reads the case, takes the lock, shows the exact request. No PayPal write."""
    if not service.enabled():
        return _off(request)
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    cookie, session = _session(request)
    if dispute_id not in service.pool_ids():
        return refusal_page(request, 404, "Case not in the pool", "This case is not offered here.", back_url="/judge", back_label="Back to the cases")
    try:
        _raw, norm = service.read_case(client, dispute_id)
        state = service.case_state(norm, holder=service.holder_of(dispute_id), session=session)
        if state == service.TAKEN:
            raise service.Refused("Another visitor took this case. Pick another one.")
        if state in (service.USED, service.UNAVAILABLE):
            raise service.Refused("This case is not available now. Pick another one.")
        if not service.take(dispute_id, session):
            raise service.Refused("Another visitor took this case. Pick another one.")
    except service.Refused as exc:
        return refusal_page(request, 409, "Case not available", str(exc), back_url="/judge", back_label="Back to the cases")
    except Exception:  # noqa: BLE001
        return refusal_page(request, 503, "PayPal could not be read", "Nothing was changed. Try again in a moment.", back_url="/judge", back_label="Back to the cases")
    if state == service.ANSWER:
        return _finish(request, RedirectResponse(f"/disputes/{dispute_id}", status_code=303), cookie)
    return _page(request, "case.html", cookie, {"dispute_id": dispute_id, "note": service.NOTE,
                                                "fingerprint": service.request_sha(dispute_id)[:12]})


@router.post("/judge/cases/{dispute_id}/start", response_class=HTMLResponse, name="judge_start")
def judge_start(request: Request, dispute_id: str, csrf_token: str = Form(default=""), client=Depends(get_client)):
    if not service.enabled():
        return _off(request)
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    cookie, session = _session(request)
    try:
        result = service.start_case(client, write_client(), dispute_id, session)
    except service.Refused as exc:
        return refusal_page(request, 409, "Case not started", str(exc), back_url="/judge", back_label="Back to the cases")
    except Exception:  # noqa: BLE001
        return refusal_page(request, 503, "PayPal could not be reached", "Nothing more was sent. Check the case before trying again.",
                            back_url=f"/judge/cases/{dispute_id}/check", back_label="Check the case")
    if result["state"] == service.ANSWER:
        return _finish(request, RedirectResponse(f"/disputes/{dispute_id}", status_code=303), cookie)
    return _page(request, "result.html", cookie, {"dispute_id": dispute_id, "note": CHECK_AGAIN, "waiting": True})


@router.get("/judge/cases/{dispute_id}/check", response_class=HTMLResponse, name="judge_check")
def judge_check(request: Request, dispute_id: str, client=Depends(get_client)):
    if not service.enabled():
        return _off(request)
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    cookie, session = _session(request)
    if dispute_id not in service.pool_ids() or service.holder_of(dispute_id) != session:
        return refusal_page(request, 404, "Case not found", "This case is not yours in this session.", back_url="/judge", back_label="Back to the cases")
    try:
        _raw, norm = service.read_case(client, dispute_id)
    except Exception:  # noqa: BLE001
        return refusal_page(request, 503, "PayPal could not be read", "Nothing was changed. Try again in a moment.",
                            back_url=f"/judge/cases/{dispute_id}/check", back_label="Check again")
    if service.case_state(norm, holder=session, session=session) == service.ANSWER:
        return _finish(request, RedirectResponse(f"/disputes/{dispute_id}", status_code=303), cookie)
    return _page(request, "result.html", cookie, {"dispute_id": dispute_id, "note": CHECK_AGAIN, "waiting": True})


def register(app) -> None:
    app.include_router(router)
