"""Optional local print route; no client or mutation dependency."""
import hashlib
import logging
import sqlite3
from typing import Annotated
from fastapi import APIRouter, Path as RoutePath, Request
from fastapi.responses import HTMLResponse, Response
from copilot import config
from copilot.core.disputes import humanize_evidence_type
from copilot.features.package_print import case_file
from copilot.features.package_print.service import PackageMissing, PrintError, read
from copilot.web import status
from copilot.web.routes_disputes import templates, refusal_page
from copilot.web.security import rejected_dispute_id

router=APIRouter()
logger=logging.getLogger(__name__)

def status_line(header):
    """The one status line of the header, built through the send record of copilot/web/status.py so this page and the
    package page share one vocabulary (an abandoned true partial send reads as such). Never claims more than the stored
    record: a plainly confirmed send returns None (the template words it with the confirmed time); no attempt at all is
    'Not sent to PayPal'."""
    state=header.get("state")
    if state is None:
        return "Not sent to PayPal"
    if state=="confirmed" and not header.get("later"):
        return None
    record=status.SendRecord(state=status._STORE_STATE.get(state,state),attempt={"reason":header.get("reason") or ""},
                             attachments_match=not header.get("later"))
    label=status.evidence_status(record).label
    if state=="partial" and header.get("missing"):
        label=f"{label}: {', '.join(header['missing'])}"
    return label

def held_by_paypal(header):
    """True when PayPal holds the statements of this attempt (confirmed, or a true partial send): only then the header
    says the files and the tracking were SENT; for every other attempt they are the ones the attempt carried."""
    return header.get("state") in ("confirmed","partial")

def case_url(dispute_id,package_id):
    return f"/disputes/{dispute_id}/packages/{package_id}/case-file"

def _render(request,package,dispute_id,*,saved_copy):
    send=package["case"]["send"]
    readback=send["readback"] if send else None
    return templates.TemplateResponse(request,"features/package_print/print.html",
                                      {"package":package,"dispute_id":dispute_id,
                                       "status_line":status_line(package["header"]),
                                       "held_by_paypal":held_by_paypal(package["header"]),
                                       "source_commit":case_file.source_commit(),
                                       "readback":readback,
                                       "readback_label":humanize_evidence_type(readback["evidence_type"]) if readback else None,
                                       "case_url":case_url(dispute_id,package["package_id"]),"saved_copy":saved_copy},
                                      headers={"Cache-Control":"no-store"})

def _read(request,dispute_id,package_id):
    """(package, None) or (None, the refusal page)."""
    try:
        return read(config.db_path(),config.uploads_dir(),dispute_id,package_id),None
    except PackageMissing:
        return None,refusal_page(request,404,"Package not found","Package not found.",back_url=f"/disputes/{dispute_id}")
    except PrintError as exc:
        logger.warning("package print: package %s refused (%s)",package_id,exc)
        return None,refusal_page(request,400,"Cannot print this package",f"Cannot print this package. Review its local sources first. Reason: {exc}",back_url=f"/disputes/{dispute_id}/packages/{package_id}")

@router.get("/disputes/{dispute_id}/packages/{package_id}/print",response_class=HTMLResponse)
def page(request:Request,dispute_id:str,package_id:Annotated[int,RoutePath(gt=0,le=9223372036854775807)]):
    bad=rejected_dispute_id(dispute_id)
    if bad:return bad
    package,refusal=_read(request,dispute_id,package_id)
    if refusal is not None:
        return refusal
    return _render(request,package,dispute_id,saved_copy=False)

@router.get("/disputes/{dispute_id}/packages/{package_id}/case-file")
def download_case_file(request:Request,dispute_id:str,package_id:Annotated[int,RoutePath(gt=0,le=9223372036854775807)]):
    """W2-4: one zip of the print view, the ledger export (ledger on), the files and MANIFEST.json; check it offline with
    `python -m copilot.verify --case-file <zip>`. Read-only, like the print view."""
    bad=rejected_dispute_id(dispute_id)
    if bad:return bad
    package,refusal=_read(request,dispute_id,package_id)
    if refusal is not None:
        return refusal
    ledger_json=None
    if config.feature_enabled("ledger"):                             # the flag call guards the feature import (export closure)
        from copilot.features.ledger import service as ledger
        try:
            ledger_json,_=ledger.export(ledger.read(config.db_path(),dispute_id,ledger.configured_evidence()))
        except (ValueError,OSError,sqlite3.DatabaseError):
            ledger_json=False
    html=_render(request,package,dispute_id,saved_copy=True).body
    try:
        if ledger_json is False:
            raise PrintError("The ledger cannot be read safely.")
        data=case_file.build(package,dispute_id,html,config.uploads_dir(),ledger_json,case_file.source_commit())
    except PrintError:
        return refusal_page(request,400,"Cannot build the case file","Cannot build the case file. Review its local sources first.",back_url=f"/disputes/{dispute_id}/packages/{package_id}")
    return Response(data,media_type="application/zip",
                    headers={"Cache-Control":"no-store","X-Case-File-SHA256":hashlib.sha256(data).hexdigest(),
                             "Content-Disposition":f'attachment; filename="case-file-{dispute_id}-v{package["version"]}.zip"'})
