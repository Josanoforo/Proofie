"""The seller's record of one case: a read-only page over rows the app already keeps. No PayPal call, no write."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.features.case_history import service
from copilot.web.routes_disputes import templates
from copilot.web.security import rejected_dispute_id

router = APIRouter()


@router.get("/disputes/{dispute_id}/history", response_class=HTMLResponse)
def history(request: Request, dispute_id: str):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    view = service.build(config.db_path(), dispute_id)
    response = templates.TemplateResponse(request, "features/case_history/history.html",
                                          {"dispute_id": dispute_id, **view})
    response.headers["Cache-Control"] = "no-store"
    return response
