from fastapi import APIRouter, Request
from copilot import config
from copilot.web.routes_disputes import templates
from copilot.features.declared_limits.service import LIMITS, flags_snapshot, rc44_snapshot

router = APIRouter()


@router.get("/declared-limits")
def declared_limits(request: Request):
    flags = flags_snapshot(config.feature_enabled)
    rc44 = rc44_snapshot()
    return templates.TemplateResponse(request, "features/declared_limits/page.html",
                                      {"flags": flags, "limits": LIMITS, "rc44": rc44,
                                       "model_name": config.model_name()})
