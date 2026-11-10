"""The ten-check card, read-only: GET /disputes/{dispute_id}/packages/{package_id}/checked, and GET /disputes/{dispute_id}/checked
for the newest package of the dispute. Included by money_routes.py, so it sits under the outcome feature's flag.

The facts are gathered with the package page's own readers (the same checklist call, the same send record, the same approval
comparison), so the card and the package page cannot disagree. Nothing is written to PayPal."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core.checklist import UnknownCountryError
from copilot.core.disputes import SandboxClient, normalize
from copilot.db import repo
from copilot.features.outcome import checked
from copilot.web import routes_package, status, timefmt
from copilot.web.routes_disputes import get_client, refusal_page, templates
from copilot.web.security import rejected_dispute_id

router = APIRouter()


def build_card(client, db_path, dispute_id: str, package: dict) -> dict:
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    dispute = normalize(raw, datetime.now(timezone.utc))
    try:
        checks, _context = routes_package._checks_for(dispute, dispute_id, client, db_path)  # noqa: SLF001 - the package page's reader
    except UnknownCountryError:
        checks = []
    claims = repo.list_claims(db_path, package["id"])
    approval = repo.get_latest_valid_approval(db_path, package["id"])
    attachments = repo.list_attachments(db_path, package["id"])
    record = status.send_record(db_path, dispute_id, package)
    send_summary = routes_package._send_summary(db_path, dispute_id, package, claims, attachments, approval, record)  # noqa: SLF001
    card = checked.rows(
        checks=checks, claims=claims, package=package, approval=approval, approval_stale=send_summary["approval_stale"],
        approval_cleared=send_summary["approval_cleared"],
        approved_on=timefmt.display(approval["approved_at"]) if approval else None, record=record,
        received_on=timefmt.display(record.confirmed_at) if record.confirmed_at else None,
        due=timefmt.due_display(dispute) if dispute.get("status") == "WAITING_FOR_SELLER_RESPONSE" else None)
    return {"rows": card, "summary": checked.summary(card)}


@router.get("/disputes/{dispute_id}/checked")
def newest_package_card(request: Request, dispute_id: str):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    packages = repo.list_packages(config.db_path(), dispute_id)
    if not packages:
        return refusal_page(request, 404, "Nothing was checked yet", "This dispute has no package yet, so there is nothing to list.",
                            back_url=f"/disputes/{dispute_id}", back_label="Go back to the dispute")
    return RedirectResponse(f"/disputes/{dispute_id}/packages/{packages[-1]['id']}/checked", status_code=303)


@router.get("/disputes/{dispute_id}/packages/{package_id}/checked", response_class=HTMLResponse)
def card_page(request: Request, dispute_id: str, package_id: int, client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    package, missing = routes_package._own_package(request, dispute_id, package_id)  # noqa: SLF001
    if missing:
        return missing
    ctx = {"dispute_id": dispute_id, "package": package, **build_card(client, config.db_path(), dispute_id, package)}
    response = templates.TemplateResponse(request, "features/outcome/checked.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response
