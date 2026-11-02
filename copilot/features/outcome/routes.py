"""/disputes/{dispute_id}/outcome routes (exactly three). Registered in copilot/app.py behind COPILOT_FEATURE_OUTCOME (lazy
import inside that flag block). The page is reads only. The two appeal routes answer 403 unless
COPILOT_FEATURE_OUTCOME_APPEAL is on; with it on, the appeal is previewed with its sha256 and registered only when the
submitted sha256 equals the one recomputed here from the package, and only while PayPal's fresh GET shows the appeal
link. No other PayPal write exists in this feature."""
from __future__ import annotations

import json
import secrets
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.web import status as web_status
from copilot.web.dispute_state import is_buyer_closed
from copilot.core.disputes import SandboxClient
from copilot.db import repo
from copilot.features.outcome import appeal, service, store, appeal_approval
from copilot.features.outcome.read import read_outcome
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
RESULT_TEXT = {
    "sent_confirmed": "PayPal shows a change after the appeal: sent and confirmed by reading the dispute back.",
    "sent_unconfirmed": "The appeal was sent, but PayPal's answer could not confirm it yet. Refresh before doing anything else.",
    "failed": "PayPal refused the appeal. Nothing was changed.",
}


def _appeal_enabled() -> bool:
    return config.feature_enabled("outcome_appeal")


def _render(request: Request, dispute_id: str, view: service.OutcomeView, status_code: int = 200, **extra):
    token = new_csrf_token()
    buyer_closed = is_buyer_closed(view.read.outcome_code)
    page_name = ("What PayPal has recorded" if config.feature_enabled("outcome") and (buyer_closed or not view.read.decided)
                 else "What PayPal decided")
    appeal_workspace = None
    appeal_workspace_error = None
    if config.feature_enabled("appeal_workspace"):
        try:
            from copilot.features.appeal_workspace.service import assemble_case
            appeal_workspace = assemble_case(dispute_id, view, config.db_path(), config.uploads_dir())
        except (OSError, TypeError, ValueError) as exc:
            appeal_workspace_error = str(exc)
    ctx = {"buyer_closed": buyer_closed, "page_name": page_name, "dispute_id": dispute_id, "view": view, "read": view.read, "file": view.file, "gaps": view.gaps,
           "appeal_workspace": appeal_workspace, "appeal_workspace_error": appeal_workspace_error,
           "packages": repo.list_packages(config.db_path(), dispute_id) if view.appeal_allowed else [],
           "appeal_enabled": _appeal_enabled(), "csrf_token": token, "preview": None, "body_text": "",
           "result": None, "result_text": "", "active_appeal_approval": config.feature_enabled("active_approval"),
           "appeal_review_sha256": "", "appeal_receipt_id": "",
           "message_status": web_status.message_status, "tracking_status": web_status.tracking_status, **extra}
    response = templates.TemplateResponse(request, "features/outcome/outcome.html", ctx, status_code=status_code)
    set_csrf_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"          # a check/result page: never shown from the cache
    return response


def _guard(request: Request, dispute_id: str, csrf_token: str):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not _appeal_enabled():
        return refusal_page(request, 403, "Appeal is switched off",
                            "Appeal is switched off in this installation, so nothing was sent to PayPal.",
                            back_url=f"/disputes/{dispute_id}/outcome", back_label="Back to the outcome")
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    return None


@router.get("/disputes/{dispute_id}/outcome", response_class=HTMLResponse)
def outcome_page(request: Request, dispute_id: str, client: SandboxClient = Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    return _render(request, dispute_id, service.outcome_view(client, config.db_path(), dispute_id))


@router.post("/disputes/{dispute_id}/outcome/appeal/preview", response_class=HTMLResponse)
def appeal_preview(request: Request, dispute_id: str, csrf_token: str = Form(default=""),
                   package_id: str = Form(default=""), decision: str = Form(default=""),
                   review_sha256: str = Form(default=""), client: SandboxClient = Depends(get_client)):
    refused = _guard(request, dispute_id, csrf_token)
    if refused:
        return refused
    back = f"/disputes/{dispute_id}/outcome"
    if not package_id.isdigit():
        return refusal_page(request, 400, "Choose a package", "Choose the package whose evidence you want to appeal with.",
                            back_url=back, back_label="Back to the outcome")
    read = read_outcome(client, dispute_id)
    if not read.appeal_link_present:
        return refusal_page(request, 409, "No appeal is offered",
                            "PayPal does not offer an appeal for this dispute right now; nothing was prepared.",
                            back_url=back, back_label="Back to the outcome")
    prepared = service.prepare_appeal(config.db_path(), dispute_id, int(package_id))
    if prepared.body is None:
        return refusal_page(request, 400, "This appeal cannot be prepared", prepared.reason, back_url=back, back_label="Back to the outcome")
    extra = {}
    if config.feature_enabled("active_approval"):
        try:
            prepared, approval_input, review_digest = appeal_approval.review(client, config.db_path(), dispute_id, int(package_id))
            receipt_id = ""
            if decision:
                if decision != "approve_appeal":
                    raise appeal_approval.ReceiptError("Confirm approval of this appeal explicitly.")
                receipt_id = appeal_approval.approve(config.db_path(), approval_input, review_sha256)
            extra = {"appeal_review_sha256": review_digest, "appeal_receipt_id": receipt_id}
        except appeal_approval.ReceiptError as exc:
            return refusal_page(request, 409, "Review this appeal again", str(exc), back_url=back, back_label="Back to the outcome")
        except sqlite3.Error:
            return refusal_page(request, 503, "Approval could not be saved", "Nothing was sent. Review again when storage is available.", back_url=back, back_label="Back to the outcome")
    view = service.outcome_view(client, config.db_path(), dispute_id)
    return _render(request, dispute_id, view, preview=prepared, package_id=int(package_id),
                   body_text=json.dumps(prepared.body, sort_keys=True, ensure_ascii=False), **extra)


@router.post("/disputes/{dispute_id}/outcome/appeal/register", response_class=HTMLResponse)
def appeal_register(request: Request, dispute_id: str, csrf_token: str = Form(default=""),
                    package_id: str = Form(default=""), approval_sha256: str = Form(default=""),
                    appeal_receipt_id: str = Form(default=""), client: SandboxClient = Depends(get_client)):
    refused = _guard(request, dispute_id, csrf_token)
    if refused:
        return refused
    back = f"/disputes/{dispute_id}/outcome"
    if not package_id.isdigit():
        return refusal_page(request, 400, "Choose a package", "The package is missing.", back_url=back, back_label="Back to the outcome")
    prepared = service.prepare_appeal(config.db_path(), dispute_id, int(package_id))
    if config.feature_enabled("active_approval") and appeal_receipt_id:
        try:
            row = appeal_approval.receipt(config.db_path(), appeal_receipt_id)
            if row and row["dispute_id"] == dispute_id and row["package_id"] == int(package_id) and row["state"] == "ready":
                if prepared.body is None or prepared.sha256 != approval_sha256:
                    appeal_approval.invalidate(config.db_path(), appeal_receipt_id)
        except sqlite3.Error:
            return refusal_page(request, 503, "Appeal storage unavailable", "Nothing was sent. Review the saved approval before continuing.",
                                back_url=back, back_label="Back to the outcome")
    if prepared.body is None:
        return refusal_page(request, 400, "This appeal cannot be sent", prepared.reason, back_url=back, back_label="Back to the outcome")
    if not secrets.compare_digest(approval_sha256.encode("utf-8"), prepared.sha256.encode("utf-8")):
        return refusal_page(request, 409, "What you approved is not what would be sent",
                            "The approved request no longer matches the package, so nothing was sent to PayPal. "
                            "Prepare the appeal again and approve it.", back_url=back, back_label="Back to the outcome")
    try:
        result = appeal.register_appeal(client, dispute_id, prepared.body, list(prepared.attachments), prepared.sha256,
                                        db_path=config.db_path(), package_id=int(package_id), receipt_id=appeal_receipt_id)
    except appeal_approval.ReceiptError as exc:
        return refusal_page(request, 409, "Approve this appeal first", str(exc), back_url=back, back_label="Back to the outcome")
    except sqlite3.Error:
        return refusal_page(request, 503, "Appeal storage unavailable",
                            "An approval or attempt could not be recorded. Do not retry an uncertain appeal; review its state first.",
                            back_url=back, back_label="Back to the outcome")
    if result.status == "rejected_link":
        return refusal_page(request, 409, "No appeal is offered", result.reason, back_url=back, back_label="Back to the outcome")
    if result.posted:
        store.save_appeal(dispute_id, prepared.body, prepared.body_sha256, prepared.sha256, result.readback, result.status,
                          " ".join(lim.message for lim in result.limitations) or None)
    view = service.outcome_view(client, config.db_path(), dispute_id)
    return _render(request, dispute_id, view, result=result, result_text=((RESULT_TEXT.get(result.status, result.reason) + (" " + result.reason if result.source_changed else ""))
                                                        if result.posted else result.reason))
