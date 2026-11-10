"""Two read-only pages under the outcome feature's flag (COPILOT_FEATURE_OUTCOME): the money of one case and the totals.

  GET /disputes/{dispute_id}/outcome/money   one GET of the dispute: kept, lost or still held, with the amount
  GET /money                                 every case the app may list (the hosted demo: its pool), totals per currency

Neither page writes to PayPal. The facts come from copilot/features/outcome/money.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from copilot.core.disputes import SandboxClient
from copilot.features.outcome import money
from copilot.web.routes_disputes import get_client, templates
from copilot.web.security import rejected_dispute_id

router = APIRouter()


def _page(request: Request, name: str, ctx: dict):
    response = templates.TemplateResponse(request, name, ctx)
    response.headers["Cache-Control"] = "no-store"          # money read from PayPal: never shown from the cache
    return response


@router.get("/disputes/{dispute_id}/outcome/money", response_class=HTMLResponse)
def case_money_page(request: Request, dispute_id: str, client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    view = money.case_view(client, dispute_id)
    return _page(request, "features/outcome/money_case.html",
                 {"dispute_id": dispute_id, "case": view.case, "limitation": view.limitation, "fees_label": money.FEES_LABEL})


@router.get("/money", response_class=HTMLResponse)
def account_money_page(request: Request, client: SandboxClient = Depends(get_client)):
    return _page(request, "features/outcome/money.html", {"view": money.account_view(client), "fees_label": money.FEES_LABEL})

from copilot.features.outcome.checked_routes import router as checked_router  # noqa: E402 - the ten-check card, same flag

router.include_router(checked_router)
