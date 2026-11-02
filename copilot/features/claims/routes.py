"""Standalone source review for the optional claims flag."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from copilot import config
from copilot.core.checklist import UnknownCountryError
from copilot.core.disputes import normalize
from copilot.db import repo
from copilot.features.claims.adversarial import adversarial_review
from copilot.features.claims.model import safe_text
from copilot.features.claims.service import compose, document_names
from copilot.web.routes_disputes import get_client, refusal_page, templates
from copilot.web.routes_package import _checks_for, _no_rule_set_line
from copilot.web.security import rejected_dispute_id

router = APIRouter()


@router.get("/disputes/{dispute_id}/packages/{package_id}/claims-review")
def review(request: Request, dispute_id: str, package_id: int, client=Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    norm = normalize(raw, datetime.now(timezone.utc))
    try:
        checks, _ = _checks_for(norm, dispute_id, client, config.db_path())
        claims = compose(config.db_path(), dispute_id, package_id, checks, norm)
        adversarial = adversarial_review(config.db_path(), dispute_id, package_id, checks, norm)
    except UnknownCountryError:
        return refusal_page(request, 400, "No rule set available", _no_rule_set_line())
    except ValueError:
        return refusal_page(request, 404, "Package unavailable", "Open a package belonging to this dispute.")
    return templates.TemplateResponse(request, "features/claims/review.html",
                                      {"dispute_id": dispute_id, "package_id": package_id,
                                       "package_version": (repo.get_package(config.db_path(), package_id) or {}).get("version"),
                                       "claims": claims, "adversarial_review": adversarial, "safe_text": safe_text,
                                       "document_names": document_names(config.db_path(), dispute_id, claims, redact_private=True)})
