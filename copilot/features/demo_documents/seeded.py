"""D-101 r1 (R1-5): on a case the sandbox seed created, one confirmed click proposes the case's documents and shipment fields.

The seed puts a fictitious recipient on both orders (`tools/sandbox_seed.py` SHIPPING_NAME). This route runs only when PayPal's own
transaction read returns that name, so it never acts on any other case. It attaches the five synthetic documents (the same
generator and upload checks as the hosted demo flow, labelled DEMO DATA) and saves a proposed set of shipment fields, derived from
the first synthetic document, into the manual fields the seller already uses. Values the seller typed earlier win. The seller
still reviews every claim and approves the package; nothing here sends anything to PayPal."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core.context import SRC_SEARCH, fetch_context
from copilot.core.disputes import SandboxClient
from copilot.db import repo
from copilot.features.demo_documents.routes import attach_generated, verified_sale_date
from copilot.web.form_fields import shipment_fields
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page, templates
from copilot.web.routes_package import EMPTY_MANUAL_FLAG, _clear_approval, _is_sent_version, _sent_version_refusal
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf
from tools.sandbox_seed import SHIPPING_NAME

router = APIRouter()
FLAG = "demo_documents_seeded"
_BACK = "Go back to the dispute"
_NOT_SEEDED = ("PayPal's record of this sale does not carry the fictitious recipient the Proofie sandbox seed uses, so this is not "
               "a seeded demo case. Nothing was attached or filled in.")
DEMO_LABEL = "DEMO DATA"
# Same facts as the first synthetic document (tools/make_demo_documents.py: ship = sale + 1 day, delivered = sale + 3 days).
PROPOSAL_LABELS = (("carrier", "Carrier"), ("tracking_number", "Tracking number"), ("delivery_status", "Delivery status"),
                   ("ship_date", "Ship date"), ("delivery_date", "Delivery date"), ("receiver_name", "Received by"))


def proposal_form(sale_date: date) -> dict:
    """The proposed shipment values as the manual-fields form would post them."""
    return {"carrier": "FEDEX", "tracking_number": "DEMO-FDX-000001", "delivery_status": "Delivered",
            "ship_date": (sale_date + timedelta(days=1)).isoformat(),
            "delivery_date": (sale_date + timedelta(days=3)).isoformat(), "receiver_name": "Ana Demo"}


def is_seeded(context) -> bool:
    return context.source_of("recipient_name") == SRC_SEARCH and context.get("recipient_name") == SHIPPING_NAME


def offered(client: SandboxClient, norm: dict) -> bool:
    """True when the seeded-demo page would accept this case: the same reads and checks as verified_sale_date and is_seeded.
    The case page draws the entry link only then, so the link never leads to a refusal. A failed read is False; nothing is created."""
    try:
        context = fetch_context(client, norm)
        sale_date = context.get("payment_date")
        if context.source_of("payment_date") != SRC_SEARCH or not isinstance(sale_date, str):
            return False
        date.fromisoformat(sale_date[:10])
        return is_seeded(context)
    except Exception:  # noqa: BLE001 - an unreadable case simply gets no entry link
        return False


def _off(request: Request, dispute_id: str):
    if config.feature_enabled(FLAG) and not config.hosted_demo():
        return None
    return refusal_page(request, 404, "Demo proposal is not available", "This feature is turned off.",
                        back_url=f"/disputes/{dispute_id}", back_label=_BACK)


def _checked(request: Request, dispute_id: str, client: SandboxClient):
    """(context, sale date, refusal) for a seeded case; any refusal is the page to return."""
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return None, None, bad_id
    context, sale_date, refusal = verified_sale_date(request, dispute_id, client)
    if refusal is not None:
        return None, None, refusal
    if not is_seeded(context):
        return None, None, refusal_page(request, 409, "This is not a seeded demo case", _NOT_SEEDED,
                                        back_url=f"/disputes/{dispute_id}", back_label=_BACK)
    return context, sale_date, None


def _latest_package(dispute_id: str):
    packages = repo.list_packages(config.db_path(), dispute_id)
    return packages[-1] if packages else None


@router.get("/disputes/{dispute_id}/seeded-demo", response_class=HTMLResponse)
def confirm_seeded_demo(request: Request, dispute_id: str, client: SandboxClient = Depends(get_client)):
    off = _off(request, dispute_id)
    if off is not None:
        return off
    _context, sale_date, refusal = _checked(request, dispute_id, client)
    if refusal is not None:
        return refusal
    form = proposal_form(sale_date)
    csrf_token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/demo_documents/seeded_confirm.html", {
        "dispute_id": dispute_id, "csrf_token": csrf_token, "label": DEMO_LABEL,
        "rows": [(label, form[name]) for name, label in PROPOSAL_LABELS], "sale_date": sale_date.isoformat()})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/disputes/{dispute_id}/seeded-demo", response_class=HTMLResponse)
def apply_seeded_demo(request: Request, dispute_id: str, csrf_token: str = Form(...),
                      client: SandboxClient = Depends(get_client)):
    off = _off(request, dispute_id)
    if off is not None:
        return off
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    _context, sale_date, refusal = _checked(request, dispute_id, client)
    if refusal is not None:
        return refusal
    db_path = config.db_path()
    package = _latest_package(dispute_id)
    if package is not None and _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package["id"])
    proposed, errors, _values = shipment_fields(proposal_form(sale_date))
    if errors:
        return refusal_page(request, 422, "The proposal could not be prepared",
                            "The proposed shipment details did not pass the form checks. Nothing was attached.",
                            back_url=f"/disputes/{dispute_id}", back_label=_BACK)
    started = package is None
    if started:        # the seller has no package yet: start the manual one, as the "start manually" button does
        package_id = repo.insert_package(db_path, dispute_id, source="human_edit", validation_ok=False,
                                         review_flags=[EMPTY_MANUAL_FLAG])
        repo.log_action(db_path, "vendor", "start_manual_package", dispute_id, {"package_id": package_id})
    else:
        package_id = package["id"]
    document_ids, refusal = attach_generated(request, dispute_id, package_id, sale_date, skip_existing=True)
    if refusal is not None:
        return refusal
    existing = repo.get_manual_fields(db_path, dispute_id)
    merged = {**proposed, **existing}                       # what the seller already typed wins
    if merged != existing:
        repo.set_manual_fields(db_path, dispute_id, merged)
    _clear_approval(db_path, dispute_id, package_id)
    repo.log_action(db_path, "vendor", "apply_seeded_case_proposal", dispute_id,
                    {"package_id": package_id, "package_started": started, "document_ids": document_ids, "label": DEMO_LABEL,
                     "fields_proposed": sorted(proposed), "fields_kept": sorted(set(existing) & set(proposed)),
                     "sale_date": sale_date.isoformat()})
    return RedirectResponse(
        f"/disputes/{dispute_id}/packages/{package_id}?{urlencode({'done': 'synthetic_documents_attached'})}#attachments",
        status_code=303)
