from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from copilot.core.disputes import SandboxClient
from copilot.features.call_script import service
from copilot.web.dispute_state import is_buyer_closed
from copilot.web.routes_disputes import get_client, refusal_page, templates
from copilot.web.security import rejected_dispute_id

router = APIRouter()


@router.get("/disputes/{dispute_id}/call-script", response_class=HTMLResponse)
def page(request: Request, dispute_id: str, lang: str = "en", client: SandboxClient = Depends(get_client)):
    if rejected_dispute_id(dispute_id):
        return refusal_page(request, 400, "Check the dispute id", "That dispute reference is not valid.", back_url="/disputes", back_label="Go back to disputes")
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    except Exception:
        return refusal_page(request, 503, "Could not read this case", "Proofie could not read this case. No call script was prepared.", back_url=f"/disputes/{dispute_id}", back_label="Go back to the dispute")
    from copilot.core.disputes import normalize
    from datetime import datetime, timezone
    norm = normalize(raw, datetime.now(timezone.utc))
    if str(norm.get("stage") or "").upper() != "INQUIRY" or is_buyer_closed((raw.get("dispute_outcome") or {}).get("outcome_code")):
        return refusal_page(request, 409, "No call script for this case", "This script is for an open inquiry only.", back_url=f"/disputes/{dispute_id}", back_label="Go back to the dispute")
    try:
        script = service.build(lang, norm.get("status"), norm.get("stage"))
    except ValueError as exc:
        return refusal_page(request, 400, "No call script for this case", str(exc), back_url=f"/disputes/{dispute_id}", back_label="Go back to the dispute")
    return templates.TemplateResponse(request, "features/call_script/index.html",
                                      {"dispute_id": dispute_id, "script": script})
