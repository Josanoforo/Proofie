"""Dedicated triage reads; the core entry performs no additional reads."""
from fastapi import APIRouter, Depends, Request

from copilot import config
from copilot.core.checklist import UnknownCountryError
from copilot.features.claims.model import safe_text
from copilot.features.triage import service
from copilot.web.routes_disputes import get_client, refusal_page, templates
from copilot.web.routes_package import _no_rule_set_line
from copilot.web.security import rejected_dispute_id

router = APIRouter()


@router.get('/disputes/{dispute_id}/triage')
def triage(request: Request, dispute_id: str, client=Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    try:
        verdict, checks, limitations, economics, rules = service.read(client, config.db_path(), dispute_id)
    except UnknownCountryError:
        return refusal_page(request, 400, 'No rule set available', _no_rule_set_line())
    return templates.TemplateResponse(request, 'features/triage/review.html',
        {'dispute_id': dispute_id, 'verdict': verdict, 'checks': checks, 'limitations': limitations,
         'economics': economics, 'rules': rules, 'safe_text': safe_text})
