"""GET /account only. No POST anywhere in this feature. Registered in copilot/app.py behind the flag."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.core.disputes import SandboxClient
from copilot.features.account_cliff import counting, service, trajectory
from copilot.features.account_cliff.network_context import NETWORK_FACTS, WON_DISPUTE_SENTENCE
from copilot.features.account_cliff.rules import ALL_CONSTANTS, SCHEDULE_LIMITATION, cents_to_usd_text
from copilot.web.routes_disputes import get_client, templates
from copilot.web import timefmt

router = APIRouter()


def _usd(cents: int | None) -> str:
    return "Not available" if cents is None else f"{cents_to_usd_text(cents)} USD"


def _count(value: int | None) -> str:
    return "Not available" if value is None else str(value)


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, client: SandboxClient = Depends(get_client)):
    summary = (service.compute(client, include_exposure=True)
               if config.feature_enabled("explainable_queue") else service.compute(client))
    if summary.high_volume is None:
        status = "Not available: the counts could not be read, see the limitations below"
    elif summary.sales <= 100:
        status = f"Not high volume: needs more than 100 sales in the window ({summary.sales} so far)"
    elif summary.high_volume:
        status = "At or above the 1.50 % threshold: the high-volume fee applies"
    else:
        noun = "dispute" if summary.disputes_to_threshold == 1 else "disputes"
        status = f"Below the 1.50 % threshold: {summary.disputes_to_threshold} more {noun} would reach it"
    path = trajectory.compute_path_safe(client) if config.feature_enabled("account_cliff_path") else None
    return templates.TemplateResponse(request, "features/account_cliff/account.html", {
        "cliff_path": path, "cliff_headline": trajectory.headline(path) if path else None,
        "cliff_rows": trajectory.rows(path) if path else [], "cliff_notes": trajectory.notes(path) if path else [],
        "account_exposure": summary.account_exposure, "account_exposure_error": summary.account_exposure_error,
        "schedule_limitation": SCHEDULE_LIMITATION, "summary": summary, "status": status, "line": service.line_text(summary),
        "rate": counting.rate_text(summary.rate_bps), "sales": _count(summary.sales),
        "disputes": _count(summary.disputes), "fee_now": _usd(summary.fee_now_cents),
        "fee_if_high": _usd(summary.fee_if_high_cents), "constants": ALL_CONSTANTS,
        "network_facts": NETWORK_FACTS, "won_sentence": WON_DISPUTE_SENTENCE,
        "computed_at": timefmt.fmt(summary.computed_at)})
