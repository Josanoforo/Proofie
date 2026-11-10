"""Document upload: type/size validated by content, filenames sanitised, text extracted
when possible and marked honestly when not. CSRF-protected - this is the app's first
real POST form.
"""
from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from copilot import config
from copilot.core.content_type import (PROBLEM_NO_FILE, PROBLEM_UNREADABLE, detect_content_type, problem_message,
                                       validate_upload)
from copilot.core.document_ingest import PdfReaderUnavailable, extract_text
from copilot.core.raw_storage import read_raw, write_raw
from copilot.db import repo
from copilot.web import status
from copilot.web.routes_disputes import csrf_expired_page, refusal_page
from copilot.web.security import rejected_dispute_id, verify_csrf

router = APIRouter()

_ORIGINAL_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif"}

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_filename(name: str | None) -> str:
    """Strips any path components (never trust a client-supplied path) and any character outside a safe
    allowlist. Never returns an empty string (falls back to a generic name) so a stored filename is
    always safe to display and to use as part of a path if ever needed."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARS.sub("_", name).strip("._") or "upload"
    return name[:200]


def _upload_refusal(request: Request, dispute_id: str, problems: list):
    """One fixed sentence per problem; nothing the browser sent (name, type, size) is echoed."""
    return refusal_page(request, 400, "This file could not be uploaded", "Could not upload this file:",
                        items=list(problems), back_url=f"/disputes/{dispute_id}/workspace/prepare#documents",
                        back_label="Go back to the dispute")


def _upload_landing(db_path, dispute_id: str, doc_id: int) -> str:
    """Where a saved upload goes. With an editable package (the latest one has no confirmed, pending or
    unconfirmed attempt) the seller lands on that package's file list to attach it; otherwise on the dispute's
    documents. Display routing only: nothing is attached here."""
    package, record = status.latest_package_record(db_path, dispute_id, repo.list_packages(db_path, dispute_id))
    if package is not None and record is not None and record.state in ("none", "refused", "blocked", "abandoned"):
        query = urlencode({"done": "document_added_package", "doc": doc_id})
        return f"/disputes/{dispute_id}/packages/{package['id']}?{query}#attachments"
    return f"/disputes/{dispute_id}/workspace/prepare?{urlencode({'done': 'document_added', 'doc': doc_id})}#documents"


@router.post("/disputes/{dispute_id}/documents", response_class=HTMLResponse)
async def upload_document(request: Request, dispute_id: str, csrf_token: str = Form(...),
                          file: UploadFile | None = None, then: str = Form("")):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    # Validate dispute_id (untrusted path segment) before it reaches a redirect Location or
    # any further use - the same expression every other web route and the MCP validate against.
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id

    if file is None or (not file.filename and not await file.read(1)):
        return _upload_refusal(request, dispute_id, [problem_message(PROBLEM_NO_FILE)])
    await file.seek(0)
    try:
        data = await file.read()
    except Exception:   # noqa: BLE001 - a broken upload stream is the seller's to retry, never a raw error page
        return _upload_refusal(request, dispute_id, [problem_message(PROBLEM_UNREADABLE)])
    safe_name = sanitize_filename(file.filename)
    problems = validate_upload(safe_name, data)
    if problems:
        return _upload_refusal(request, dispute_id, problems)

    content_type = detect_content_type(data)
    try:
        extracted_text, extraction_ok = extract_text(content_type, data)
    except PdfReaderUnavailable:
        # a problem with this install, not with the file. Nothing is stored (a stored row would say
        # "unreadable" and send the seller to type values for a document that is fine).
        repo.log_action(config.db_path(), "system", "upload_pdf_reader_unavailable", dispute_id,
                        {"filename": safe_name})
        return refusal_page(request, 503, "The PDF reader is not available",
                            "The PDF reader is not available on this computer, so this PDF was not added. "
                            "Nothing was changed. Reinstall the app's requirements (README, Install) and try "
                            "again; image files still work.",
                            back_url=f"/disputes/{dispute_id}", back_label="Back to the dispute")
    sha256 = hashlib.sha256(data).hexdigest()
    write_raw(config.uploads_dir(), sha256, data)   # the original bytes, for a real send to attach later
    db_path = config.db_path()
    doc_id = repo.insert_document(db_path, dispute_id, safe_name, content_type, len(data), sha256,
                                  extracted_text, extraction_ok)
    repo.log_action(db_path, "vendor", "upload_document", dispute_id,
                    {"document_id": doc_id, "filename": safe_name, "extraction_ok": extraction_ok})
    if then == "prepare" and config.feature_enabled("seller_shortcuts") and not repo.list_packages(db_path, dispute_id):
        # "Upload and prepare the package" (COPILOT_FEATURE_SELLER_SHORTCUTS): the same package creation as the Generate button,
        # run straight after the upload instead of a second page and a second click. Nothing is approved or sent here.
        from starlette.concurrency import run_in_threadpool
        from copilot.web import routes_package
        from copilot.web.routes_disputes import get_client
        client = request.app.dependency_overrides.get(get_client, get_client)()
        return await run_in_threadpool(routes_package.create_package, request, dispute_id, csrf_token, 0, client)
    return RedirectResponse(_upload_landing(db_path, dispute_id, doc_id), status_code=303)


@router.get("/disputes/{dispute_id}/documents/{document_id}/original")
def original_document(request: Request, dispute_id: str, document_id: int):
    """Read the original bytes only through a document belonging to this case.

    Extracted text remains separate metadata; this endpoint never reads an arbitrary path.
    """
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    if config.hosted_demo():                   # the shared demo never serves stored originals to other visitors
        return refusal_page(request, 404, "Document not found", "That document could not be found.",
                            back_url=f"/disputes/{dispute_id}/workspace/prepare#documents", back_label="Back to this dispute")
    document = repo.get_document(config.db_path(), document_id)
    if document is None or document["dispute_id"] != dispute_id:
        return refusal_page(request, 404, "Document not found", "That document could not be found.",
                            back_url=f"/disputes/{dispute_id}/workspace/prepare#documents", back_label="Back to this dispute")
    sha256 = document.get("sha256") or ""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return refusal_page(request, 404, "Document not found", "That document could not be found.",
                            back_url=f"/disputes/{dispute_id}/workspace/prepare#documents", back_label="Back to this dispute")
    raw = read_raw(config.uploads_dir(), sha256)
    if raw is None or hashlib.sha256(raw).hexdigest() != sha256:
        return refusal_page(request, 404, "Document not found", "That document could not be found.",
                            back_url=f"/disputes/{dispute_id}/workspace/prepare#documents", back_label="Back to this dispute")
    content_type = document.get("content_type")
    if content_type not in _ORIGINAL_TYPES:
        content_type = "application/octet-stream"
    filename = sanitize_filename(document.get("filename"))
    return Response(raw, media_type=content_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })


def _return_target(db_path, dispute_id: str, return_to) -> int | None:
    """The package a check-row exclude came from: only the server-built 'package:{pid}' of THIS dispute counts, any
    other value (a URL, another dispute's package, junk) is ignored. Returns the package id or None."""
    match = re.fullmatch(r"package:([0-9]{1,18})", return_to) if isinstance(return_to, str) else None
    if match is None:
        return None
    package = repo.get_package(db_path, int(match.group(1)))
    return package["id"] if package is not None and package["dispute_id"] == dispute_id else None


def _take_out_of_unsent_responses(db_path, dispute_id: str, document_id: int) -> None:
    """v1.1.2: a file left out of the checks is also out of what is sent. It is removed from every response of this case
    that PayPal does not hold yet (a sent version is PayPal's record and never changes); the approval of a response it
    was part of is cleared, exactly as when she removes a file herself."""
    from copilot.web import routes_package
    for package in repo.list_packages(db_path, dispute_id):
        if not any(a["document_id"] == document_id for a in repo.list_attachments(db_path, package["id"])):
            continue
        if routes_package._is_sent_version(db_path, dispute_id, package):
            continue
        repo.detach_document(db_path, package["id"], document_id)
        repo.log_action(db_path, "vendor", "detach_document", dispute_id,
                        {"package_id": package["id"], "document_id": document_id, "because": "left_out_of_checks"})
        routes_package._clear_approval(db_path, dispute_id, package["id"], log=True)


def _set_excluded(request: Request, dispute_id: str, document_id: int, excluded: bool,
                  csrf_token: str, action: str, return_to="") -> HTMLResponse:
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    document = repo.get_document(db_path, document_id)
    if document is None or document['dispute_id'] != dispute_id:
        return refusal_page(request,404,"Document not found","That document could not be found.",back_url=f"/disputes/{dispute_id}/workspace/prepare#documents")
    repo.set_document_excluded(db_path, document_id, excluded)
    repo.log_action(db_path, "vendor", action, dispute_id, {"document_id": document_id})
    if excluded:
        _take_out_of_unsent_responses(db_path, dispute_id, document_id)
    done = "document_excluded" if excluded else "document_included"
    query = urlencode({"done": done, "doc": document_id})
    package_id = _return_target(db_path, dispute_id, return_to)
    if package_id is not None:
        return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}?{query}#checks", status_code=303)
    return RedirectResponse(f"/disputes/{dispute_id}/workspace/prepare?{query}#documents", status_code=303)


@router.post("/disputes/{dispute_id}/documents/{document_id}/exclude", response_class=HTMLResponse)
def exclude_document(request: Request, dispute_id: str, document_id: int, csrf_token: str = Form(...),
                     return_to: str = Form(default="")):
    """A checklist conflict's 'remove the document' resolution: the document stops feeding
    the checklist/package input, but its row and history stay - reversible via include_document below."""
    return _set_excluded(request, dispute_id, document_id, True, csrf_token, "exclude_document", return_to)


@router.post("/disputes/{dispute_id}/documents/{document_id}/include", response_class=HTMLResponse)
def include_document(request: Request, dispute_id: str, document_id: int, csrf_token: str = Form(...),
                     return_to: str = Form(default="")):
    return _set_excluded(request, dispute_id, document_id, False, csrf_token, "include_document", return_to)
