"""Local observation view; opening this page makes no PayPal call."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.web import status
from copilot.web.routes_disputes import templates
from .evlog import LOG_FACTS
from .rate import LIMIT_PER_MINUTE
from .store import view

router = APIRouter()


@router.get("/events/state", response_class=HTMLResponse)
def state_page(request: Request):
    state = view(config.db_path())
    # Stored rows carry the status code only (never PayPal's action links), so the "PayPal is updating this dispute"
    # reading, which needs the links, cannot apply: a stand-in link keeps UNDER_REVIEW as "Under review by PayPal".
    rows = [{**row, "status_view": status.dispute_status({"status": row["status"], "action_links": {"stored-row": {}}})}
            for row in state["rows"]]
    return templates.TemplateResponse(request, "features/events/state.html",
                                      {**state, "rows": rows, "log_facts": LOG_FACTS, "limit": LIMIT_PER_MINUTE})
