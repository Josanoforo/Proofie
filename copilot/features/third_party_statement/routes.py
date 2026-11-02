from __future__ import annotations

import hashlib
import re
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from copilot import config
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.features.third_party_statement import service, store
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
DRAWER_NAME = "third_party_statements.sqlite3"
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
TRANSFER_ACTION = "third_party_statement_copied_to_case_document"


def _drawer_path():
    return config.data_dir() / DRAWER_NAME


def _render(request, dispute_id, *, error=None, status_code=200):
    token = new_csrf_token()
    statements = store.list_for_case(_drawer_path(), dispute_id)
    packages = repo.list_packages(config.db_path(), dispute_id)
    latest = max(packages, key=lambda row: (row["version"], row["id"])) if packages else None
    response = templates.TemplateResponse(request, "features/third_party_statement/index.html", {
        "dispute_id": dispute_id, "statements": statements, "csrf_token": token,
        "error": error, "latest_package": latest})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    response.status_code = status_code
    return response


@router.get("/disputes/{dispute_id}/third-party-statements", response_class=HTMLResponse)
def statement_drawer(request: Request, dispute_id: str):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    return _render(request, dispute_id)


@router.post("/disputes/{dispute_id}/third-party-statements", response_class=HTMLResponse)
async def record_statement(request: Request, dispute_id: str, source_party: str = Form(default=""),
                           kind: str = Form(default="statement"), statement: str = Form(default=""),
                           file: UploadFile | None = File(default=None), csrf_token: str = Form(default="")):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload this page and try again.", status_code=403)
    try:
        source_party = service.validate_source(source_party)
        if kind == "statement":
            payload = service.validate_text(statement).encode("utf-8")
            content_type, filename = "text/plain; charset=utf-8", None
        elif kind == "photo" and file is not None:
            payload = await file.read(service.MAX_IMAGE_BYTES + 1)
            content_type = service.validate_image(payload)
            filename = SAFE_NAME.sub("_", (file.filename or "third-party-photo").replace("\\", "/").rsplit("/", 1)[-1])[:120]
            if not filename:
                filename = "third-party-photo"
        else:
            raise ValueError("Choose a third-party statement or a JPEG/PNG photo.")
        store.add(_drawer_path(), dispute_id, source_party, kind, content_type, filename, payload)
    except (ValueError, OSError) as exc:
        return _render(request, dispute_id, error=str(exc), status_code=400)
    return RedirectResponse(f"/disputes/{dispute_id}/third-party-statements?recorded=1", status_code=303)


@router.get("/disputes/{dispute_id}/third-party-statements/{statement_id}/original")
def statement_original(request: Request, dispute_id: str, statement_id: int):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    row = store.get_for_case(_drawer_path(), dispute_id, statement_id)
    if row is None or row["kind"] != "photo" or row["content_type"] not in service.IMAGE_TYPES:
        return HTMLResponse("Statement not found.", status_code=404)
    return Response(row["payload"], media_type=row["content_type"], headers={
        "Content-Disposition": f'attachment; filename="{row["filename"] or "third-party-photo"}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/disputes/{dispute_id}/third-party-statements/{statement_id}/case-document")
def copy_photo_to_case_document(request: Request, dispute_id: str, statement_id: int,
                                csrf_token: str = Form(default="")):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload this page and try again.", status_code=403)
    row = store.get_for_case(_drawer_path(), dispute_id, statement_id)
    if row is None or row["kind"] != "photo" or row["content_type"] not in service.IMAGE_TYPES:
        return HTMLResponse("Only a stored third-party photo can be copied to case documents.", status_code=409)
    db_path = config.db_path()
    payload = row["payload"]
    sha = hashlib.sha256(payload).hexdigest()
    filename = "third-party-unverified-" + str(statement_id) + "-" + (row["filename"] or "photo")
    write_raw(config.uploads_dir(), sha, payload)
    document_id = repo.insert_document(db_path, dispute_id, filename, row["content_type"], len(payload), sha,
        f"Third-party statement from {row['source_party']}; uncorroborated. Seller copied the photo from the separate local drawer at {row['recorded_at']}.",
        False)
    repo.set_document_excluded(db_path, document_id, True)
    repo.log_action(db_path, "vendor", TRANSFER_ACTION, dispute_id,
                    {"document_id": document_id, "statement_id": statement_id,
                     "source_party": row["source_party"], "uncorroborated": True})
    packages = repo.list_packages(db_path, dispute_id)
    latest = max(packages, key=lambda item: (item["version"], item["id"])) if packages else None
    target = (f"/disputes/{dispute_id}/packages/{latest['id']}?" + urlencode({"done": "document_added_package", "doc": document_id}) + "#attachments") if latest else f"/disputes/{dispute_id}#documents"
    return RedirectResponse(target, status_code=303)
