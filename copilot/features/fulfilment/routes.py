"""/fulfilment routes. Registered in copilot/app.py behind COPILOT_FEATURE_FULFILMENT (the import is lazy, inside that
flag block). The only PayPal write is POST /v1/shipping/trackers, reached only from register_file, after the seller
approved the exact stored body (the check page's token and its sha256 travel in the confirm form; the body itself is
loaded from the stored check, never rebuilt from form fields).
COPILOT_FEATURE_FULFILMENT_REGISTER (default off) switches that write off: the file is then saved without any
registration (the fallback when registration is not wanted).

Flow: form -> POST preview (validate, store the check, 303) -> GET check page (renders the stored body)
-> POST register (load the stored body, verify its sha256, register_once, 303) -> GET file page (renders the stored
row, never calls PayPal). POST check re-reads PayPal for a file whose outcome is not confirmed and appends a new file
version; it sends nothing."""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core import carriers, checklist, eligibility, write_guard
from copilot.core.context import SRC_SEARCH, SRC_TRACKING, fetch_context
from copilot.core.disputes import SandboxClient, readback_audit
from copilot.db import repo
from copilot.features.fulfilment import register as reg
from copilot.features.fulfilment import store
from copilot.features.fulfilment.checks import pre_dispute_checks
from copilot.features.fulfilment.register import AlreadyRegistered, read_trackers, read_trackers_listing, register_once
from copilot.features.fulfilment.tracker_request import (STATUS_LABELS, STATUSES, body_from_form, declared_address,
                                                         form_problems, request_sha256, validate_capture_id)
from copilot.web import status as web_status
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, set_csrf_cookie, validate_dispute_id, verify_csrf

router = APIRouter()

ADDRESS_FIELDS = ("line1", "city", "state", "postal_code", "country_code")
CHECK_STATE_LABELS = {"verified": "Verified", "missing": "Missing", "present_unverifiable": "Present, not verifiable",
                      "conflict": "Does not match", "not_applicable": "Not applicable"}
EXPIRED_TITLE = "This check has expired"
EXPIRED_MESSAGE = "This check has expired. Nothing was sent."
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
STATUS_CHOICES = tuple((code, STATUS_LABELS[code]) for code in STATUSES)
USED_SUGGESTION_SENTENCE = "Filled in from your shipment details. Check them, then press Review what will be sent."
NO_TRACKER_NOTE = "No tracking number is registered in PayPal"
# The release rule shared with the resolution flow (copilot/core/write_guard.py). Trackers have no settle phase of
# their own, so the 10-minute floor governs.
RELEASE_MIN_AGE_SECONDS = write_guard.release_min_age()


def _register_enabled() -> bool:
    return config.feature_enabled("fulfilment_register")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bad_id(capture_id: str, request: Request) -> HTMLResponse | None:
    problem = validate_capture_id(capture_id)
    return refusal_page(request, 400, "Check the transaction id", problem,
                        back_url="/fulfilment", back_label="Back to tracking files") if problem else None


def _money(cents, currency) -> str | None:
    if not isinstance(cents, int):
        return None
    sign, cents = ("-", -cents) if cents < 0 else ("", cents)
    return f"{sign}{cents // 100}.{cents % 100:02d} {currency or ''}".strip()


def _transaction_view(client, capture_id: str) -> tuple[dict, dict]:
    """What PayPal tells about the capture (reads only); never raises for an expected failure. The second value
    says what each read did: payment_read ok | not_found | failed, tracking_read ok | failed."""
    now = datetime.now(timezone.utc)
    ctx = fetch_context(client, {"seller_transaction_ids": [capture_id], "create_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    view = {"payment_date": ctx.get("payment_date"),
            "amount": _money(ctx.get("total_amount_cents"), ctx.get("total_currency")),
            "total_amount_cents": ctx.get("total_amount_cents"), "total_currency": ctx.get("total_currency"),
            "recipient_name": ctx.get("recipient_name"), "shipping_address": ctx.get("shipping_address"),
            "tracking_number": ctx.get("tracking_number")}
    reads = {"payment_read": "ok", "tracking_read": "ok"}
    for lim in ctx.limitations:
        if lim.source == SRC_SEARCH:
            if lim.message.startswith("PayPal returned no transaction details for this transaction"):
                reads["payment_read"] = "not_found"
            elif "shipping address" not in lim.message and reads["payment_read"] == "ok":
                reads["payment_read"] = "failed"
        elif lim.source == SRC_TRACKING and not lim.message.startswith(NO_TRACKER_NOTE):
            reads["tracking_read"] = "failed"
    return view, reads


def _policy(transaction: dict) -> dict:
    try:
        rules = checklist.load_rules(country=config.RULES_COUNTRY)
        return eligibility.shipment_information(rules, transaction.get("total_amount_cents"),
                                                transaction.get("total_currency"))
    except checklist.UnknownCountryError:
        return {"available": False}


def _page(request, name: str, context: dict, status_code: int = 200, csrf: str | None = None, no_store: bool = False):
    if name == "form.html":
        context = {**context, "shipment_policy": _policy(context.get("transaction") or {})}
    response = templates.TemplateResponse(request, f"features/fulfilment/{name}", context, status_code=status_code)
    if csrf:
        set_csrf_cookie(response, csrf)
    if no_store:
        response.headers["Cache-Control"] = "no-store"
    return response


def _expired_page(request, capture_id: str, status_code: int):
    return refusal_page(request, status_code, EXPIRED_TITLE, EXPIRED_MESSAGE, back_url=f"/fulfilment/{capture_id}",
                        back_label="Prepare the tracking again")


# ----------------------------------------------------------------------------------- what is on record
def _tracker_of(row: dict) -> dict:
    return json.loads(row["request_json"])["trackers"][0]


def _file_notice(row: dict | None) -> dict | None:
    """The 'already sent' notice for one stored file, built from records only: the file shows a send (registered,
    accepted but unconfirmed, unknown), or the reservation for its exact body is taken. Never 'sent to PayPal' for a
    file that was only saved."""
    if row is None:
        return None
    eff = store.effective_status(row)
    reservation = write_guard.lookup(row["capture_id"], reg.ACTION, row["request_sha256"])
    if eff == "saved_without_registration":
        return None
    if eff in store.SENT_POSSIBLE:
        kind = {"registered": "sent", "registered_unconfirmed": "accepted", "unknown": "unknown"}[eff]
        return {"kind": kind, "created_at": row["created_at"], "file_id": row["id"]}
    if write_guard.is_blocking(reservation):
        return {"kind": "in_flight", "created_at": row["created_at"], "file_id": row["id"]}
    return None


def _sha_notice(capture_id: str, sha: str) -> dict | None:
    """The same notice for an exact body (the form page and the check page): the stored file of that body, else the
    reservation."""
    if not SHA_RE.match(sha or ""):
        return None
    sent = store.latest_sent_for_request(capture_id, sha)
    if sent is not None:
        return _file_notice(sent)
    reservation = write_guard.lookup(capture_id, reg.ACTION, sha)
    if write_guard.is_blocking(reservation):
        return {"kind": "in_flight", "created_at": None, "file_id": None}
    return None


def _existing_view(existing: dict | None) -> dict | None:
    if existing is None:
        return None
    eff = store.effective_status(existing)
    view = web_status.tracking_status(eff)
    return {"row": existing, "status": eff, "label": view.label, "kind": view.kind}


# ------------------------------------------------------------------------------------------------ the form
def _form_values(form: dict) -> dict:
    keys = ("tracking_number", "carrier", "carrier_other_name", "status", "declared_line1", "declared_city",
            "declared_state", "declared_postal_code", "declared_country_code", "declared_country_other")
    return {k: str(form.get(k) or "") for k in keys}


def _form_context(capture_id: str, values: dict, errors: dict, token: str, **extra) -> dict:
    address_open = any(values.get(f"declared_{k}") for k in ADDRESS_FIELDS) or bool(values.get("declared_country_other")) \
        or any(key.startswith("declared_") for key in errors)
    return {"capture_id": capture_id, "csrf_token": token, "values": values, "form_errors": errors,
            "statuses": STATUS_CHOICES, "register_enabled": _register_enabled(), "address_open": address_open,
            "from_token": "", "notice": None, "error_page": False, "reads": {}, "transaction": {},
            "existing": None, "dispute_suggestion": None, "suggestion_href": None, "from_dispute": "", **extra}


def _valid_dispute(value) -> str:
    """The dispute id a page was reached from, or "" when it is not an id of PayPal's shape (checked whole)."""
    return value if isinstance(value, str) and value and not validate_dispute_id(value) else ""


def _file_url(capture_id: str, file_id, dispute_id: str = "", extra: str = "") -> str:
    query = [part for part in (extra, f"from_dispute={dispute_id}" if dispute_id else "") if part]
    return f"/fulfilment/{capture_id}/file/{file_id}" + ("?" + "&".join(query) if query else "")


def _dispute_suggestion(dispute_id: str, use: bool) -> dict | None:
    """the tracking number and carrier the seller already typed on the package page of
    this dispute, offered here as a suggestion. A local read of the dispute's own saved shipment fields: no PayPal call, nothing
    saved, nothing prefilled unless the seller follows the 'Use these values' link (use=True), and even then the seller still
    presses 'Review what will be sent'. None when the id has no PayPal shape or the dispute holds neither value."""
    if not dispute_id or validate_dispute_id(dispute_id):
        return None
    fields = repo.get_manual_fields(config.db_path(), dispute_id)

    def saved(name: str) -> str:
        value = (fields.get(name) or {}).get("value") if isinstance(fields.get(name), dict) else None
        return " ".join(str(value).split()) if value is not None else ""
    number, carrier_text = saved("tracking_number"), saved("carrier")
    if not number and not carrier_text:
        return None
    match = carriers.map_carrier(carrier_text) if carrier_text else None
    carrier_values = ({"carrier": match.code or "", "carrier_other_name": match.carrier_name_other or ""}
                      if match else {"carrier": "", "carrier_other_name": ""})
    shown = carriers.display_name(carrier_text)
    saved_words = (f"tracking {number} ({shown})" if number and shown else f"tracking {number}" if number
                   else f"the carrier {shown}")
    return {"dispute_id": dispute_id, "tracking_number": number, "carrier": shown, "used": use,
            "sentence": f"You saved {saved_words} on the package page for dispute {dispute_id}.",
            "used_sentence": USED_SUGGESTION_SENTENCE, "values": {"tracking_number": number, **carrier_values}}


def _query_prefill(tracking_number: str, carrier: str, carrier_name: str, status: str = "") -> dict | None:
    """Values another page of the app passed in the link (the shipment-time page). Shown as a suggestion the seller
    reviews: nothing is saved, nothing is sent, and the usual checks still run when the seller presses 'Review what will
    be sent'. A value that is not a clean tracking number or a catalogue code is dropped. None when no number is left."""
    number = "".join(str(tracking_number or "").split())
    if not number or len(number) > carriers.TRACKING_MAX_LENGTH or not number.isprintable():
        return None
    code = str(carrier or "").strip()
    code = code if code in carriers.KNOWN_CODES or code == "OTHER" else ""
    name = " ".join(str(carrier_name or "").split())[:carriers.CARRIER_NAME_MAX_LENGTH] if code == "OTHER" else ""
    shown = carriers.display_name(code) if code and code != "OTHER" else name
    return {"tracking_number": number, "carrier": shown, "used": True,
            "sentence": f"You saved tracking {number}" + (f" ({shown})" if shown else "") + " on the shipment-time page of this sale.",
            "used_sentence": USED_SUGGESTION_SENTENCE,
            "values": {"tracking_number": number, "carrier": code, "carrier_other_name": name,
                       **({"status": status} if status in STATUSES else {})}}


@router.get("/fulfilment", response_class=HTMLResponse)
def list_page(request: Request):
    files = [{"row": f, "status": store.effective_status(f)} for f in store.list_files()]
    return _page(request, "list.html", {"files": files, "register_enabled": _register_enabled(),
                                        "tracking_status": web_status.tracking_status})


@router.get("/fulfilment/open")
def open_capture(request: Request, capture_id: str = ""):
    capture_id = re.sub(r"\s+", "", capture_id)
    bad = _bad_id(capture_id, request)
    return bad if bad else RedirectResponse(f"/fulfilment/{capture_id}", status_code=303)


def _readiness_package(request: Request, dispute_id: str, package_id: str):
    """Resolve an explicit editable package; invalid references never fall back to latest."""
    from copilot.web.routes_package import _is_sent_version
    if not isinstance(package_id, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", package_id):
        return None, refusal_page(request, 404, "Package not found", "That package could not be found.",
                                  back_url=f"/disputes/{dispute_id}", back_label="Go back to the dispute")
    package = repo.get_package(config.db_path(), int(package_id))
    if not package or package.get("dispute_id") != dispute_id:
        return None, refusal_page(request, 404, "Package not found", "That package could not be found.",
                                  back_url=f"/disputes/{dispute_id}", back_label="Go back to the dispute")
    if _is_sent_version(config.db_path(), dispute_id, package):
        return None, refusal_page(request, 409, "This version was sent", "This version is read-only.",
                                  back_url=f"/disputes/{dispute_id}/packages/{package_id}", back_label="Go back to this package")
    return package, None


@router.get("/fulfilment/{capture_id}", response_class=HTMLResponse)
def form_page(request: Request, capture_id: str, already: str = "", from_token: str = Query(default="", alias="from"),
              from_dispute: str = "", from_package: str = "", use: str = "", prefill_tracking: str = "",
              prefill_carrier: str = "", prefill_carrier_name: str = "", prefill_status: str = "", client: SandboxClient = Depends(get_client)):
    bad = _bad_id(capture_id, request)
    if bad:
        return bad
    view, reads = _transaction_view(client, capture_id)
    token = new_csrf_token()
    earlier = store.get_check(capture_id, from_token) if from_token else None
    values = _form_values(earlier["form"]) if earlier else _form_values({})
    from_dispute = _valid_dispute(from_dispute) or _valid_dispute((earlier or {}).get("form", {}).get("from_dispute"))
    suggestion = None if earlier else _dispute_suggestion(from_dispute, use == "1")
    suggestion_href = f"/fulfilment/{capture_id}?from_dispute={from_dispute}&use=1" if suggestion else None
    if suggestion is None and not earlier and prefill_tracking:
        suggestion = _query_prefill(prefill_tracking, prefill_carrier, prefill_carrier_name, prefill_status)
        suggestion_href = None
    if suggestion and suggestion["used"]:
        values = _form_values({**values, **suggestion["values"]})
    context = _form_context(capture_id, values, {}, token, transaction=view, reads=reads, dispute_suggestion=suggestion,
                            suggestion_href=suggestion_href,
                            existing=_existing_view(store.get_file(capture_id)),
                            notice=_sha_notice(capture_id, already),
                            from_token=from_token if earlier else "", from_dispute=from_dispute)
    if from_package and (not config.feature_enabled("sale_readiness") or not from_dispute):
        return refusal_page(request, 400, "Package unavailable", "A valid case is required for this package.",
                            back_url="/disputes", back_label="Go back to disputes")
    if config.feature_enabled("sale_readiness") and from_dispute:
        try:
            from copilot.features.sale_readiness import service as readiness
            from copilot.features.fulfilment import store as fulfilment_store
            terms = readiness.load_case_terms(from_dispute, capture_id)
            documents = repo.list_documents(config.db_path(), from_dispute)
            packages = repo.list_packages(config.db_path(), from_dispute)
            latest = max(packages, key=lambda p: (p["version"], p["id"])) if packages else None
            selected = latest
            if from_package:
                selected, refusal = _readiness_package(request, from_dispute, from_package)
                if refusal is not None:
                    return refusal
                from copilot.core.disputes import normalize
                current = normalize(client.call("GET", f"/v1/customer/disputes/{from_dispute}", "show_dispute"),
                                    datetime.now(timezone.utc))
                if current.get("dispute_id") != from_dispute or capture_id not in (current.get("seller_transaction_ids") or []):
                    return refusal_page(request, 400, "Sale not linked", "This sale is not linked to this case.",
                                        back_url=f"/disputes/{from_dispute}", back_label="Go back to the dispute")
            attachments = repo.list_attachments(config.db_path(), selected["id"]) if selected else []
            saved_file = fulfilment_store.get_file(capture_id)
            saved_tracking = None
            if saved_file:
                try:
                    body = json.loads(saved_file.get("request_json") or "{}")
                    tracker = (body.get("trackers") or [{}])[0]
                    saved_tracking = {"tracking_number": tracker.get("tracking_number"), "carrier": tracker.get("carrier")}
                except (TypeError, ValueError, IndexError):
                    saved_tracking = None
            context["sale_readiness_view"] = readiness.case_view(from_dispute, capture_id, view, documents,
                                                                  attachments, terms, saved_tracking)
            context["sale_readiness_dispute"] = from_dispute
            context["case_terms_saved"] = terms is not None
            context["sale_readiness_documents"] = documents
            context["sale_readiness_terms"] = (terms or {}).get("terms", {})
            context["sale_readiness_package_url"] = (f"/disputes/{from_dispute}/packages/{selected['id']}#attachments"
                                                       if selected else "")
            context["sale_readiness_package_id"] = str(selected["id"]) if from_package and selected else ""
        except (OSError, TypeError, ValueError) as exc:
            context["sale_readiness_error"] = str(exc)
    return _page(request, "form.html", context, csrf=token)


def _form_error(request, capture_id, values, errors, status_code=400):
    token = new_csrf_token()
    context = _form_context(capture_id, values, errors, token, error_page=True,
                            existing=_existing_view(store.get_file(capture_id)),
                            from_token=values.get("from_token", ""), from_dispute=values.get("from_dispute", ""))
    return _page(request, "form.html", context, status_code, csrf=token)


@router.post("/fulfilment/{capture_id}/preview", response_class=HTMLResponse)
def preview(request: Request, capture_id: str, csrf_token: str = Form(default=""), tracking_number: str = Form(default=""),
            carrier: str = Form(default=""), carrier_other_name: str = Form(default=""), status: str = Form(default=""),
            declared_line1: str = Form(default=""), declared_city: str = Form(default=""),
            declared_state: str = Form(default=""), declared_postal_code: str = Form(default=""),
            declared_country_code: str = Form(default=""), declared_country_other: str = Form(default=""),
            from_token: str = Form(default=""), from_dispute: str = Form(default=""),
            client: SandboxClient = Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad = _bad_id(capture_id, request)
    if bad:
        return bad
    values = _form_values({"tracking_number": tracking_number, "carrier": carrier, "carrier_other_name": carrier_other_name,
                           "status": status, "declared_line1": declared_line1, "declared_city": declared_city,
                           "declared_state": declared_state, "declared_postal_code": declared_postal_code,
                           "declared_country_code": declared_country_code,
                           "declared_country_other": declared_country_other})
    values["from_token"] = from_token
    values["from_dispute"] = _valid_dispute(from_dispute)
    declared, address_errors = declared_address(values)
    errors = {**form_problems(tracking_number, carrier, status, carrier_other_name), **address_errors}
    if errors:
        return _form_error(request, capture_id, values, errors)
    body = body_from_form(capture_id, tracking_number, carrier, status, carrier_other_name)
    number = body["trackers"][0]["tracking_number"]
    notes: dict = {}
    if _register_enabled():
        # PayPal's own list is read (a GET) so a number it already lists is refused before anything is sent.
        listed, read_limits = read_trackers(client, capture_id)
        if any(str(t.get("tracking_number", "")).strip() == number for t in listed):
            errors = {"tracking_number": f"PayPal already lists tracking {number} for this sale. Nothing was sent."}
            return _form_error(request, capture_id, values, errors)
        if read_limits:
            notes["trackers_read_failed_at"] = _now_iso()
    check_token = store.save_check(capture_id, body, request_sha256(body), declared,
                                   {k: v for k, v in values.items() if k != "from_token"}, notes)
    if from_token:
        store.supersede_check(capture_id, from_token)
    return RedirectResponse(f"/fulfilment/{capture_id}/check/{check_token}#preview", status_code=303)


# ---------------------------------------------------------------------------------------- the check page
def _carrier_rows(tracker: dict) -> dict:
    code = tracker["carrier"]
    if code == "OTHER":
        name = tracker.get("carrier_name_other") or ""
        return {"text": f"Other carrier: {name} (not in PayPal's list)",
                "aggregator": carriers.map_carrier(name).aggregator}
    shown = carriers.display_name(code)
    return {"text": f"Carrier: {shown} (PayPal code {code})", "aggregator": False}


@router.get("/fulfilment/{capture_id}/check/{token}", response_class=HTMLResponse)
def check_page(request: Request, capture_id: str, token: str):
    """Renders the stored body; makes no PayPal call and sends nothing."""
    bad = _bad_id(capture_id, request)
    if bad:
        return bad
    check = store.get_check(capture_id, token)
    if check is None:
        return _expired_page(request, capture_id, 404)
    tracker = check["body"]["trackers"][0]
    sha = check["request_sha256"]
    typed = (check["form"].get("tracking_number") or "").strip()
    csrf = new_csrf_token()
    notice = _sha_notice(capture_id, sha)
    ups_advisory = tracker["carrier"] == "UPS" and not re.fullmatch(r"1Z[0-9A-Za-z]{16}", tracker["tracking_number"])
    return _page(request, "check.html", {
        "capture_id": capture_id, "csrf_token": csrf, "token": token, "sha256": sha, "tracker": tracker,
        "body_text": json.dumps(check["body"], sort_keys=True, ensure_ascii=False), "declared": check["declared"],
        "carrier": _carrier_rows(tracker), "status_label": STATUS_LABELS.get(tracker["status"], "Not available"),
        "sent_as": tracker["tracking_number"] if typed and typed != tracker["tracking_number"] else None,
        "notes": check["notes"], "notice": notice, "ups_advisory": ups_advisory,
        "delivered_warning": tracker["status"] == "DELIVERED", "register_enabled": _register_enabled()},
        csrf=csrf, no_store=True)


# ------------------------------------------------------------------------------------------ the register
@router.post("/fulfilment/{capture_id}/register", response_class=HTMLResponse)
def register_file(request: Request, capture_id: str, csrf_token: str = Form(default=""), token: str = Form(default=""),
                  request_sha256_submitted: str = Form(default="", alias="request_sha256"),
                  client: SandboxClient = Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad = _bad_id(capture_id, request)
    if bad:
        return bad
    check = store.get_check(capture_id, token)
    if check is None:
        return _expired_page(request, capture_id, 409)
    body, sha = check["body"], check["request_sha256"]
    if request_sha256(body) != sha or not secrets.compare_digest(request_sha256_submitted.encode("utf-8"),
                                                                  sha.encode("utf-8")):
        return refusal_page(request, 409, "What you approved is not what would be sent",
                            "The approved request no longer matches the stored check, so nothing was sent to PayPal. "
                            "Prepare the request again and approve it.",
                            back_url=f"/fulfilment/{capture_id}", back_label="Back to the form")
    declared = check["declared"]
    post_result = None
    if _register_enabled():
        result = register_once(client, body, sha)
        if isinstance(result, AlreadyRegistered):
            # the same approved body is registered once. Nothing was POSTed; PRG to the stored file that
            # could have reached PayPal, else to the form, whose notice comes from the reservation.
            earlier = store.latest_sent_for_request(capture_id, sha)
            target = f"/fulfilment/{capture_id}/file/{earlier['id']}?already=1" if earlier \
                else f"/fulfilment/{capture_id}?already={sha}"
            return RedirectResponse(target, status_code=303)
        file_status, readback, post_result = result.status, result.tracker, result
        limitation_text = " ".join(lim.message for lim in result.limitations) or None
        if readback is not None:
            readback = {**readback, "read_at": _now_iso()}
    else:
        file_status, readback, limitation_text = "saved_without_registration", None, None
    try:
        checks = pre_dispute_checks(client, capture_id, declared or None).to_json()
    except Exception:  # noqa: BLE001 - the POST already happened: the record of it must still be saved
        checks = {"checks": [], "not_evaluable": [], "protection_eligibility": None, "address_comparison": {},
                  "limitations": [{"source": "checks", "message": "Proofie could not read PayPal for the checks at the sale."}],
                  "rules": {}}
    if post_result is not None and post_result.status == reg.STATUS_FAILED:
        checks = {**checks, "post_result": {"http_status": post_result.post_status, "reason": post_result.reason}}
    saved = store.save_file(capture_id, body, sha, sha, readback, checks, file_status, limitation_text)
    # post/redirect/get: the result is a GET of the stored file, so a refresh or Back cannot repeat the write
    return RedirectResponse(_file_url(capture_id, saved["id"], _valid_dispute(check["form"].get("from_dispute"))),
                            status_code=303)


# ---------------------------------------------------------------------------------------- check PayPal again
@router.post("/fulfilment/{capture_id}/check", response_class=HTMLResponse)
def check_paypal_again(request: Request, capture_id: str, csrf_token: str = Form(default=""),
                       file_id: int = Form(default=0), from_dispute: str = Form(default=""),
                       client: SandboxClient = Depends(get_client)):
    """Reads the trackers of the sale from PayPal (a GET) and appends a NEW file version with what PayPal said. It
    sends nothing and never updates a stored row."""
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad = _bad_id(capture_id, request)
    if bad:
        return bad
    saved = store.get_file_by_id(capture_id, file_id)
    if saved is None:
        return refusal_page(request, 404, "File not found", "No saved tracking file with that number exists for this transaction.",
                            back_url="/fulfilment", back_label="Back to tracking files")
    eff = store.effective_status(saved)
    dispute = _valid_dispute(from_dispute)
    if eff not in ("unknown", "registered_unconfirmed"):
        return RedirectResponse(_file_url(capture_id, saved["id"], dispute), status_code=303)
    number = _tracker_of(saved)["tracking_number"]
    sha = saved["request_sha256"]
    items, limits, complete = read_trackers_listing(client, capture_id,
                                                    log_extra=readback_audit(sha, sha, tracking_number=number))
    match = next((t for t in items if str(t.get("tracking_number", "")).strip() == number), None)
    checks = json.loads(saved["checks_json"])
    now = _now_iso()
    if match is None and complete and not limits:
        # PayPal answered with a complete listing and does not list the tracking. Its send-once
        # key is released only when the POST is old enough (the resolution rule); a send in flight is never touched.
        reservation = write_guard.lookup(capture_id, reg.ACTION, sha)
        posted_at = reservation["created_at"] if reservation else store.first_sent_epoch(capture_id, sha)
        age = time.time() - posted_at if posted_at else None
        if age is not None and age >= RELEASE_MIN_AGE_SECONDS and not (
                reservation and reservation["state"] == write_guard.RESERVED):
            changed = write_guard.release_after_absence(capture_id, reg.ACTION, sha)
            if changed or reservation is None:
                # every check page of this body prepared before the release is spent: a repeat needs a new one
                store.supersede_checks_for_request(capture_id, sha, time.time())
            checks["recheck"] = {"outcome": "not_listed", "read_at": now, "released": True,
                                 "minutes_after_send": int(age // 60)}
            new = store.save_file(capture_id, json.loads(saved["request_json"]), sha, saved["approval_sha256"],
                                  json.loads(saved["tracker_readback_json"]), checks, eff, saved["limitation_text"],
                                  audit_action="fulfilment_checked")
            return RedirectResponse(_file_url(capture_id, new["id"], dispute), status_code=303)
    if match is not None:
        checks.pop("recheck", None)
        new = store.save_file(capture_id, json.loads(saved["request_json"]), sha, saved["approval_sha256"],
                              {**reg._readback(match), "read_at": now}, checks, "registered", None,
                              audit_action="fulfilment_checked")
    else:
        outcome = "read_failed" if limits else "not_listed"
        checks["recheck"] = {"outcome": outcome, "read_at": now}
        new = store.save_file(capture_id, json.loads(saved["request_json"]), sha, saved["approval_sha256"],
                              json.loads(saved["tracker_readback_json"]), checks, eff, saved["limitation_text"],
                              audit_action="fulfilment_checked")
    return RedirectResponse(_file_url(capture_id, new["id"], dispute), status_code=303)


# ------------------------------------------------------------------------------------------- the file page
def _neutral_check(check: dict) -> str | None:
    """A label for a row that is not a failure at the sale: a document-only row before any dispute exists, or a row
    PayPal's own wording calls information only."""
    if "for information only" in (check.get("message") or "").lower():
        return "Not provided"
    if not check.get("paypal_sourced") and check.get("state") == "missing":
        return "Not yet"
    return None


@router.get("/fulfilment/{capture_id}/file/{file_id}", response_class=HTMLResponse)
def file_page(request: Request, capture_id: str, file_id: int, already: str = "", from_dispute: str = ""):
    """The saved result of one registration, rebuilt from the stored row only: no PayPal call."""
    bad = _bad_id(capture_id, request)
    if bad:
        return bad
    saved = store.get_file_by_id(capture_id, file_id)
    if saved is None:
        return refusal_page(request, 404, "File not found", "No saved tracking file with that number exists for this transaction.",
                            back_url="/fulfilment", back_label="Back to tracking files")
    checks = json.loads(saved["checks_json"])
    check_rows = []
    for c in checks.get("checks", []):
        neutral = _neutral_check(c)
        check_rows.append({**c, "state_label": neutral or CHECK_STATE_LABELS.get(c["state"], "Not available"),
                           "neutral": neutral is not None})
    eff = store.effective_status(saved)
    tracker = _tracker_of(saved)
    csrf = new_csrf_token()
    return _page(request, "result.html", {
        "capture_id": capture_id, "file": saved, "status": eff, "tracking_view": web_status.tracking_status(eff),
        "tracker": tracker, "carrier": _carrier_rows(tracker), "readback": json.loads(saved["tracker_readback_json"]),
        "limitation_text": saved["limitation_text"], "checks": check_rows, "check_data": checks,
        "post_result": checks.get("post_result") or {}, "recheck": checks.get("recheck"),
        "notice": _file_notice(saved) if already else None, "csrf_token": csrf,
        "register_enabled": _register_enabled(), "status_labels": STATUS_LABELS,
        "from_dispute": _valid_dispute(from_dispute)}, csrf=csrf)
