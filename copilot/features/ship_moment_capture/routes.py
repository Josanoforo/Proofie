from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from copilot import config
from copilot.db import repo
from copilot.features.ship_moment_capture import service
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, validate_dispute_id, verify_csrf

router = APIRouter()
ID = re.compile(r"^[A-Za-z0-9]{17}$")
PACKAGE_ID = re.compile(r"^[1-9][0-9]{0,17}$")


def _case_terms(dispute_id: str, capture_id: str):
    if validate_dispute_id(dispute_id) or not ID.fullmatch(capture_id):
        raise ValueError("The case and transaction references are invalid.")
    from copilot.features.sale_readiness import service as readiness
    record = readiness.load_case_terms(dispute_id, capture_id)
    if not record:
        raise ValueError("Save readiness terms for this exact case and transaction before linking photos to the case.")
    return record


def _package_url(dispute_id: str, package_id: str | None = None) -> str | None:
    if package_id is not None:
        if not isinstance(package_id, str) or not PACKAGE_ID.fullmatch(package_id):
            return None
        package = repo.get_package(config.db_path(), int(package_id))
        if not package or package.get("dispute_id") != dispute_id:
            return None
        return f"/disputes/{quote(dispute_id)}/packages/{package_id}#attachments"
    packages = repo.list_packages(config.db_path(), dispute_id)
    if not packages:
        return None
    latest = max(packages, key=lambda row: int(row["id"]))
    return f"/disputes/{quote(dispute_id)}/packages/{latest['id']}#attachments"


def _package_input(request: Request, form=None) -> tuple[str | None, bool, bool]:
    query_values = request.query_params.getlist("from_package")
    form_values = form.getlist("from_package") if form is not None else []
    if len(query_values) > 1 or len(form_values) > 1:
        return None, True, False
    if query_values and form_values and query_values[0] != form_values[0]:
        return None, True, False
    values = form_values or query_values
    return (values[0], True, True) if values else (None, False, True)


def _page(request: Request, capture_id: str, token: str, record=None, *, dispute_id="", package_id=None,
          case_documents=None, package_url=None, error="", status=200):
    response = templates.TemplateResponse(request, "features/ship_moment_capture/form.html",
        {"capture_id": capture_id, "csrf_token": token, "record": record, "error": error,
         "dispute_id": dispute_id, "from_package": package_id,
         "case_documents": case_documents or [], "package_url": package_url,
         "offer": service.registration_offer(capture_id, record, dispute_id)},
        status_code=status)
    set_csrf_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/fulfilment/{capture_id}/moment", response_class=HTMLResponse)
def form(request: Request, capture_id: str, from_dispute: str = ""):
    if not ID.fullmatch(capture_id):
        return HTMLResponse("Invalid transaction ID", status_code=400)
    record = service.read(capture_id)
    dispute_id = from_dispute or (record or {}).get("dispute_id", "")
    package_id, has_package, package_input_ok = _package_input(request)
    if not package_input_ok or (has_package and not dispute_id):
        return HTMLResponse("The selected package could not be found.", status_code=404)
    package_url = _package_url(dispute_id, package_id) if has_package else None
    if has_package and package_url is None:
        return HTMLResponse("The selected package could not be found.", status_code=404)
    if record and record.get("dispute_id") and dispute_id != record["dispute_id"]:
        return HTMLResponse("This shipment record is linked to a different case.", status_code=409)
    docs = []
    if dispute_id:
        try:
            _case_terms(dispute_id, capture_id)
            docs = service.case_documents(dispute_id, capture_id, record or {})
            package_url = package_url or _package_url(dispute_id)
        except (OSError, ValueError) as exc:
            token = new_csrf_token()
            return _page(request, capture_id, token, record, dispute_id=dispute_id, package_id=package_id,
                         package_url=package_url,
                         error=str(exc), status=409)
    token = new_csrf_token()
    return _page(request, capture_id, token, record, dispute_id=dispute_id, package_id=package_id,
                 case_documents=docs, package_url=package_url)


@router.post("/fulfilment/{capture_id}/moment", response_class=HTMLResponse)
async def save(request: Request, capture_id: str, csrf_token: str = Form(default=""),
               carrier: str = Form(default=""), tracking_number: str = Form(default=""), from_dispute: str = Form(default=""),
               label: UploadFile | None = File(default=None),
               parcel: UploadFile | None = File(default=None)):
    if not ID.fullmatch(capture_id):
        return HTMLResponse("Invalid transaction ID", status_code=400)
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("This form expired. Reload it before saving.", status_code=403)
    form_data = await request.form()
    package_id, has_package, package_input_ok = _package_input(request, form_data)
    if not package_input_ok:
        return HTMLResponse("The selected package could not be found.", status_code=404)
    submitted_dispute = form_data.get("from_dispute", "")
    if not isinstance(submitted_dispute, str):
        submitted_dispute = from_dispute if isinstance(from_dispute, str) else ""
    previous = service.read(capture_id)
    dispute_for_package = submitted_dispute or (previous or {}).get("dispute_id", "")
    package_url = None
    if has_package:
        package_url = _package_url(dispute_for_package, package_id) if dispute_for_package else None
        if package_url is None:
            return HTMLResponse("The selected package could not be found.", status_code=404)
    items = []
    for upload in (label, parcel):
        if upload is None or not upload.filename:
            items.append(None)
            continue
        payload = await upload.read(service.MAX_IMAGE_BYTES + 1)
        items.append((upload.filename, upload.content_type or "", payload))
    try:
        # Direct route calls in the existing unit harness receive FastAPI's Form sentinel;
        # only an actual posted string may bind this capture to a case.
        posted_dispute_id = submitted_dispute
        dispute_id = posted_dispute_id or (previous or {}).get("dispute_id", "")
        if previous and previous.get("dispute_id") and dispute_id != previous["dispute_id"]:
            raise ValueError("This shipment record is linked to a different case.")
        if dispute_id:
            _case_terms(dispute_id, capture_id)
        record = service.save(capture_id, carrier, items[0], items[1], dispute_id=dispute_id or None,
                              tracking_number=tracking_number if isinstance(tracking_number, str) else None)
        if dispute_id:
            service.link_case_documents(dispute_id, capture_id, record)
    except ValueError as exc:
        token = new_csrf_token()
        return _page(request, capture_id, token, service.read(capture_id), dispute_id=posted_dispute_id,
                     package_id=package_id, package_url=package_url,
                     error=str(exc), status=409 if "case" in str(exc).lower() or "transaction" in str(exc).lower() else 400)
    except OSError:
        token = new_csrf_token()
        posted_dispute_id = submitted_dispute
        return _page(request, capture_id, token, service.read(capture_id), dispute_id=posted_dispute_id,
                     package_id=package_id, package_url=package_url,
                     error="The shipment photo could not be saved or linked to case documents. Reload and try again.",
                     status=503)
    query = {"from_dispute": dispute_id} if dispute_id else {}
    if has_package:
        query["from_package"] = package_id
    query["saved"] = "1"
    from urllib.parse import urlencode
    return RedirectResponse(f"/fulfilment/{capture_id}/moment?{urlencode(query)}", status_code=303)


@router.get("/fulfilment/{capture_id}/moment/{kind}")
def photo(request: Request, capture_id: str, kind: str, from_dispute: str = ""):
    if not ID.fullmatch(capture_id):
        return HTMLResponse("Invalid transaction ID", status_code=400)
    record = service.read(capture_id)
    if record is None:
        return HTMLResponse("No saved shipment photo exists for this transaction.", status_code=404)
    package_id, has_package, package_input_ok = _package_input(request)
    dispute_id = from_dispute or str(record.get("dispute_id") or "")
    if not package_input_ok or (has_package and
            (record.get("dispute_id") != dispute_id or not _package_url(dispute_id, package_id))):
        return HTMLResponse("The selected package could not be found.", status_code=404)
    if from_dispute:
        if record.get("dispute_id") != from_dispute:
            return HTMLResponse("This shipment photo is not linked to this case.", status_code=404)
        try:
            _case_terms(from_dispute, capture_id)
        except (OSError, ValueError):
            return HTMLResponse("This shipment photo is not linked to this case.", status_code=404)
    item = service.photo(capture_id, kind)
    if item is None:
        return HTMLResponse("No saved shipment photo exists for this transaction.", status_code=404)
    payload, content_type, _filename = item
    return Response(payload, media_type=content_type,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": "inline"})
