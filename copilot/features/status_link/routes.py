from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from copilot.features.status_link import service
from copilot.web.routes_disputes import csrf_expired_page, refusal_page, templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, verify_csrf

router = APIRouter()
FIELD_NAMES = ("carrier", "tracking_number", "status", "registered", "description", "locale")


def _seller_page(request, template, status_code=200, **context):
    csrf = new_csrf_token()
    response = templates.TemplateResponse(request, f"features/status_link/{template}",
        {"csrf_token": csrf, "statuses": service.STATUSES, "key_ready": service.signing_key() is not None,
         "ttl_days": service.TTL_DAYS, **context}, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, csrf)
    return response


def _fields(form) -> dict:
    return {name: str(form.get(name, "")) for name in FIELD_NAMES}


@router.get("/status-link", response_class=HTMLResponse, name="status_link_form")
def status_link_form(request: Request):
    return _seller_page(request, "form.html", values={"locale": "en"}, error=None)


@router.post("/status-link/preview", response_class=HTMLResponse)
def status_link_preview(request: Request, csrf_token: str = Form(default=""), carrier: str = Form(default=""),
                        tracking_number: str = Form(default=""), status: str = Form(default=""),
                        registered: str = Form(default=""), description: str = Form(default=""),
                        locale: str = Form(default="en")):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    raw = {"carrier": carrier, "tracking_number": tracking_number, "status": status, "registered": registered,
           "description": description, "locale": locale}
    try:
        fields = service.clean(raw)
    except service.LinkError as exc:
        return _seller_page(request, "form.html", status_code=400, values=raw, error=str(exc))
    return _seller_page(request, "preview.html", view={**fields, "expires": service.expiry().date().isoformat()},
                        fields=fields, sha=service.payload_sha256(fields))


@router.post("/status-link/approve", response_class=HTMLResponse)
def status_link_approve(request: Request, csrf_token: str = Form(default=""), sha: str = Form(default=""),
                        carrier: str = Form(default=""), tracking_number: str = Form(default=""),
                        status: str = Form(default=""), registered: str = Form(default=""),
                        description: str = Form(default=""), locale: str = Form(default="en")):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    raw = {"carrier": carrier, "tracking_number": tracking_number, "status": status, "registered": registered,
           "description": description, "locale": locale}
    try:
        fields = service.clean(raw)
        if sha != service.payload_sha256(fields):
            raise service.LinkError("The page changed after you reviewed it. Start again so you approve what the buyer will see.")
        token = service.make_token(fields)
    except service.LinkError as exc:
        return refusal_page(request, 400, "Status link not created", str(exc),
                            back_url="/status-link", back_label="Start again")
    base = service.public_base() or f"{request.url.scheme}://{request.url.netloc}"
    return _seller_page(request, "created.html", link=f"{base}/s/{token}", on_public_base=bool(service.public_base()),
                        view={**fields, "expires": service.expiry().date().isoformat()})


@router.get("/s/{token}", response_class=HTMLResponse, name="status_link_public")
def status_link_public(request: Request, token: str):
    """Read-only, no cookie, no database, no PayPal call. Every failure is the same 404."""
    address = request.client.host if request.client else "unknown"
    if not service.LIMITER.allow(address):
        return PlainTextResponse("Too many requests. Try again in a few minutes.", status_code=429,
                                 headers={"Retry-After": "600", "Cache-Control": "no-store"})
    view = service.read_token(token)
    if view is None:
        response = PlainTextResponse("Not found.", status_code=404)
    else:
        response = templates.TemplateResponse(request, "features/status_link/public.html", {"view": view})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
