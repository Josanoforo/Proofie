"""GET /replay/<case>: the labelled, dated replay page. Registered only with COPILOT_FEATURE_REPLAY on AND the hosted demo on
(copilot/app.py). Read only: no PayPal call, no database access, no model call, no form."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from copilot.features.replay import service
from copilot.web.routes_disputes import refusal_page, templates
from copilot.web.security import rejected_dispute_id

router = APIRouter()


@router.get("/replay/{case}", response_class=HTMLResponse)
def replay_page(request: Request, case: str):
    # The one fixed data-only alias is not a PayPal dispute ID; all other paths keep the existing shape guard.
    if case != service.DATA_ONLY_CASE:
        bad = rejected_dispute_id(case)
        if bad:
            return bad
    data = service.load(case)
    if data is None:
        return refusal_page(request, 404, "No recorded run for this case",
                            "There is no recording of the local assistant for this case.", back_url="/disputes",
                            back_label="All disputes")
    response = templates.TemplateResponse(request, "features/replay/replay.html", {"replay": service.view(data)})
    response.headers["Cache-Control"] = "no-store"
    return response
