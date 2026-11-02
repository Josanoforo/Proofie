"""Opt-in CSRF-protected local preparation; observation comes from the server."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response
from copilot import config
from copilot.features.local_autopilot import observations, preparation
from copilot.web.security import rejected_dispute_id, verify_csrf

router = APIRouter()


@router.post("/disputes/{dispute_id}/local-preparation")
def prepare(request: Request, dispute_id: str, csrf_token: str = Form(default=""),
            observation_token: str = Form(default="")):
    if not config.feature_enabled("local_autopilot") or not config.feature_enabled("local_preparation"):
        return Response("Not found", status_code=404)
    if not verify_csrf(request, csrf_token):
        return Response("Reload the case before continuing.", status_code=403)
    bad = rejected_dispute_id(dispute_id)
    if bad: return bad
    observation = observations.get(observation_token, dispute_id)
    result = preparation.prepare_known_case(config.db_path(), observation)
    state = result.get("state", "unavailable")
    allowed = {"ready", "needs_input", "stale", "in_progress", "cooldown", "not_eligible", "human_review", "unavailable"}
    if state not in allowed: state = "unavailable"
    return RedirectResponse(f"/disputes/{dispute_id}/workspace/prepare?local_preparation={state}#local-preparation", status_code=303)
