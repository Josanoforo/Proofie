from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core.disputes import normalize
from copilot.db import repo
from copilot.features.first_dispute_guide import service, store
from copilot.web.routes_disputes import get_client, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
templates.env.globals["first_dispute_guide_seen"] = store.seen


@router.get("/disputes/{dispute_id}/first-guide", response_class=HTMLResponse)
def guide_page(request: Request, dispute_id: str, client=Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    norm = normalize(raw, datetime.now(timezone.utc))
    outcome = raw.get("dispute_outcome") if isinstance(raw, dict) else None
    outcome_code = outcome.get("outcome_code") if isinstance(outcome, dict) else None
    guide = service.build_guide(config.RULES_COUNTRY, norm, outcome_code=outcome_code)
    if not store.seen():
        store.mark_seen()
    audit = [row for row in repo.list_audit(config.db_path(), dispute_id)
             if row["action"] == "packaging_retention_confirmed"]
    retention = audit[-1] if audit else None
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/first_dispute_guide/page.html",
        {"dispute_id": dispute_id, "guide": guide, "retention": retention, "csrf_token": token})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.post("/disputes/{dispute_id}/first-guide/packaging-retention")
def save_packaging_retention(request: Request, dispute_id: str,
                             retained: bool = Form(default=False), csrf_token: str = Form(default="")):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload this page and try again.", status_code=403)
    repo.log_action(config.db_path(), "vendor", "packaging_retention_confirmed", dispute_id,
                    {"retained": retained, "scope": "packaging_label_receipt_until_case_closed"})
    return RedirectResponse(f"/disputes/{dispute_id}/first-guide?retention_saved=1", status_code=303)
