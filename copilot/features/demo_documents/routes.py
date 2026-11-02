"""Attach the five published synthetic documents to a local package in the hosted demo.

The only PayPal interaction is the existing read-only transaction context lookup, used to ground the
generator's date. Document bytes and package attachments are local app data; this route never sends evidence.
"""
from __future__ import annotations

import hashlib
import struct
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
import zlib

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core.content_type import (ALLOWED_CONTENT_TYPES, detect_content_type, validate_call_total,
                                       validate_upload)
from copilot.core.context import SRC_SEARCH, fetch_context
from copilot.core.document_ingest import PdfReaderUnavailable, extract_text
from copilot.core.disputes import SandboxClient, normalize
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.web.routes_disputes import (csrf_expired_page, get_client, refusal_page, templates)
from copilot.web.routes_package import _clear_approval, _is_sent_version, _own_package, _sent_version_refusal
from copilot.web.security import (new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf)
from tools.make_demo_documents import generate

router = APIRouter()
_FLAG = "demo_documents"
_BACK = "Go back to the dispute"
_DATE_REQUIRED = ("PayPal did not return a verified transaction date for this sale. No synthetic documents were "
                  "created or attached. Review the transaction details and try again.")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DEMO_LABEL = b"SYNTHETIC / DEMO DATA - fictitious. Not a real carrier document."


def _label_generated_png(data: bytes) -> bytes:
    """Add an explicit PNG text label to the generator's intentionally unreadable scan."""
    if not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("The synthetic scan is not a PNG with an IHDR header")
    payload = b"Comment\0" + _DEMO_LABEL
    chunk = (struct.pack(">I", len(payload)) + b"tEXt" + payload
             + struct.pack(">I", zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF))
    return data[:33] + chunk + data[33:]


def _off(request: Request, dispute_id: str) -> HTMLResponse | None:
    if config.hosted_demo() and config.feature_enabled(_FLAG):
        return None
    return refusal_page(request, 404, "Demo documents are not available",
                        "This hosted demo feature is turned off.",
                        back_url=f"/disputes/{dispute_id}", back_label=_BACK)


@router.get("/disputes/{dispute_id}/packages/{package_id}/demo-documents", response_class=HTMLResponse)
def confirm_demo_documents(request: Request, dispute_id: str, package_id: int):
    off = _off(request, dispute_id)
    if off is not None:
        return off
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    package, refusal = _own_package(request, dispute_id, package_id)
    if refusal is not None:
        return refusal
    if _is_sent_version(config.db_path(), dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package_id)
    csrf_token = new_csrf_token()
    response = templates.TemplateResponse(
        request, "features/demo_documents/confirm.html",
        {"dispute_id": dispute_id, "package_id": package_id, "csrf_token": csrf_token})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, csrf_token)
    return response


def verified_sale_date(request: Request, dispute_id: str, client: SandboxClient):
    """(context, sale date, refusal): the transaction initiation date as PayPal's existing read path returns it. A failed
    or unverified read creates nothing; the refusal is the page to return."""
    back = dict(back_url=f"/disputes/{dispute_id}", back_label=_BACK)
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
        context = fetch_context(client, normalize(raw, datetime.now(timezone.utc)))
    except Exception:  # noqa: BLE001 - a failed read creates no documents or attachments
        return None, None, refusal_page(request, 503, "Could not verify the sale date", _DATE_REQUIRED, **back)
    sale_date = context.get("payment_date")
    if context.source_of("payment_date") != SRC_SEARCH or not isinstance(sale_date, str):
        return None, None, refusal_page(request, 409, "Could not verify the sale date", _DATE_REQUIRED, **back)
    try:
        return context, date.fromisoformat(sale_date[:10]), None
    except ValueError:
        return None, None, refusal_page(request, 409, "Could not verify the sale date", _DATE_REQUIRED, **back)


def attach_generated(request: Request, dispute_id: str, package_id: int, generator_date: date, *,
                     skip_existing: bool = False):
    """(document ids, refusal): generate the five synthetic documents for `generator_date`, run them through the existing
    upload checks and attach them to the package. `skip_existing` leaves a document that is already attached alone
    (the seeded-case proposal is repeatable); the hosted route keeps refusing a name that is already there."""
    db_path = config.db_path()
    back = dict(back_url=f"/disputes/{dispute_id}", back_label=_BACK)
    attached = repo.list_attachments(db_path, package_id)
    names = {row["filename"] for row in attached}
    with tempfile.TemporaryDirectory(prefix="proofie-demo-docs-") as tmp:
        paths = generate(Path(tmp), today=generator_date)
        prepared = []
        for path in paths:
            if skip_existing and path.name in names:
                continue
            data = path.read_bytes()
            if path.suffix.lower() == ".png":
                try:
                    data = _label_generated_png(data)
                except ValueError:
                    return None, refusal_page(request, 422, "Synthetic documents could not be prepared",
                                              "The generated scan did not pass the existing image checks. Nothing was attached.",
                                              **back)
            problems = validate_upload(path.name, data)
            content_type = detect_content_type(data)
            if problems or content_type not in ALLOWED_CONTENT_TYPES:
                return None, refusal_page(request, 422, "Synthetic documents could not be prepared",
                                          "The generated documents did not pass the existing upload checks. Nothing was attached.",
                                          **back)
            if path.name in names:
                return None, refusal_page(request, 409, "Synthetic documents are already attached",
                                          "A document with one of these names is already attached. Nothing was changed.", **back)
            try:
                extracted_text, extraction_ok = extract_text(content_type, data)
            except PdfReaderUnavailable:
                return None, refusal_page(request, 503, "The PDF reader is not available",
                                          "The PDF reader is unavailable, so no synthetic documents were attached.", **back)
            prepared.append((path.name, data, content_type, extracted_text, extraction_ok))

        problems = validate_call_total([row["size_bytes"] for row in attached] + [len(item[1]) for item in prepared])
        if problems:
            return None, refusal_page(request, 400, "Cannot attach these documents", problems[0], **back)

        document_ids = []
        for filename, data, content_type, extracted_text, extraction_ok in prepared:
            sha256 = hashlib.sha256(data).hexdigest()
            write_raw(config.uploads_dir(), sha256, data)
            doc_id = repo.insert_document(db_path, dispute_id, filename, content_type, len(data), sha256,
                                          extracted_text, extraction_ok)
            repo.attach_document(db_path, package_id, doc_id)
            document_ids.append(doc_id)
    return document_ids, None


@router.post("/disputes/{dispute_id}/packages/{package_id}/demo-documents", response_class=HTMLResponse)
def attach_demo_documents(request: Request, dispute_id: str, package_id: int, csrf_token: str = Form(...),
                          client: SandboxClient = Depends(get_client)):
    off = _off(request, dispute_id)
    if off is not None:
        return off
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    package, refusal = _own_package(request, dispute_id, package_id)
    if refusal is not None:
        return refusal
    if _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package_id)

    # Ground relative dates in the transaction initiation date returned by PayPal's existing read path.
    _context, generator_date, refusal = verified_sale_date(request, dispute_id, client)
    if refusal is not None:
        return refusal
    document_ids, refusal = attach_generated(request, dispute_id, package_id, generator_date)
    if refusal is not None:
        return refusal

    _clear_approval(db_path, dispute_id, package_id)
    repo.log_action(db_path, "vendor", "attach_synthetic_demo_documents", dispute_id,
                    {"package_id": package_id, "document_ids": document_ids,
                     "date_source": SRC_SEARCH, "sale_date": generator_date.isoformat()})
    return RedirectResponse(
        f"/disputes/{dispute_id}/packages/{package_id}?{urlencode({'done': 'synthetic_documents_attached'})}#attachments",
        status_code=303)
