import json
import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.db import repo
from copilot.features.buyer_note import service
from copilot.features.fulfilment.tracker_request import validate_capture_id
from copilot.web.routes_disputes import templates, get_client, refusal_page
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf
from urllib.parse import urlencode, quote

router = APIRouter()

_REFUSAL_TITLES = {400: "Check the details", 403: "Your session expired", 404: "Nothing to show here", 409: "Not available for this sale"}


def _refuse(request, message, status):
    """A refusal on a page the seller reached through a link: the site's error layout, with a way back (not bare text)."""
    return refusal_page(request, status, _REFUSAL_TITLES.get(status, "Not available"), message,
                        back_url="/disputes", back_label="Go back to disputes")
PACKAGE_ID = re.compile(r"^[1-9][0-9]{0,17}$")


def _package_reference(request, form=None):
    query_values = request.query_params.getlist("from_package")
    form_values = form.getlist("from_package") if form is not None else []
    if len(query_values) > 1 or len(form_values) > 1:
        return None, True, False
    if query_values and form_values and query_values[0] != form_values[0]:
        return None, True, False
    values = form_values or query_values
    return (values[0], True, True) if values else (None, False, True)


def _explicit_sale_package(capture_id: str, package_id: str) -> tuple[str, str] | None:
    if not isinstance(package_id, str) or not PACKAGE_ID.fullmatch(package_id):
        return None
    try:
        from copilot.features.ship_moment_capture import service as moment_service
        from copilot.features.sale_readiness import service as readiness
        moment = moment_service.read(capture_id)
        dispute_id = (moment or {}).get("dispute_id")
        linked = readiness.load_case_terms(dispute_id, capture_id) if isinstance(dispute_id, str) else None
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(dispute_id, str) or not linked or linked.get("dispute_id") != dispute_id or linked.get("capture_id") != capture_id:
        return None
    package = repo.get_package(config.db_path(), int(package_id))
    if not package or package.get("dispute_id") != dispute_id:
        return None
    return dispute_id, f"/disputes/{quote(dispute_id)}/packages/{package_id}#attachments"


def _delivery_incident_context(capture_id: str, requested_dispute: str, package_id: str | None):
    """Load only capture-bound facts; a query-string case is not proof of a case relationship."""
    if validate_capture_id(capture_id):
        return None, "Enter a valid sale transaction ID.", 400
    if requested_dispute and rejected_dispute_id(requested_dispute):
        return None, "The selected case could not be found.", 404
    if not config.feature_enabled("fulfilment"):
        return None, "Saved shipment records are unavailable in this setup.", 404
    from copilot.features.fulfilment import store as fulfilment_store
    shipment = fulfilment_store.get_file(capture_id)
    if shipment is None:
        return None, "No saved local shipment record exists for this sale.", 404

    moment = None
    if config.feature_enabled("ship_moment_capture"):
        from copilot.features.ship_moment_capture import service as moment_service
        try:
            moment = moment_service.read(capture_id)
        except (OSError, TypeError, ValueError):
            return None, "The saved shipment photo record cannot be read safely.", 409
    recorded_dispute = str((moment or {}).get("dispute_id") or "")
    if requested_dispute and recorded_dispute and requested_dispute != recorded_dispute:
        return None, "This shipment record is linked to a different case.", 409
    dispute_id = requested_dispute or recorded_dispute

    terms_record = None
    linked = False
    if dispute_id and config.feature_enabled("sale_readiness"):
        from copilot.features.sale_readiness import service as readiness
        try:
            terms_record = readiness.load_case_terms(dispute_id, capture_id)
        except (OSError, ValueError):
            return None, "The saved readiness record cannot be read safely.", 409
        linked = bool(terms_record and terms_record.get("schema_version") == 1
                      and terms_record.get("dispute_id") == dispute_id
                      and terms_record.get("capture_id") == capture_id
                      and isinstance(terms_record.get("terms"), dict))

    package_url = None
    selected_package_id = None
    package_has_reference = package_id is not None
    if package_has_reference:
        if not linked or not PACKAGE_ID.fullmatch(package_id):
            return None, "The selected package could not be found.", 404
        package = repo.get_package(config.db_path(), int(package_id))
        if not package or package.get("dispute_id") != dispute_id:
            return None, "The selected package could not be found.", 404
        selected_package_id = package_id
        package_url = f"/disputes/{quote(dispute_id)}/packages/{package_id}#attachments"
    elif linked:
        packages = repo.list_packages(config.db_path(), dispute_id)
        if packages:
            latest = max(packages, key=lambda row: int(row["id"]))
            selected_package_id = str(latest["id"])
            package_url = f"/disputes/{quote(dispute_id)}/packages/{selected_package_id}#attachments"

    tracking_source_sha = hashlib.sha256((shipment.get("request_json") or "").encode("utf-8")).hexdigest()
    readiness_source_sha = (hashlib.sha256(json.dumps(terms_record, sort_keys=True, ensure_ascii=False,
                                                      separators=(",", ":")).encode("utf-8")).hexdigest()
                            if linked else None)
    photo_source_hashes = {}
    if linked and moment and moment.get("dispute_id") == dispute_id and config.feature_enabled("ship_moment_capture"):
        from copilot.features.ship_moment_capture import service as moment_service
        linked_docs = moment_service.case_documents(dispute_id, capture_id, moment)
        linked_hashes = {row.get("sha256") for row in linked_docs}
        for kind in ("label", "parcel"):
            item = moment_service.photo(capture_id, kind)
            if item:
                digest = hashlib.sha256(item[0]).hexdigest()
                if digest in linked_hashes:
                    photo_source_hashes[kind] = digest

    return {"capture_id": capture_id, "shipment": shipment, "moment": moment,
            "dispute_id": dispute_id, "terms_record": terms_record if linked else None,
            "case_linked": linked, "package_id": selected_package_id,
            "package_has_reference": package_has_reference, "package_url": package_url,
            "tracking_source_sha256": tracking_source_sha,
            "readiness_source_sha256": readiness_source_sha,
            "photo_source_sha256": photo_source_hashes}, "", 200


def _delivery_incident_page(request, context, incident, locale, *, error=None, status_code=200, seller_text=None):
    token = new_csrf_token()
    shipment = context["shipment"]
    terms_record = context.get("terms_record") or {}
    terms = terms_record.get("terms") if isinstance(terms_record.get("terms"), dict) else {}
    note = service.sale_note(context["capture_id"], shipment, locale, terms)
    try:
        tracking_rows = json.loads(shipment.get("request_json") or "{}").get("trackers") or []
        saved_tracker = next((row for row in tracking_rows if isinstance(row, dict)
                              and row.get("transaction_id") == context["capture_id"]), {})
        note["tracking_number"] = saved_tracker.get("tracking_number")
    except (TypeError, ValueError, AttributeError):
        note["tracking_number"] = None

    saved_status = None
    saved_status_source = "Saved local fulfilment record; status was not independently checked."
    request_sha = None
    request_facts = None
    if config.feature_enabled("delivery_facts"):
        try:
            from copilot.features.delivery_facts.service import FactsError, FactsMissing, read
            request_facts = read(config.db_path(), context["capture_id"])
        except (FactsError, FactsMissing, OSError, ValueError):
            request_facts = None
    if isinstance(request_facts, dict):
        requested = request_facts.get("request")
        if isinstance(requested, dict):
            saved_status = requested.get("status")
        request_sha = request_facts.get("sha256")
        saved_status_source = (f"Validated local fulfilment capture {context['capture_id']} · source SHA-256 {request_sha}; "
                               "this is the saved request/readback snapshot, not a current carrier response.")
    if saved_status is None:
        try:
            payload = json.loads(shipment.get("request_json") or "{}")
            tracker = next((row for row in payload.get("trackers", []) if isinstance(row, dict)
                            and row.get("transaction_id") == context["capture_id"]), {})
            saved_status = tracker.get("status")
        except (TypeError, ValueError, AttributeError):
            saved_status = None

    readiness_sha = None
    if context.get("terms_record"):
        readiness_sha = context.get("readiness_source_sha256")
    estimate_source = (f"Schema-1 readiness terms for {context['dispute_id']} / {context['capture_id']} · SHA-256 {readiness_sha}."
                       if readiness_sha else "No linked schema-1 readiness terms are available; no estimate or case relationship is inferred.")

    photos = []
    moment = context.get("moment")
    if moment and config.feature_enabled("ship_moment_capture"):
        from copilot.features.ship_moment_capture import service as moment_service
        linked_docs = moment_service.case_documents(context["dispute_id"], context["capture_id"], moment) \
            if context.get("case_linked") and moment.get("dispute_id") == context.get("dispute_id") else []
        saved_files = moment.get("files") if isinstance(moment.get("files"), dict) else {}
        for kind, filename in saved_files.items():
            item = moment_service.photo(context["capture_id"], kind)
            if not item:
                continue
            payload, _content_type, safe_filename = item
            digest = __import__("hashlib").sha256(payload).hexdigest()
            document = next((row for row in linked_docs if row.get("filename", "").endswith(digest[:12] + (".jpg" if _content_type == "image/jpeg" else ".png"))), None)
            query = {}
            if context.get("case_linked") and moment.get("dispute_id") == context.get("dispute_id"):
                query["from_dispute"] = context["dispute_id"]
            if context.get("package_id"):
                query["from_package"] = context["package_id"]
            photo_url = f"/fulfilment/{context['capture_id']}/moment/{kind}"
            if query:
                photo_url += "?" + urlencode(query)
            photos.append({"kind": kind, "filename": safe_filename, "sha256": digest,
                           "url": photo_url, "case_document_id": document.get("id") if document else None,
                           "case_linked": bool(document),
                           "source_label": "Seller-entered original; image contents not authenticated."})

    transit_guidance = None
    if saved_status and config.feature_enabled("transit_delivered_guide"):
        from copilot.features.transit_delivered_guide.service import guidance_for
        transit_guidance = guidance_for(saved_status)

    carrier_paths = []
    tracker = {}
    try:
        payload = json.loads(shipment.get("request_json") or "{}")
        tracker = next((row for row in payload.get("trackers", []) if isinstance(row, dict)
                        and row.get("transaction_id") == context["capture_id"]), {})
    except (TypeError, ValueError, AttributeError):
        pass
    saved_code = tracker.get("carrier")
    if saved_code and saved_code != "OTHER" and config.feature_enabled("lost_package_paths"):
        from copilot.core.carriers import map_carrier
        from copilot.features.lost_package_paths.service import carrier_rows, paths_for
        known = []
        for row in carrier_rows():
            mapped = map_carrier(row.get("name"))
            if mapped.code == saved_code:
                known.append(row)
        if len(known) == 1 and context.get("case_linked"):
            carrier_paths = paths_for(known[0]["id"], context["dispute_id"])["paths"]

    lessons = []
    kind = terms.get("kind") if isinstance(terms.get("kind"), str) else ""
    if kind in ("physical", "digital", "service") and config.feature_enabled("sale_lesson"):
        from copilot.features.sale_lesson.service import lessons_for_preparation
        lessons = lessons_for_preparation(kind)

    guide_incident = "delay" if incident == "buyer_question" else incident
    incident_view = service.delivery_incident_content(
        guide_incident, locale, note, saved_status=saved_status, saved_status_source=saved_status_source,
        estimate_source=estimate_source, photos=photos, linked_to_case=context.get("case_linked", False),
        transit_guidance=transit_guidance, carrier_paths=carrier_paths, lessons=lessons)
    if incident == "buyer_question":
        incident_view["title"] = "Buyer question" if locale == "en" else "Consulta del comprador"
        incident_view["script"] = ("Thank you for your question. I will review the recorded details and reply after checking them." if locale == "en" else "Gracias por tu consulta. Revisaré los datos registrados antes de responder.")
    responses = []
    if context.get("case_linked"):
        responses = [row for row in repo.list_audit(config.db_path(), context["dispute_id"])
                     if row.get("action") == "delivery_incident_response"
                     and isinstance(row.get("detail"), dict)
                     and row["detail"].get("capture_id") == context["capture_id"]
                     and row["detail"].get("incident") == incident
                     and row["detail"].get("package_id") == (int(context["package_id"]) if context.get("package_id") else None)]
    response = templates.TemplateResponse(request, "features/buyer_note/delivery_incident.html", {
        "context": context, "incident_view": incident_view, "incident_types": service.DELIVERY_INCIDENTS,
        "selected_incident": incident, "locale": locale, "responses": responses,
        "csrf_token": token, "error": error, "seller_text": seller_text,
        "readiness_url": f"/fulfilment/{context['capture_id']}?from_dispute={quote(context['dispute_id'])}#sale-readiness" if context.get("dispute_id") else f"/fulfilment/{context['capture_id']}#sale-readiness",
        "lessons_url": f"/sale-lessons?kind={quote(kind)}" if kind and config.feature_enabled("sale_lesson") else None,
    }, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


def _trade_profile():
    if not config.feature_enabled("trade_profiles"):
        return None
    from copilot.features.trade_profiles import service as profiles
    return profiles.current_profile()


def _seller_facts(dispute_id: str, capture_id: str):
    description = tracking = carrier = expected_date = currency = ""
    if capture_id and config.feature_enabled("sale_readiness"):
        try:
            from copilot.features.sale_readiness import service as readiness
            saved = readiness.load_case_terms(dispute_id, capture_id)
            if saved and isinstance(saved.get("terms"), dict):
                terms = saved["terms"]
                description = terms.get("description", "")
                tracking = terms.get("tracking_number", "")
                carrier = terms.get("carrier", "")
                expected_date = terms.get("expected_date", "")
                currency = terms.get("currency", "")
        except (OSError, ValueError):
            pass
    fields = repo.get_manual_fields(config.db_path(), dispute_id)
    tracking = tracking or str((fields.get("tracking_number") or {}).get("value", ""))
    carrier = carrier or str((fields.get("carrier") or {}).get("value", ""))
    return {"description": description, "tracking_number": tracking, "carrier": carrier,
            "expected_date": expected_date, "currency": currency}


def _render(request, dispute_id, capture_id, locale, *, error=None, status_code=200):
    token = new_csrf_token()
    facts = _seller_facts(dispute_id, capture_id)
    note_facts = {key: value for key, value in facts.items() if key != "currency"}
    try:
        note = service.build_note(**note_facts, locale=locale)
    except ValueError as exc:
        note, error = service.build_note(description="", tracking_number=None, carrier=None,
                                          expected_date=None, locale=locale), str(exc)
    response = templates.TemplateResponse(request, "features/buyer_note/index.html", {
        "dispute_id": dispute_id, "capture_id": capture_id, "locale": locale, "note": note,
        "responses": service.responses(config.db_path(), dispute_id), "csrf_token": token,
        "error": error, "can_paypal_draft": bool(facts.get("currency")),
        "trade_profile": _trade_profile()}, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.get("/disputes/{dispute_id}/buyer-note", response_class=HTMLResponse)
def buyer_note_page(request: Request, dispute_id: str, capture_id: str = "", locale: str = "en"):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if capture_id and validate_capture_id(capture_id):
        capture_id = ""
    if locale not in ("en", "es"):
        locale = "en"
    return _render(request, dispute_id, capture_id, locale)


@router.post("/disputes/{dispute_id}/buyer-note/response", response_class=HTMLResponse)
def record_buyer_response(request: Request, dispute_id: str, state: str = Form(default=""),
                          capture_id: str = Form(default=""), locale: str = Form(default="en"),
                          csrf_token: str = Form(default="")):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not verify_csrf(request, csrf_token):
        return _refuse(request, "Reload this page and try again.", 403)
    if capture_id and validate_capture_id(capture_id):
        capture_id = ""
    try:
        service.record_response(config.db_path(), dispute_id, state)
    except (ValueError, OSError) as exc:
        return _render(request, dispute_id, capture_id, locale if locale in ("en", "es") else "en",
                       error=str(exc), status_code=400)
    query = urlencode({"capture_id": capture_id, "locale": locale})
    return RedirectResponse(f"/disputes/{dispute_id}/buyer-note?{query}", status_code=303)


@router.post("/disputes/{dispute_id}/buyer-note/draft", response_class=HTMLResponse)
def prepare_paypal_message(request: Request, dispute_id: str, capture_id: str = Form(default=""),
                           locale: str = Form(default="en"), csrf_token: str = Form(default="")):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    if not verify_csrf(request, csrf_token):
        return _refuse(request, "Reload this page and try again.", 403)
    if capture_id and validate_capture_id(capture_id):
        capture_id = ""
    if locale not in ("en", "es"):
        locale = "en"
    facts = _seller_facts(dispute_id, capture_id)
    note = service.build_note(**{key: value for key, value in facts.items() if key != "currency"}, locale=locale)
    if not facts.get("currency"):
        return _render(request, dispute_id, capture_id, locale,
                       error="No recorded sale currency is available; no PayPal message proposal was created.", status_code=409)
    from copilot.features.negotiation_drafts import service as drafts
    data = {"locale": locale, "recipient": "Buyer", "currency": facts["currency"],
            "allowed_actions": ["message"],
            "facts": [{"id": "buyer_note", "text": note["text"],
                "source": "seller-reviewed bilingual note from local recorded shipment facts"}],
            "proposals": [{"id": "buyer-note-followup", "kind": "followup", "amount_cents": 0,
                "conditions": "Review the exact draft; current PayPal permission and human-bound approval are required before any separate send.",
                "created_at": datetime.now(timezone.utc).isoformat()}],
            "responses": []}
    try:
        result = drafts.build(data, drafts.case_history_path(dispute_id))
    except (ValueError, OSError) as exc:
        return _render(request, dispute_id, capture_id, locale, error=str(exc), status_code=400)
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/negotiation_drafts/index.html", {
        "raw": json.dumps(data, ensure_ascii=False, indent=2), "result": result, "error": None,
        "csrf": token, "buyer_note_proposal": True, "trade_profile": _trade_profile()})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.get("/fulfilment/{capture_id}/buyer-note", response_class=HTMLResponse)
def sale_buyer_note_page(request: Request, capture_id: str, locale: str = "en"):
    if validate_capture_id(capture_id):
        return _refuse(request, "Enter a valid sale transaction ID.", 400)
    from copilot.features.fulfilment import store as fulfilment_store
    shipment = fulfilment_store.get_file(capture_id)
    if shipment is None:
        return _refuse(request, "No saved local shipment record exists for this sale.", 404)
    package_id, has_package, package_input_ok = _package_reference(request)
    if not package_input_ok:
        return _refuse(request, "The selected package could not be found.", 404)
    locale = locale if locale in ("en", "es") else "en"
    dispute_id = ""
    sale_terms = {}
    shipment_capture_record = None
    if config.feature_enabled("ship_moment_capture"):
        try:
            from copilot.features.ship_moment_capture import service as shipment_capture_service
            shipment_capture_record = shipment_capture_service.read(capture_id)
            dispute_id = str((shipment_capture_record or {}).get("dispute_id") or "")
            if dispute_id:
                from copilot.features.sale_readiness import service as readiness
                linked = readiness.load_case_terms(dispute_id, capture_id)
                if linked and linked.get("dispute_id") == dispute_id and linked.get("capture_id") == capture_id:
                    sale_terms = linked.get("terms") if isinstance(linked.get("terms"), dict) else {}
                else:
                    dispute_id = ""
        except (OSError, TypeError, ValueError):
            dispute_id = ""
            sale_terms = {}
    try:
        note = service.sale_note(capture_id, shipment, locale, sale_terms)
    except ValueError as exc:
        return _refuse(request, str(exc), 409)
    shipment_capture = shipment_capture_record if isinstance(shipment_capture_record, dict) else None
    if shipment_capture and shipment_capture.get("capture_id") != capture_id:
        shipment_capture = None
    package_url = None
    if dispute_id:
        if has_package:
            selected = _explicit_sale_package(capture_id, package_id)
            if not selected or selected[0] != dispute_id:
                return _refuse(request, "The selected package could not be found.", 404)
            package_url = selected[1]
        else:
            packages = repo.list_packages(config.db_path(), dispute_id)
            if packages:
                latest = max(packages, key=lambda row: int(row["id"]))
                package_url = f"/disputes/{quote(dispute_id)}/packages/{latest['id']}#attachments"
    elif has_package:
        return _refuse(request, "The selected package could not be found.", 404)
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/buyer_note/sale.html", {
        "capture_id": capture_id, "locale": locale, "note": note,
        "responses": service.sale_responses(config.db_path(), capture_id), "csrf_token": token,
        "shipment_status": shipment.get("status", "unknown"), "shipment_capture": shipment_capture,
        "dispute_id": dispute_id, "package_url": package_url,
        "from_package": package_id if has_package else ""})
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.post("/fulfilment/{capture_id}/buyer-note/response", response_class=HTMLResponse)
async def record_sale_buyer_response(request: Request, capture_id: str, state: str = Form(default=""),
                               locale: str = Form(default="en"), csrf_token: str = Form(default="")):
    if validate_capture_id(capture_id):
        return _refuse(request, "Enter a valid sale transaction ID.", 400)
    if not verify_csrf(request, csrf_token):
        return _refuse(request, "Reload this page and try again.", 403)
    form_data = await request.form()
    package_id, has_package, package_input_ok = _package_reference(request, form_data)
    if not package_input_ok:
        return _refuse(request, "The selected package could not be found.", 404)
    if has_package and _explicit_sale_package(capture_id, package_id) is None:
        return _refuse(request, "The selected package could not be found.", 404)
    from copilot.features.fulfilment import store as fulfilment_store
    if fulfilment_store.get_file(capture_id) is None:
        return _refuse(request, "No saved local shipment record exists for this sale.", 404)
    try:
        service.record_sale_response(config.db_path(), capture_id, state)
    except (ValueError, OSError) as exc:
        return _refuse(request, str(exc), 400)
    locale = locale if locale in ("en", "es") else "en"
    query = {"locale": locale}
    if has_package:
        query["from_package"] = package_id
    return RedirectResponse(f"/fulfilment/{quote(capture_id)}/buyer-note?{urlencode(query)}", status_code=303)


@router.get("/fulfilment/{capture_id}/delivery-incident", response_class=HTMLResponse)
def delivery_incident_page(request: Request, capture_id: str, from_dispute: str = "",
                           incident: str = "delivered_not_received", locale: str = "en"):
    if incident not in service.DELIVERY_INCIDENTS and not (incident == "buyer_question" and config.feature_enabled("incident_proposals")):
        return _refuse(request, "Choose a supported delivery issue.", 400)
    locale = locale if locale in ("en", "es") else "en"
    package_id, _has_package, input_ok = _package_reference(request)
    if not input_ok:
        return _refuse(request, "The selected package could not be found.", 404)
    context, error, status = _delivery_incident_context(capture_id, from_dispute, package_id)
    if error:
        return _refuse(request, error, status)
    return _delivery_incident_page(request, context, incident, locale)


@router.post("/fulfilment/{capture_id}/delivery-incident/response", response_class=HTMLResponse)
async def record_delivery_incident_response(request: Request, capture_id: str,
                                            incident: str = Form(default=""),
                                            state: str = Form(default=""),
                                            locale: str = Form(default="en"),
                                            from_dispute: str = Form(default=""),
                                            csrf_token: str = Form(default="")):
    if validate_capture_id(capture_id):
        return _refuse(request, "Enter a valid sale transaction ID.", 400)
    if not verify_csrf(request, csrf_token):
        return _refuse(request, "Reload this page and try again.", 403)
    if incident not in service.DELIVERY_INCIDENTS or state not in service.DELIVERY_INCIDENT_RESPONSES:
        return _refuse(request, "Choose a delivery issue and seller-recorded response.", 400)
    locale = locale if locale in ("en", "es") else "en"
    form_data = await request.form()
    query_disputes = request.query_params.getlist("from_dispute")
    form_disputes = form_data.getlist("from_dispute")
    if len(query_disputes) > 1 or len(form_disputes) > 1 or (query_disputes and form_disputes
            and query_disputes[0] != form_disputes[0]):
        return _refuse(request, "The selected case could not be found.", 404)
    submitted_dispute = (form_disputes or query_disputes or [from_dispute])[0]
    package_id, has_package, input_ok = _package_reference(request, form_data)
    if not input_ok:
        return _refuse(request, "The selected package could not be found.", 404)
    context, error, status = _delivery_incident_context(capture_id, submitted_dispute, package_id)
    if error:
        return _refuse(request, error, status)
    if not context.get("case_linked"):
        return _delivery_incident_page(request, context, incident, locale,
                                       error="Save schema-1 readiness for this exact case and sale before recording a case-linked response or associating shipment originals.",
                                       status_code=409)
    shipment = context["shipment"]
    try:
        trackers = json.loads(shipment.get("request_json") or "{}").get("trackers") or []
        tracker = next((row for row in trackers if isinstance(row, dict)
                        and row.get("transaction_id") == capture_id), {})
    except (TypeError, ValueError, AttributeError):
        tracker = {}
    readiness_record = context.get("terms_record") or {}
    readiness_terms = readiness_record.get("terms") if isinstance(readiness_record.get("terms"), dict) else {}
    note = service.sale_note(capture_id, shipment, locale, readiness_terms)
    details = {"capture_id": capture_id, "incident": incident, "response": state,
               "package_id": int(context["package_id"]) if context.get("package_id") else None,
               "source": "seller_recorded", "carrier_code": tracker.get("carrier"),
               "carrier_name_other": tracker.get("carrier_name_other"),
               "carrier_warning": note.get("carrier_warning"),
               "readiness_schema_version": 1,
               "source_hashes": {"tracking_request": context.get("tracking_source_sha256"),
                                 "readiness_terms": context.get("readiness_source_sha256"),
                                 "shipment_photos": context.get("photo_source_sha256", {})}}
    repo.log_action(config.db_path(), "vendor", "delivery_incident_response",
                    context["dispute_id"], details)
    query = {"from_dispute": context["dispute_id"], "incident": incident, "locale": locale}
    if has_package:
        query["from_package"] = package_id
    return RedirectResponse(f"/fulfilment/{quote(capture_id)}/delivery-incident?{urlencode(query)}", status_code=303)


def _incident_review_page(request, context, incident, locale, seller_text, *, error=None, proposal=None, status_code=200):
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/buyer_note/incident_review.html", {
        "context": context, "incident": incident, "locale": locale, "seller_text": seller_text,
        "error": error, "proposal": proposal, "csrf_token": token,
    }, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


def _incident_current(capture_id, dispute_id, package_id, locale, client):
    from copilot.features.resolution.service import page_state
    from copilot.features.local_autopilot.gap_resolution import current_case
    from copilot.features.negotiation_drafts.incident_binding import incident_fingerprint
    context, error, status = _delivery_incident_context(capture_id, dispute_id, package_id)
    if error or not context.get("case_linked") or not context.get("package_id"):
        raise ValueError("The same case, sale and package must be linked before continuing.")
    state = page_state(client, dispute_id)
    if not state.norm or not state.actions.message:
        raise ValueError("A message is not currently available; copy and review locally instead.")
    gap = current_case(config.db_path(), dispute_id, int(context["package_id"]), state.norm, capture_id=capture_id, locale=locale)
    if not gap.get("ok"):
        raise ValueError("The recorded case and sale could not be checked.")
    return context, state, gap, incident_fingerprint


@router.post("/fulfilment/{capture_id}/delivery-incident/proposal", response_class=HTMLResponse)
def incident_proposal(request: Request, capture_id: str, from_dispute: str = Form(default=""),
                      from_package: str = Form(default=""), incident: str = Form(default=""),
                      locale: str = Form(default=""), seller_text: str = Form(default=""),
                      csrf_token: str = Form(default=""), client=Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return _refuse(request, "Reload this form.", 403)
    if not all(config.feature_enabled(flag) for flag in ("incident_proposals", "negotiation_drafts", "resolution", "resolution_writes")):
        return _refuse(request, "Proposal preparation is unavailable.", 404)
    fallback = {"capture_id": capture_id, "dispute_id": from_dispute, "package_id": from_package}
    try:
        context, state, gap, fingerprint = _incident_current(capture_id, from_dispute, from_package or None, locale, client)
        context["gap_context"] = gap
        # Only replace the untouched neutral placeholder; never overwrite a seller edit.
        neutral = {"I am reviewing the information recorded for this sale.", "Estoy revisando los datos registrados de esta venta."}
        if seller_text in neutral:
            from copilot.features.buyer_note.incident_proposal import initial_message
            seller_text = initial_message(incident, locale, gap)
        if incident not in service.DELIVERY_INCIDENTS | {"buyer_question"} or locale not in ("en", "es"):
            raise ValueError("Choose the issue and language explicitly.")
        from copilot.features.negotiation_drafts import service as drafts
        bound = fingerprint(gap_context=gap, incident=incident, locale=locale, seller_text=seller_text, readiness_record=context["terms_record"], shipment_record=context["shipment"])
        facts = drafts.case_facts(state.norm)
        proposal = {"id": "incident-" + bound[:24], "kind": "information", "seller_text": seller_text,
                    "source_incident": {"fingerprint": bound, "incident": incident, "locale": locale}}
        result = drafts.build({"locale": locale, "recipient": "Buyer", "currency": state.norm.get("currency"),
                               "allowed_actions": ["message"], "facts": facts, "proposals": [proposal], "responses": []},
                              drafts.case_history_path(from_dispute))
        return _incident_review_page(request, context, incident, locale, seller_text, proposal=result["proposals"][0])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _incident_review_page(request, fallback, incident, locale, seller_text, error=str(exc), status_code=409)


@router.post("/fulfilment/{capture_id}/delivery-incident/preview", response_class=HTMLResponse)
def incident_preview(request: Request, capture_id: str, from_dispute: str = Form(default=""),
                     from_package: str = Form(default=""), incident: str = Form(default=""),
                     locale: str = Form(default=""), logical_id: str = Form(default=""),
                     seller_text: str = Form(default=""), csrf_token: str = Form(default=""), client=Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return _refuse(request, "Reload this form.", 403)
    if not all(config.feature_enabled(flag) for flag in ("incident_proposals", "negotiation_drafts", "resolution", "resolution_writes")):
        return _refuse(request, "Preview is unavailable.", 404)
    fallback = {"capture_id": capture_id, "dispute_id": from_dispute, "package_id": from_package}
    try:
        context, state, gap, fingerprint = _incident_current(capture_id, from_dispute, from_package or None, locale, client)
        from copilot.features.negotiation_drafts.service import read_case_history
        proposal = next((row for row in read_case_history(from_dispute) if row.get("logical_id") == logical_id), None)
        bound = fingerprint(gap_context=gap, incident=incident, locale=locale, seller_text=seller_text, readiness_record=context["terms_record"], shipment_record=context["shipment"])
        if not proposal or proposal.get("draft") != seller_text or proposal.get("source_incident") != {"fingerprint": bound, "incident": incident, "locale": locale}:
            raise ValueError("The sources or draft changed; review and prepare a new proposal. Your text remains below.")
        from copilot.features.resolution.routes import preview
        return preview(request, from_dispute, csrf_token=csrf_token, action="message", amount="", note="", text=seller_text, client=client)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _incident_review_page(request, fallback, incident, locale, seller_text, error=str(exc), status_code=409)
