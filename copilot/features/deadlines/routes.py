"""PayPal due dates: one read per visit (no automatic refresh); GETs share the owned rate budget."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from copilot.web.routes_disputes import get_client, templates
from copilot.web import timefmt
from copilot.features.events.rate import BudgetUnavailable
from copilot.core.disputes import list_cut_line
from .service import read, WARNING_DAYS

router = APIRouter()


@router.get("/deadlines", response_class=HTMLResponse)
def deadlines_page(request: Request, client=Depends(get_client)):
    now, error = datetime.now(timezone.utc), None
    try:
        rows, pages = read(client, now)
    except BudgetUnavailable:
        rows, pages = [], 0
        error = ("PayPal was not read this time: Proofie limits how often it reads PayPal. "
                 "Nothing was changed. Try again in a minute with Refresh from PayPal.")
    response = templates.TemplateResponse(request, "features/deadlines/watch.html", {
        "rows": rows, "pages": pages, "list_cut_line": list_cut_line(rows), "checked_at": timefmt.fmt(now) if not error else None,
        "warning_days": WARNING_DAYS, "read_error": error}, status_code=503 if error else 200)
    response.headers["Cache-Control"] = "no-store"
    return response
