from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.db import repo
from copilot.features.second_opinion import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()


def _snapshot(dispute_id: str, package_id: int):
    package = repo.get_package(config.db_path(), package_id)
    if package is None or package["dispute_id"] != dispute_id:
        raise ValueError("Package was not found for this case.")
    claims = repo.list_claims(config.db_path(), package_id)
    attachments = repo.list_attachments(config.db_path(), package_id)
    return {"dispute_id": dispute_id, "package_id": package_id, "version": package["version"],
            "claims": [{"text": str(row["text"]), "basis": str(row.get("basis") or "seller statement")} for row in claims],
            "attachments": [str(row["filename"]) for row in attachments],
            "read_only_notice": "Read-only snapshot. This reviewer cannot edit or approve it."}


@router.post("/disputes/{dispute_id}/packages/{package_id}/second-opinion", response_class=HTMLResponse)
def create(request: Request, dispute_id: str, package_id: int, csrf_token: str = Form(default="")):
    if rejected_dispute_id(dispute_id):
        return HTMLResponse("Invalid case ID", status_code=400)
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload the package page before creating a link.", status_code=403)
    try:
        snapshot = _snapshot(dispute_id, package_id)
        token, row = service.create_link(dispute_id, package_id, snapshot)
    except (OSError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    link = f"/second-opinion/{token}"
    response = templates.TemplateResponse(request, "features/second_opinion/created.html",
        {"link": link, "expires_at": row["expires_at"], "dispute_id": dispute_id})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/second-opinion/{token}", response_class=HTMLResponse)
def review(request: Request, token: str):
    try:
        row = service.open_view(token)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=410)
    csrf = new_csrf_token()
    response = templates.TemplateResponse(request, "features/second_opinion/review.html",
        {"snapshot": row["snapshot"], "token": token, "csrf_token": csrf, "error": ""})
    set_csrf_cookie(response, csrf)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/second-opinion/{token}/note", response_class=HTMLResponse)
def note(request: Request, token: str, csrf_token: str = Form(default=""), reviewer_note: str = Form(default="")):
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload the review before saving a note.", status_code=403)
    try:
        record = service.save_note(token, reviewer_note)
    except (OSError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=409)
    return templates.TemplateResponse(request, "features/second_opinion/note_saved.html", {"record": record})
