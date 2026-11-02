"""/webhooks routes (exactly two). Registered in copilot/app.py behind COPILOT_FEATURE_WEBHOOKS (lazy import inside that flag
block). PayPal reaches the listener through a tunnel agent on this machine that forwards to 127.0.0.1, so the app's
loopback-only middleware still holds.

POST /webhooks/paypal has NO CSRF check, on purpose: the caller is PayPal, not a browser carrying our double-submit cookie.
With the verification sub-flag enabled, authentication is the signature verified with PayPal (verify.py). Disabled local
deliveries remain unverified and perform no client calls; hosted refuses them. The
dispute state is read by our own GET, never taken from the event. This is the only POST of the app without the cookie check.
The route reads the raw bytes, answers the status of service.handle_event and a one-line JSON, and nothing else."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from copilot import config
from copilot.core.disputes import SandboxClient
from copilot.features.webhooks import service, store
from copilot.web.routes_disputes import get_client, templates

router = APIRouter()
NOTICE = ("Every notice is checked with PayPal. The dispute status shown comes from Proofie's own request to PayPal, "
          "never from the notice.")
NOTICE_UNVERIFIED = ("Signature checks are off, so these notices are not verified and Proofie did not ask PayPal about them.")
NOT_CONFIGURED = "Proofie is not set up to receive PayPal notices."
NOT_CONFIGURED_DETAIL = "Set COPILOT_WEBHOOK_ID to the webhook id from the PayPal dashboard."


def webhook_id() -> str | None:
    """The webhook id from the environment (read on every call), stripped; None when empty or unset."""
    return (os.environ.get("COPILOT_WEBHOOK_ID") or "").strip() or None


def verification_client(request: Request):
    """Do not construct a client when verification is disabled."""
    enabled = config.feature_enabled("webhooks_verify")
    if config.hosted_demo() and not enabled:
        raise HTTPException(401, "Webhook receiver requires verification in hosted mode")
    if not enabled:
        return None
    factory = request.app.dependency_overrides.get(get_client, get_client)
    return factory()


@router.post("/webhooks/paypal")
async def receive(request: Request, client: SandboxClient = Depends(verification_client)):
    raw = await request.body()
    handled = await run_in_threadpool(service.handle_event, client, config.db_path(), request.headers, raw, webhook_id())
    return JSONResponse({"verification": handled.verification, "readback_status": handled.readback_status},
                        status_code=handled.http_status)


@router.get("/webhooks/events", response_class=HTMLResponse)
def events_page(request: Request):
    events = [{**e, "body_prefix": (e["body_sha256"] or "")[:12]} for e in store.list_events(100)]
    return templates.TemplateResponse(request, "features/webhooks/events.html", {
        "events": events, "notice": NOTICE if config.feature_enabled("webhooks_verify") else NOTICE_UNVERIFIED,
        "configured": webhook_id() is not None, "not_configured": NOT_CONFIGURED, "not_configured_detail": NOT_CONFIGURED_DETAIL})
