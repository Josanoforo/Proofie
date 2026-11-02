from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.features.two_clocks.service import compare
from copilot.web.routes_disputes import get_client, templates
from copilot.web.security import rejected_dispute_id
from copilot.core.disputes import normalize
from datetime import datetime, timezone
import re

router = APIRouter()


@router.get("/disputes/{dispute_id}/two-clocks", response_class=HTMLResponse)
def two_clocks_page(request: Request, dispute_id: str, client=Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    norm = normalize(raw, datetime.now(timezone.utc))
    capture = next((item for item in norm.get("seller_transaction_ids", [])
                    if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9]{17}", item)), None)
    estimate = None
    if capture and config.feature_enabled("sale_readiness"):
        from copilot.features.sale_readiness import service as readiness
        saved = readiness.load_case_terms(dispute_id, capture)
        if saved and isinstance(saved.get("terms"), dict):
            estimate = saved["terms"].get("expected_date")
    result = compare(estimate, norm.get("seller_response_due_date"))
    response = templates.TemplateResponse(request, "features/two_clocks/page.html",
                                          {"dispute_id": dispute_id, "result": result})
    response.headers["Cache-Control"] = "no-store"
    return response
