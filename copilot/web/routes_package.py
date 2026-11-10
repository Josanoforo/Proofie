"""Evidence package generation, review, traceable editing, and audited approval. The model only proposes
(copilot/core/evidence_model.py, reusing pilot/step4_evidence_ai.py unchanged) or is bypassed entirely in
manual mode; a human always reviews and approves before anything is ever sent (the send logic lives in
copilot/core/send_service.py; its routes are in routes_send.py).

Package flow: the page leads with one next-step panel (copilot/web/next_step.py, display only), every
in-page write answers 303 to its own section with a done key (confirmed against stored state before it is shown), a
version PayPal confirmed is read-only (every write route refuses it with 409), and the file list and the "what will
be sent" block read the one list the send reads (repo.list_attachments).
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from copilot import config
from copilot.core.checklist import UnknownCountryError, evaluate
from copilot.core import checklist_input
from copilot.core.checklist_input import MANUAL_FIELD_NAMES, cached_rules, docs_for_dispute, extracted_docs_for
from copilot.core.content_type import ALLOWED_CONTENT_TYPES, PROBLEM_TYPE, problem_message, validate_call_total
from copilot.core.context import fetch_context
from copilot.core.own_tracker import own_tracker_client
from copilot.core.eligibility import EFFECT_SENTENCE as ELIGIBILITY_EFFECT_SENTENCE, notes_for
from copilot.core.disputes import SandboxClient, humanize_evidence_type, normalize
from copilot.core.evidence_model import (claims_from_output, describe_delivery_problem,
                                         find_delivery_claims_citing_conflicting_sources,
                                         find_delivery_claims_citing_untrusted_documents,
                                         find_unsupported_delivery_assertions, propose_package,
                                         untrusted_document_ids)
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.auto_prep import AI_UNAVAILABLE_FLAG, ai_unavailable_flag  # noqa: F401  (the constant stays importable from here)
from copilot.core.send import (approval_fingerprint_with_attachments, build_request_input, pick_tracking,
                               sha256_json)
from copilot.core import send_service
from copilot.core.send_service import send_status_banner
from copilot.core.vision_model import claims_for_dispute_images
from copilot.db import repo
from copilot.web import field_words, links, next_step, status, timefmt
from copilot.web.dispute_state import REVIEWING_NEUTRAL_TEXT
from copilot.web.form_fields import shipment_fields, date_words
from copilot.web import case_workspace, presentation
from copilot.web.routes_disputes import compact_case_header, csrf_expired_page, get_client, mark_paypal_read, refusal_page
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, validate_dispute_id, verify_csrf

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["cdmx"] = timefmt.fmt_iso
templates.env.filters["datewords"] = date_words
templates.env.globals.update(state_kind=presentation.state_kind, state_label=presentation.state_label,
                             action_label=presentation.action_label, stage_label=presentation.stage_label)
templates.env.globals["hosted_demo"] = config.hosted_demo               # read on every render
templates.env.globals["hosted_demo_sentence"] = config.HOSTED_DEMO_SENTENCE
templates.env.globals["feature_enabled"] = config.feature_enabled       # always defined, flags read per render
templates.env.globals["valid_dispute_id"] = lambda value: validate_dispute_id(value) is None   # the nav only carries a well-formed case id
presentation.install(templates.env)                                         # shell: when/when_text filters, status and wording helpers


EMPTY_MANUAL_FLAG = "Manual package: nothing has been entered yet."
SENT_VERSION_REFUSAL = "This version was sent to PayPal, so it cannot change."
NOTE_TOO_LONG = "Note must be 500 characters or fewer"
NOTE_MAX_LENGTH = 500
INR_REASON = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
DUPLICATE_NAME_MESSAGE = ("A file named {name} is already attached to this package. PayPal lists attached files by name "
                          "only, so Proofie can confirm two files only when their names differ. Detach the other file "
                          "first, or upload this one again under another name.")


def policy_source():
    """One presentation of the same shipped rules used by the checklist, no client call."""
    try:
        rules = cached_rules()
    except UnknownCountryError:
        return None
    return {"country": {"MX": "Mexico", "US": "United States"}.get(rules.country, rules.country),
            "updated": date_words(rules.effective_date), "title": rules.source_title,
            "url": rules.source_url, "rule_set_id": rules.rule_set_id}


def _document_proposal(db_path, dispute_id: str, documents: list, has_statements: bool) -> dict | None:
    """The shipment details a document states, read by code (COPILOT_FEATURE_SELLER_SHORTCUTS): the carrier and the tracking number
    of the one document that reads both with confidence "read", valid by the same tracking check as typed input, and agreed by every
    other readable document that states a number. Shown as a proposal in the form; the seller checks it against the document and
    presses Save. None when no document states both, a number is not valid, or two documents state different numbers."""
    from copilot.core import carriers
    readings = []
    for doc in extracted_docs_for(db_path, dispute_id):
        if not doc.raw_text_available:
            continue
        tracking = (doc.fields.get("tracking_number") or {})
        carrier = (doc.fields.get("carrier") or {})
        if tracking.get("confidence") == "read" and tracking.get("value"):
            readings.append((str(doc.doc_id), carriers.normalize_tracking(tracking["value"]),
                             carrier.get("value") if carrier.get("confidence") == "read" else None))
    if not readings or len({r[1] for r in readings}) != 1 or carriers.tracking_number_problem(readings[0][1]):
        return None
    chosen = next((r for r in readings if r[2]), None)
    if chosen is None:
        return None
    match = carriers.map_carrier(chosen[2])
    if not match.code:
        return None
    names = {str(d["id"]): d["filename"] for d in documents}
    name = carriers.display_name(match.carrier_name_other if match.code == "OTHER" else match.code)
    statement = (f"I shipped this parcel with {name}; the tracking number is {chosen[1]}."
                 if not has_statements else "")
    return {"tracking": chosen[1], "carrier_code": match.code, "carrier_other": match.carrier_name_other or "",
            "carrier_name": name, "filename": names.get(chosen[0], "the document"), "statement": statement}


def _open_flags(package: dict | None, claims: list, manual_fields: dict) -> list:
    """The 'nothing has been entered yet' notice is written when a manual package is created and nothing ever
    cleared it, so a complete manual package still needed an override text before Approve. It is dropped here (not in
    the database: the stored list is the history) as soon as the package has a statement or typed shipment details.
    No other notice is touched."""
    flags = list((package or {}).get("review_flags") or [])
    if claims or manual_fields:
        flags = [f for f in flags if f != EMPTY_MANUAL_FLAG]
    return flags


def _no_rule_set_line() -> str:
    """The line for an installation whose country setting has no rule set. The country is the one
    cached_rules() tried (checklist_input.RULES_COUNTRY); with none known the parenthesis is dropped."""
    country = (checklist_input.RULES_COUNTRY or "").strip()
    where = f" ({country})" if country else ""
    return (f"Proofie has no Seller Protection rule set for the country in this installation's setting{where}, "
            f"so it cannot check "
            f"these items. Everything you type or upload is still shown, but nothing here is marked as verified "
            f"against a rule.")


def _checks_for(dispute: dict, dispute_id: str, client: SandboxClient, db_path) -> list:
    """Shared by review_package and approve_package so both always see the same checklist for the same
    data - approval must gate on exactly what the vendor was shown (never a re-computed, possibly
    different, set of checks)."""
    # after a send PayPal confirmed, the tracker it registered for OUR number is not a delivery report (core/own_tracker.py)
    context = fetch_context(own_tracker_client(client, db_path, dispute_id), dispute)
    extracted_docs = extracted_docs_for(db_path, dispute_id)
    return evaluate(context, extracted_docs, cached_rules()), context


_CHECK_GROUP_TITLES = (("verified", "Verified"), ("missing", "Missing"), ("conflict", "Does not match"),
                      ("present_unverifiable", "Present, but Proofie cannot verify"),
                      ("not_applicable", "Not applicable"))


# The rule files and checklist (copilot/core, copilot/rules) own these labels; the page may word one differently
# for the seller without touching them.
_SELLER_LABELS = {"Delivered at a branch (ocurre)": "Delivered to a carrier branch for pickup",
                  "Tracking number": "The tracking number is this order's",
                  "Delivery place matches the address on the PayPal transaction": "The delivery address matches the sale in PayPal",
                  "Shipment shows as delivered": "The proof PayPal asks for: delivered",
                  "Delivery date": "The proof PayPal asks for: delivery date",
                  "Signed proof of delivery (total above the limit for the payment currency)":
                      "The proof PayPal asks for: signed receipt (total above the limit for the payment currency)"}


def blocking_conflict_line(checks) -> str | None:
    """The consequence of a conflict that blocks the approval (the two date checks she can acknowledge do not block):
    the receipt sentence only for the address check, the general one otherwise, None when nothing blocks."""
    from copilot.core.checklist import ACKNOWLEDGEABLE_DATE_CHECKS
    blocking = {c.id for c in checks if c.state == "conflict" and c.id not in ACKNOWLEDGEABLE_DATE_CHECKS}
    if "destination" in blocking:
        return "This answer cannot be sent while the receipt does not match PayPal's data."
    return "This answer cannot be sent while a check does not match PayPal's data." if blocking else None


def seller_label(text: str) -> str:
    return _SELLER_LABELS.get(text, text)


_STATE_LABELS = {"verified": "Verified", "missing": "Missing",
                 "present_unverifiable": "Present, but Proofie cannot verify",
                 "conflict": "Does not match", "not_applicable": "Not applicable"}
_BASIS_LABELS = {"case_request": "PayPal's case asks for this",
                 "published_policy": "PayPal Seller Protection policy",
                 "app_quality_rule": "Proofie's own quality rule"}
CASE_REQUEST_WHY = "Sending is the step that answers this request."


# A claim PayPal is reviewing (stage CHARGEBACK, status UNDER_REVIEW, outcome feature on) takes nothing more from the seller:
# the checks that would invite adding or typing something say the shared phrase instead, and offer no add or type link.
REVIEWING_NOTHING_TO_ADD = REVIEWING_NEUTRAL_TEXT


def _check_rows(checks: list, dispute_id: str, package_id: int, documents_by_id: dict, reviewing: bool = False) -> list:
    """One row per check for the review table: a readable state, the basis, and a REAL action for it - an
    anchor, a link or a form, never the plain enum token the checklist uses internally. A
    conflict also carries one-click removal of the conflicting document (the same exclude route the
    approval page offers); everything stays the seller's decision."""
    documents_href = f"/disputes/{dispute_id}/workspace/prepare#documents"
    rows = []
    for c in checks:
        actions: list = []
        if c.action == "add_document":
            actions += [{"kind": "link", "label": "Add a document", "href": documents_href},
                        {"kind": "link", "label": "Type the details", "href": "#shipment-details"}]
        elif c.action == "fix_value":
            actions.append({"kind": "link", "label": "Fix the value", "href": "#shipment-details"})
        elif c.action == "review_difference":
            actions += [{"kind": "link", "label": "Type the correct details", "href": "#shipment-details"},
                        {"kind": "link", "label": "Check your documents", "href": documents_href}]
            for src in c.sources:
                key = src.split(":", 1)[1] if src.startswith("document:") else None
                if key and key != "manual" and key in documents_by_id:
                    actions.append({"kind": "exclude",
                                    "label": f'Leave {documents_by_id[key]["filename"]} out of the checks',
                                    "href": f"/disputes/{dispute_id}/documents/{key}/exclude",
                                    "return_to": f"package:{package_id}"})
            actions.append({"kind": "link", "label": "Or decide at approval", "href": "#approval"})
        elif c.action == "check_status":
            actions.append({"kind": "link", "label": "Refresh from PayPal",
                            "href": f"/disputes/{dispute_id}/packages/{package_id}"})
        elif c.state in ("missing", "conflict", "present_unverifiable"):
            actions.append({"kind": "link", "label": "Type the details", "href": "#shipment-details"})
        if reviewing and c.action != "none":
            actions = []                      # nothing can be added or typed while PayPal reviews the claim
        seen, unique = set(), []
        for a in actions:
            if (a["kind"], a["href"]) not in seen:
                seen.add((a["kind"], a["href"]))
                unique.append(a)
        rows.append({"check": c, "label": seller_label(c.label_for_seller),
                     "state_label": _STATE_LABELS.get(c.state, c.state),
                     "tag_state": ("todo" if c.state == "missing" and c.id in {"receiver_name", "capture_date"}
                                   else {"verified":"ok","missing":"bad","conflict":"bad",
                                         "present_unverifiable":"warn","not_applicable":"na"}.get(c.state,"todo")),
                     "basis_label": _BASIS_LABELS.get(c.basis, c.basis), "actions": unique,
                     "why": REVIEWING_NOTHING_TO_ADD if reviewing and c.action != "none" else None})
    return rows


_CAPTURE_ID = re.compile(r"[A-Za-z0-9]{17}")


def _tracker_on_file(db_path, dispute: dict) -> dict | None:
    """the tracking number and carrier the seller already registered for the sale
    of this dispute on the tracking form (feature fulfilment), so they are offered here instead of typed twice. Local and
    read only: the latest saved file of the sale is read on a read-only connection (no table is created by a page view,
    no PayPal call); offered only when its status is registered or registered_unconfirmed. Display only: the seller
    confirms by pressing Save shipment details."""
    if not config.feature_enabled("fulfilment"):
        return None
    capture = next((c for c in (dispute.get("seller_transaction_ids") or []) if isinstance(c, str)
                    and _CAPTURE_ID.fullmatch(c)), None)
    if capture is None:
        return None
    conn = None
    try:
        conn = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fulfilment_files'").fetchone():
            return None
        row = conn.execute("SELECT request_json, status FROM fulfilment_files WHERE capture_id = ? "
                           "ORDER BY id DESC LIMIT 1", (capture,)).fetchone()
        if row is None or row["status"] not in ("registered", "registered_unconfirmed"):
            return None
        tracker = json.loads(row["request_json"])["trackers"][0]
    except (sqlite3.Error, ValueError, KeyError, IndexError, TypeError):
        return None
    finally:
        if conn is not None:
            conn.close()
    from copilot.core import carriers
    number = carriers.normalize_tracking(tracker.get("tracking_number"))
    if not number:
        return None
    code = tracker.get("carrier") if isinstance(tracker.get("carrier"), str) else ""
    other = tracker.get("carrier_name_other") if isinstance(tracker.get("carrier_name_other"), str) else ""
    return {"capture": capture, "number": number, "carrier_code": code, "carrier_other": other,
            "carrier": carriers.display_name(other or code)}


def _checks_count(checks: list) -> dict | None:
    """the completeness count printed under the Checks heading, the same numbers and sentence as
    the dispute page's package table (routes_disputes: verified, cannot be checked, total without the not-applicable
    ones), plus the first row of each state so the counts can link to it. A count of what is on the page, never a
    prediction of how PayPal will decide. None when there are no checks to count."""
    if not checks:
        return None
    verified = [c for c in checks if c.state == "verified"]
    cannot = [c for c in checks if c.state == "present_unverifiable"]
    total = sum(1 for c in checks if c.state != "not_applicable")
    if total == 0:
        return None                      # nothing to count: no sentence rather than "0 of 0 checks"
    noun = "check" if total == 1 else "checks"
    return {"verified": len(verified), "total": total, "cannot": len(cannot), "noun": noun,
            "first_verified": verified[0].id if verified else None, "first_cannot": cannot[0].id if cannot else None,
            "line": presentation.verdict_line(checks)}


def _settle_case_request_rows(rows: list, approved_and_sendable: bool) -> None:
    """When the only red rows are the ones PayPal's own case request asks for and the package is approved
    and sendable, those rows read 'not started': sending is the step that answers them. Presentation only."""
    bad = [r for r in rows if r["tag_state"] == "bad"]
    if approved_and_sendable and bad and all(r["check"].basis == "case_request" for r in bad):
        for r in bad:
            r["tag_state"] = "todo"
            r["why"] = CASE_REQUEST_WHY


_CLAIM_BASIS_LABELS = {"verified": "Backed by a source", "seller_statement": "Your own statement",
                       "buyer_statement": "The buyer's statement",
                       "model_interpretation": "Suggested — check against the source"}


def _claim_sources(claim: dict, doc_names: dict) -> list:
    """Where a statement comes from, in words (the claim card): the document by file name, the seller's own
    typing, PayPal's record - never the internal source id."""
    labels = []
    for sid in claim.get("source_ids") or []:
        if sid == "paypal_dispute":
            labels.append("PayPal's dispute record")
        elif sid == "doc_manual":
            labels.append("what you typed")
        elif sid.startswith("doc_"):
            labels.append(f"document {doc_names.get(sid.split('_', 1)[1], '#' + sid.split('_', 1)[1])}")
        else:
            labels.append(sid)
    if not labels:
        labels.append("you" if claim.get("basis") == "seller_statement" else "no source cited")
    return labels


# ------------------------------------------------------------------------------- the send state of the package page
# The data-state of the send banner comes from the stored send record (copilot/web/status.py), never from the words of
# a banner: the words are shown, the record decides. Kind is a banner kind (info/warn/error/ok).
_RECORD_BANNER = {
    "confirmed": ("ok", "confirmed", None),
    "reserved": ("warn", "result-uncertain", "check"),
    "attempted": ("warn", "result-uncertain", "check"),
    "partial": ("warn", "partial", "check"),
    "refused": ("error", "rejected", "fix"),
    "blocked": ("error", "blocked", "fix"),
    "abandoned": ("error", "abandoned", None),
}


def send_state_for_record(record, approval_valid: bool) -> dict:
    """The box the review page draws for one package's send record: kind, a stable data-state for the DOM, and the one
    action that fits. Result uncertain is amber and offers Check again (GET only); it is neither an error nor a
    success. A confirmed send whose attached files no longer match what PayPal received is not
    shown as received."""
    state = getattr(record, "state", "none") if record is not None else "none"
    if state == "none":
        return {"kind": "info", "state": "approved" if approval_valid else "prepared", "action": None}
    if state == "confirmed" and not getattr(record, "attachments_match", True):
        return {"kind": "warn", "state": "later-file", "action": None}
    kind, name, action = _RECORD_BANNER.get(state, ("info", "prepared", None))
    return {"kind": kind, "state": name, "action": action}


def _attachment_shas(db_path, package_id: int) -> list:
    """The attachment hashes of a package, built the way the SEND path builds them (repo.list_attachments, which
    joins `documents`), never from the raw attachment rows. Approval, the review page and the send must all use
    this one list, or the page could offer Send for something the server then refuses (a package_
    attachments row whose document row is gone)."""
    return [a["sha256"] for a in repo.list_attachments(db_path, package_id)]


def _earlier_partial(attempts: list, own_sha) -> bool:
    """True when every send that is still waiting and is not this package's own (the row of the same statements under
    another package included) reached PayPal without every file: the read-back listed its statements, not every file, and
    the attempt's reason starts with missing_attachment:. One ordinary unconfirmed send among them keeps the ordinary
    words."""
    waiting = [a for a in attempts if a["state"] in ("pending", "sent_unconfirmed")
               and a["package_sha256"] != own_sha]
    return bool(waiting) and all(a["state"] == "sent_unconfirmed"
                                 and str(a.get("reason") or "").startswith(status.MISSING_FILE_PREFIX)
                                 for a in waiting)


def _send_summary(db_path, dispute_id: str, package: dict, claims: list, attachment_docs: list,
                  approval: dict | None, record=None) -> dict:
    """What would be sent (statements, attachments, tracking) and what earlier sends PayPal confirmed.
    An earlier confirmed attempt for DIFFERENT content stays identifiable as the earlier one and is
    never shown as covering the current content. Nor is a confirmed attempt shown as covering the
    package when a file was attached after it was sent (status.send_record compares the attempt's files with the
    package's files). `attachment_docs` is any list of dicts with a 'filename' (repo.list_attachments rows)."""
    tracking_number, carrier = pick_tracking(extracted_docs_for(db_path, dispute_id))
    attachment_shas = _attachment_shas(db_path, package["id"])
    notes = None
    try:
        req_input = build_request_input(claims, tracking_number, carrier)
        notes = req_input["evidences"][0]["notes"]          # the text the approval page shows under "Exact text PayPal receives"
        current_sha = sha256_json(req_input)
        fingerprint_now = approval_fingerprint_with_attachments(dispute_id, "provide_evidence", req_input,
                                                                attachment_shas)
    except ValueError:
        current_sha = None   # no tracking number yet: nothing sendable, same ValueError the send path uses
        fingerprint_now = f"not-sendable-yet-{package['id']}"   # approve_package's own placeholder shape
    store = SQLiteAttemptStore(db_path)
    try:
        attempts = store.list_by_dispute(dispute_id)
    finally:
        store.close()
    if record is None:
        record = status.send_record(db_path, dispute_id, package)
    own_sha = (record.attempt or {}).get("package_sha256") if record.state == "confirmed" else None
    own_sha_of_attempt = (record.attempt or {}).get("package_sha256")      # whatever this package's own attempt is
    earlier = [a for a in attempts if a["state"] == "confirmed" and a["package_sha256"] not in (current_sha, own_sha)]
    # An approval row can still be "valid" in the table while no longer matching what would be sent now
    # (e.g. the tracking number changed through the shipment-details form, which does not touch approvals);
    # the send route would refuse it as out of date, so the page must say the same instead of offering Send.
    approval_stale = approval is not None and approval["approval_sha256"] != fingerprint_now
    approval_cleared = approval_stale
    if approval is None:
        approval_cleared = any(e["action"] == "approve_package" and e["detail"].get("package_id") == package["id"]
                               for e in repo.list_audit(db_path, dispute_id))
    return {"tracking_number": tracking_number, "carrier": carrier, "sendable": current_sha is not None,
            "notes": notes, "fingerprint": approval["approval_sha256"] if approval else None,
            "claims_count": len(claims), "attachments": [d["filename"] for d in attachment_docs],
            "earlier_confirmed": [{"when": timefmt.fmt_iso(a["ts"]), "ts": a["ts"]} for a in earlier],
            "approval_cleared": approval_cleared, "approval_stale": approval_stale,
            "has_unconfirmed": any(a["state"] in ("pending","sent_unconfirmed") for a in attempts),
            "earlier_partial": _earlier_partial(attempts, own_sha_of_attempt),
            "current_confirmation": (timefmt.fmt_iso(record.confirmed_at)
                                     if record.state == "confirmed" and record.content_matches
                                     and record.attachments_match else None)}


# ------------------------------------------------------------------------------------ redirects, ownership, refusals
def _package_url(dispute_id: str, package_id) -> str:
    return f"/disputes/{dispute_id}/packages/{package_id}"


def _done_redirect(dispute_id: str, package_id, key: str, fragment: str, *, cleared: bool = False, **extra):
    """303 to the section the seller was working in, carrying the done key. The banner it asks for is only
    shown if the stored state agrees (render_package)."""
    query = {"done": key, **{name: value for name, value in extra.items()}}
    if cleared:
        query["cleared"] = "1"
    return RedirectResponse(f"{_package_url(dispute_id, package_id)}?{urlencode(query)}#{fragment}", status_code=303)


def _own_package(request: Request, dispute_id: str, package_id):
    """(package, None) for a package of THIS dispute, else (None, the 404 page). A package of another dispute is treated
    exactly like a missing one."""
    package = repo.get_package(config.db_path(), package_id) if str(package_id).isdigit() else None
    if package is None or package["dispute_id"] != dispute_id:
        return None, refusal_page(request, 404, "Package not found",
                                  "That package could not be found for this dispute.",
                                  back_url=f"/disputes/{dispute_id}", back_label="Go back to the dispute")
    return package, None


def _own_document(request: Request, dispute_id: str, document_id: int):
    doc = repo.get_document(config.db_path(), document_id)
    if doc is None or doc["dispute_id"] != dispute_id:
        return None, refusal_page(request, 404, "Document not found", "That document could not be found.",
                                  back_url=f"/disputes/{dispute_id}/workspace/prepare#documents", back_label="Go back to the dispute")
    return doc, None


def _is_sent_version(db_path, dispute_id: str, package: dict) -> bool:
    """PayPal's own record (read back) holds this exact content with every file. Such a version never changes."""
    record = status.send_record(db_path, dispute_id, package)
    # a version PayPal holds statements of (confirmed, with a file added afterwards, or a true partial)
    # is just as final: editing it in place would hide the statements PayPal has. One rule for every page and route
    # (status.is_sent_record).
    return status.is_sent_record(record)


def _sent_version_refusal(request: Request, dispute_id: str, package_id):
    return refusal_page(request, 409, "This version was sent", SENT_VERSION_REFUSAL,
                        back_url=_package_url(dispute_id, package_id), back_label="Go back to this package")


def _clear_approval(db_path, dispute_id: str, package_id, *, log: bool = False) -> bool:
    """Any change to what would be sent clears a valid approval (the fingerprint was computed over the old content).
    True when a valid approval existed and was cleared."""
    prior = repo.get_latest_valid_approval(db_path, package_id)
    if not prior:
        return False
    repo.invalidate_approval(db_path, prior["id"])
    if log:
        repo.log_action(db_path, "system", "invalidate_approval_after_edit", dispute_id,
                        {"package_id": package_id, "approval_id": prior["id"]})
    return True


def _normalise_statement(text: str) -> str:
    """The statement as typed, with the browser's line endings made one (CRLF or CR -> LF) so the same words never hash
    two ways. build_request_input strips each statement before it joins them; nothing else is changed."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


@router.post("/disputes/{dispute_id}/packages", response_class=HTMLResponse)
def create_package(request: Request, dispute_id: str, csrf_token: str = Form(...), manual: int = 0,
                   client: SandboxClient = Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    # Validate dispute_id (untrusted path segment) before any PayPal call, redirect, or HTML.
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()

    if manual:
        package_id = repo.insert_package(db_path, dispute_id, source="human_edit", validation_ok=False,
                                         review_flags=[EMPTY_MANUAL_FLAG])
        repo.log_action(db_path, "vendor", "start_manual_package", dispute_id, {"package_id": package_id})
        return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}", status_code=303)

    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    dispute = normalize(raw, datetime.now(timezone.utc))
    docs, names = docs_for_dispute(db_path, dispute_id)
    if not docs:
        # AI generation needs at least one readable document; otherwise go straight to manual mode
        # rather than asking the model to invent evidence from nothing. An image
        # document (never readable as text) can still get a vision pass here - purely
        # additive suggestions, never a reason to skip the manual-mode shell itself.
        package_id = repo.insert_package(db_path, dispute_id, source="human_edit", validation_ok=False,
                                         review_flags=["No readable documents yet - add one, or continue "
                                                      "manually."])
        repo.log_action(db_path, "vendor", "start_manual_package_no_documents", dispute_id,
                        {"package_id": package_id})
        vision_claims = claims_for_dispute_images(db_path, dispute_id)
        if vision_claims:
            repo.insert_claims(db_path, package_id, vision_claims)
            repo.log_action(db_path, "system", "vision_claims_suggested", dispute_id,
                            {"package_id": package_id, "count": len(vision_claims)})
        return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}", status_code=303)

    try:
        rec = propose_package(dispute, docs, names, label=f"copilot:{dispute_id}")
    except Exception as exc:   # noqa: BLE001 - the model/GPU being unavailable (pilot's preflight()
                               # raises RuntimeError when busy, or httpx raises directly when Ollama is
                               # simply not reachable - this route once failed to catch it, unlike
                               # auto_prep.py's background path) is never a
                               # reason to show a raw error; fall back to manual mode like auto_prep.py.
        package_id = repo.insert_package(
            db_path, dispute_id, source="human_edit", validation_ok=False,
            review_flags=[ai_unavailable_flag()])
        repo.log_action(db_path, "vendor", "start_manual_package_ai_unavailable", dispute_id,
                        {"package_id": package_id, "error": str(exc)})
        return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}", status_code=303)
    out = rec.get("_output")
    claims = claims_from_output(out)
    next_evidence_index = len(out.get("proposed_evidence", [])) if out else 0
    vision_claims = claims_for_dispute_images(db_path, dispute_id, evidence_index=next_evidence_index)
    claims = claims + vision_claims
    # The same notice copilot/core/auto_prep.py already puts on an automatically
    # prepared draft - a delivery/signature/receipt assertion with no source behind it (or citing a document
    # PayPal's own data conflicts with) - must also appear on a draft the seller generates by hand. Without it
    # the seller only learns why approval is blocked AFTER pressing Approve. The block itself (approve_package)
    # is unchanged and still not overridable.
    docs_sources = {"paypal_dispute": "{}", **docs}
    extracted_docs = extracted_docs_for(db_path, dispute_id)
    try:
        checks, _context = _checks_for(dispute, dispute_id, client, db_path)
    except UnknownCountryError:
        checks = []   # no rule set for this country: the checklist-based halves cannot be computed (approval will
                      # say so); what the extracted documents alone show (an illegible one) is still applied
    # a document the checks do not trust (injection, illegible) can never source a delivery assertion
    untrusted = untrusted_document_ids(checks, extracted_docs)
    unsupported = (find_unsupported_delivery_assertions(out or {"proposed_evidence": []}, docs_sources, untrusted) +
                   find_delivery_claims_citing_conflicting_sources(claims, checks) +
                   find_delivery_claims_citing_untrusted_documents(claims, checks, extracted_docs))
    review_flags = list(rec.get("review_flags", []))
    review_flags += list(dict.fromkeys(describe_delivery_problem(u) for u in unsupported))
    package_id = repo.insert_package(
        db_path, dispute_id, source="ai", validation_ok=rec["validation_ok"],
        review_flags=review_flags, model=rec.get("model"),
        prompt_sha256=rec.get("prompt_sha256"), raw_output=rec.get("raw_output"),
        effective_next_step=rec.get("effective_next_step"))
    if claims:
        repo.insert_claims(db_path, package_id, claims)
    repo.log_action(db_path, "system", "generate_package", dispute_id,
                    {"package_id": package_id, "validation_ok": rec["validation_ok"],
                     "unsupported_delivery_assertions": len(unsupported),
                     "vision_claims": len(vision_claims)})
    return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}", status_code=303)


@router.post("/disputes/{dispute_id}/manual-fields", response_class=HTMLResponse)
async def set_manual_fields_route(request: Request, dispute_id: str, client=Depends(get_client)):
    """Vendor-typed shipment facts (manual capture), for the checklist fields no document covers yet.
    One evolving set per dispute, replaced wholesale on each submit - same field vocabulary as automatic
    extraction (copilot/core/field_extraction.py), so the checklist cannot tell which path a value took."""
    form = await request.form()
    if not verify_csrf(request, form.get("csrf_token", "")):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    package_id = form.get("package_id", "")
    package = None
    if str(package_id).isdigit():
        package = repo.get_package(db_path, int(package_id))
        if package is not None and package["dispute_id"] != dispute_id:
            package = None
    if package is not None and _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package["id"])
    fields, errors, values = shipment_fields(form)
    payment_date = None
    if fields.get("delivery_date") and not errors:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
        norm = normalize(raw, datetime.now(timezone.utc))
        context = fetch_context(client, norm)
        payment_date = context.get("payment_date")
        if payment_date and fields["delivery_date"]["value"] < str(payment_date)[:10]:
            errors["delivery_date"] = errors["payment_date"] = "Delivery date must not be before payment date."
    if errors:
        if package is not None:
            return render_package(request, dispute_id, package["id"], client,
                                  form_values=values, form_errors=errors, payment_date=payment_date)
        return refusal_page(request, 400, "Error: Shipment details", "There is a problem with the shipment details.",
                            items=list(errors.values()), back_url=f"/disputes/{dispute_id}",
                            back_label="Go back to the dispute")
    changed = repo.get_manual_fields(db_path, dispute_id) != fields
    repo.set_manual_fields(db_path, dispute_id, fields)
    cleared = False
    if changed:
        for other in repo.list_packages(db_path, dispute_id):
            approval = repo.get_latest_valid_approval(db_path, other["id"])
            if approval:
                repo.invalidate_approval(db_path, approval["id"])
                cleared = cleared or (package is not None and other["id"] == package["id"])
    repo.log_action(db_path, "vendor", "set_manual_fields", dispute_id, {"fields": sorted(fields)})
    if package is None:
        return RedirectResponse(f"/disputes/{dispute_id}", status_code=303)
    return _done_redirect(dispute_id, package["id"], "shipment_saved", "shipment-details", cleared=cleared)


@router.post("/disputes/{dispute_id}/documents/{document_id}/confirm-reading", response_class=HTMLResponse)
def confirm_document_reading(request: Request, dispute_id: str, document_id: int,
                             csrf_token: str = Form(...), field_name: str = Form(...),
                             value: str = Form(...), package_id: str = Form(default="")):
    """The vendor explicitly confirming one of
    vision_model.py's suggested readings as accurate - "seller-confirmed reading of <file>: <value>",
    always actor='vendor', never the model (copilot/db/repo.py's own CHECK constraint enforces this too).
    Once confirmed, checklist_input.py feeds that field to the checklist as confidence="read" and makes
    it citable text for delivery-assertion claims (copilot/core/evidence_model.find_unsupported_
    delivery_assertions) - the same as any other legible document, never a shortcut around the delivery-assertion gating."""
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    doc, refusal = _own_document(request, dispute_id, document_id)
    if refusal is not None:
        return refusal
    package = None
    if str(package_id).isdigit():
        package = repo.get_package(db_path, int(package_id))
        if package is not None and package["dispute_id"] != dispute_id:
            package = None
    if package is not None and _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package["id"])
    if field_name not in MANUAL_FIELD_NAMES:   # defense in depth: never trust an arbitrary field name,
                                               # even though the form only ever offers a recognised one
        return refusal_page(request,400,"Check the document reading","Unrecognized field.",
                            back_url=f"/disputes/{dispute_id}",back_label="Go back to the dispute")
    value = value.strip()
    if not value:
        return refusal_page(request,400,"Check the document reading","Cannot confirm an empty value.",
                            back_url=f"/disputes/{dispute_id}",back_label="Go back to the dispute")
    repo.confirm_document_reading(db_path, document_id, field_name, value)
    repo.log_action(db_path, "vendor", "confirm_vision_reading", dispute_id,
                    {"document_id": document_id, "filename": doc["filename"], "field_name": field_name,
                     "value": value})
    # This changes what every package in the dispute would build/check from (checklist_input.py) - any
    # already-valid approval no longer reflects what was actually reviewed, same reasoning as
    # attach_document/edit_claim invalidating approval, just dispute-wide since this route is not
    # itself scoped to one package.
    cleared = False
    for pkg in repo.list_packages(db_path, dispute_id):
        prior = repo.get_latest_valid_approval(db_path, pkg["id"])
        if prior:
            repo.invalidate_approval(db_path, prior["id"])
            cleared = cleared or (package is not None and pkg["id"] == package["id"])
    if package is None:
        return RedirectResponse(f"/disputes/{dispute_id}", status_code=303)
    claim = next((c for c in repo.list_claims(db_path, package["id"])
                  if c.get("suggested_field") == field_name and f"doc_{document_id}" in (c.get("source_ids") or [])),
                 None)
    if claim is None:
        return RedirectResponse(_package_url(dispute_id, package["id"]), status_code=303)
    return _done_redirect(dispute_id, package["id"], "reading_confirmed", f"claim-{claim['id']}",
                          cleared=cleared, claim=claim["id"])


@router.get("/disputes/{dispute_id}/packages/{package_id}", response_class=HTMLResponse)
def review_package(request: Request, dispute_id: str, package_id: int,
                   client: SandboxClient = Depends(get_client)):
    return render_package(request, dispute_id, package_id, client)


# What the seller types in the shipment form: (name, label, hint). Hints that contradict a check in
# copilot/core/checklist.py are not shipped (empty). The tracking number and the carrier are drawn by the shell's own
# fields (tracking_field, carrier_field); their rows here only feed the groups of "Where this package stands".
SHIPMENT_FIELDS = (
    ("tracking_number", "Tracking number", ""),
    ("carrier", "Carrier", ""),
    ("delivery_status", "Delivery status", "For example \"delivered\". \"In transit\" is shown as in transit, is not shown as delivered."),
    ("delivery_date", "Delivery date", "The delivery date shown on the carrier's proof."),
    ("ship_date", "Ship date", "The date the carrier picked up the parcel."),
    ("postal_code", "Postal code", "The postal code of the delivery address."),
    ("city", "City", "The city of the delivery address."),
    ("state", "State", "The state of the delivery address."),
    ("street", "Street", "Only needed if your document shows a street address."),
    ("receiver_name", "Receiver name", "Who received the package. For information only."),
    ("signature", "Signature", "Can the signature be viewed online on the carrier's site? Some countries' rules ask for it above a limit; check the signature row below."),
    ("delivery_type", "Delivery type", "Choose branch pickup if the parcel was collected at a carrier branch. This is Proofie's own risk flag, not a PayPal rule."),
    ("capture_date", "Date the proof was captured", "The day you saved or photographed the proof, not the delivery date."),
)
# Select fields: the stored value is exactly what checklist.py compares (_signature via _SIG, _branch). First option is
# empty and stores nothing.
SHIPMENT_SELECTS = {
    "signature": (("online", "Viewable online"), ("paper_only", "Paper only"),
                  ("aggregator_portal", "Tracking portal only"), ("none", "Not viewable online")),
    "delivery_type": (("home", "Home delivery"), ("branch_pickup", "Branch pickup")),
}
SHIPMENT_DATES = ("delivery_date", "ship_date", "capture_date")
_FIELD_LABELS = {name: label for name, label, _ in SHIPMENT_FIELDS}


def _present_fields(db_path, dispute_id: str) -> set:
    """Names of the shipment fields that have a usable value from a document or from the seller (display only: the
    checks, not this set, decide what is verified)."""
    present = set()
    for doc in extracted_docs_for(db_path, dispute_id):
        for name, field in (doc.fields or {}).items():
            if isinstance(field, dict) and field.get("value") not in (None, "") and field.get("confidence") != "illegible":
                present.add(name)
    return present


def _shipment_groups(*, needed: list, carrier_required: bool, present: set) -> dict:
    """'Required to send', 'Needed for the checks' and 'Optional', from the real send gate (tracking number, and the
    carrier when carrier_required) and from the fields the form marks Needed. Each item: (label, present)."""
    required = ["tracking_number"] + (["carrier"] if carrier_required else [])
    needed_names = ([] if carrier_required else ["carrier"]) + [n for n in needed if n not in required]
    optional = [n for n, _, _ in SHIPMENT_FIELDS if n not in required and n not in needed_names]

    def items(names):
        return [(_FIELD_LABELS[n], n in present) for n in names]
    return {"Required to send": items(required), "Needed for the checks": items(needed_names),
            "Optional": items(optional)}


def _reading_value(field: str, value) -> str:
    """The value of an image reading as the seller reads it: dates in words, a carrier code by its name."""
    if field in SHIPMENT_DATES:
        words = date_words(str(value)[:10])
        return words if words and words != "Not available" else str(value)
    if field == "carrier":
        from copilot.core import carriers
        return carriers.display_name(value) or str(value)
    return str(value)


def _done_info(params, *, db_path, dispute_id: str, package: dict, claims: list, attachments: list, documents: list,
               record, approval, manual_fields: dict) -> dict | None:
    """The one confirmation banner for the done key in the URL, or None. The text is presentation.done_message (facts
    read from stored rows, never request text) and the banner appears only when the stored state really matches the
    key: a forged or stale URL renders nothing."""
    key = (params.get("done") or "").strip()
    if not key:
        return None

    def number(name):
        raw = params.get(name) or ""
        return int(raw) if raw.isdigit() and len(raw) <= 9 else None

    attached_ids = {a["document_id"] for a in attachments}
    docs = {d["id"]: d for d in documents}
    doc = docs.get(number("doc"))
    claim_id = number("claim")
    claim_pos = next((i + 1 for i, c in enumerate(claims) if c["id"] == claim_id), None) if claim_id else None
    facts = None
    undo = None                         # only 'detached' and 'document_excluded', and never on a sent version
    if key == "shipment_saved":
        facts = {"ok": True} if manual_fields else None
    elif key == "statement_added":
        n = number("n")
        facts = {"n": n} if n and 1 <= n <= len(claims) else None
    elif key == "statement_saved":
        claim = next((c for c in claims if c["id"] == claim_id), None)
        facts = {"n": claim_pos} if claim and claim.get("edited_by_human") else None
    elif key == "statement_removed":
        n = number("n")
        removed = any(e["action"] == "remove_claim" and e["detail"].get("package_id") == package["id"]
                      for e in repo.list_audit(db_path, dispute_id))
        facts = {"n": n} if n and removed and 1 <= n <= len(claims) + 1 else None
    elif key == "reading_confirmed":
        claim = next((c for c in claims if c["id"] == claim_id), None)
        if claim and claim.get("confirmed_value") and claim.get("suggested_field"):
            facts = {"field": _FIELD_LABELS.get(claim["suggested_field"]) or field_words.field_word(claim["suggested_field"]),
                     "value": _reading_value(claim["suggested_field"], claim["confirmed_value"])}
    elif key == "document_added_package":
        facts = {"filename": doc["filename"]} if doc else None
    elif key == "attached":
        facts = {"filename": doc["filename"]} if doc and doc["id"] in attached_ids else None
    elif key == "detached":
        detached = doc is not None and any(e["action"] == "detach_document" and e["detail"].get("package_id") == package["id"]
                                           and e["detail"].get("document_id") == doc["id"]
                                           for e in repo.list_audit(db_path, dispute_id))
        facts = {"filename": doc["filename"]} if doc and doc["id"] not in attached_ids and detached else None
        if facts:
            undo = presentation.done_undo(key, f"{_package_url(dispute_id, package['id'])}/attach/{doc['id']}",
                                          doc["filename"])
    elif key == "document_excluded":
        facts = {"filename": doc["filename"]} if doc and doc["excluded"] else None
        if facts:
            undo = presentation.done_undo(key, f"/disputes/{dispute_id}/documents/{doc['id']}/include", doc["filename"],
                                          fields=(("return_to", f"package:{package['id']}"),))
    elif key == "document_included":
        facts = {"filename": doc["filename"]} if doc and not doc["excluded"] else None
    elif key == "approved":
        if approval and record.state == "none":
            facts = {"when": timefmt.display(approval["approved_at"])}
    if facts is None:
        return None
    text = presentation.done_message(key, **{k: v for k, v in facts.items() if k != "ok"})
    if not text:
        return None
    if params.get("cleared") == "1" and approval is None and repo.has_invalidated_approval(db_path, package["id"]):
        text = f"{text} {presentation.done_message('approval_cleared')}"
    if status.is_sent_record(record):
        undo = None
    return {"key": key, "text": text, "undo": undo}


def _next_claim_index(claims: list) -> int:
    """The claim_index for a statement added now: one past the highest in use, never reused after removals.
    Shared by the add-statement route and the approve-despite-a-conflict note."""
    return max((c["claim_index"] for c in claims), default=-1) + 1


def _sent_view(db_path, package: dict, record) -> dict | None:
    """'What was sent' of a version PayPal holds statements of: read from the attempt's stored request and file names, never
    from what the page would send now. Shown for a confirmed send (also when a file was added afterwards) and for a true
    partial send; None otherwise."""
    if not status.is_sent_record(record):
        return None
    attempt = record.attempt or {}
    request_input = attempt.get("request_input") or {}
    tracking = carrier = notes = None
    try:
        evidence = request_input["evidences"][0]
        info = evidence["evidence_info"]["tracking_info"][0]
        tracking = info.get("tracking_number")
        from copilot.core import carriers
        carrier = carriers.display_name(info.get("carrier_name_other") or info.get("carrier_name"))
        notes = evidence.get("notes")
    except (KeyError, IndexError, TypeError):
        pass
    used = None
    approvals = repo.list_approvals(db_path, package["id"])
    row = next((a for a in approvals if record.approval_sha256 and a["approval_sha256"] == record.approval_sha256), None)
    if row is not None:
        invalidated_later = bool(row.get("invalidated_at") and record.confirmed_at
                                 and row["invalidated_at"] >= record.confirmed_at)
        # One time for "used for the send": the audit send time. A change DURING the send clears the approval
        # before the read-back is stamped, so it is read from the audit flag as well as from the later invalidation.
        used = {"at": record.sent_at or row["approved_at"],
                "changed_after": bool(record.changed_after_sending or (
                    row.get("invalidated_at") and record.sent_at and row["invalidated_at"] >= record.sent_at))}
    kind = "partial" if record.state == "partial" else "later_file" if not record.attachments_match else "confirmed"
    return {"when": record.sent_at if kind == "partial" else record.confirmed_at, "tracking": tracking,
            "carrier": carrier, "notes": notes, "files": list(record.attachment_filenames), "approval": used,
            "recorded": bool(request_input), "kind": kind}


def render_package(request, dispute_id, package_id, client, *, form_values=None, form_errors=None, payment_date=None):
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    package = repo.get_package(db_path, package_id)
    # package_id is a path segment independent of dispute_id. A package that belongs to
    # another dispute is treated exactly like a missing one: it is never read or shown here. Both get the template's
    # own "package-not-found" page (which has the way back and the footer), now with status 404 instead of 200.
    if package is not None and package["dispute_id"] != dispute_id:
        package = None
    claims = repo.list_claims(db_path, package_id) if package else []
    _confirmed_cache = {}
    for c in claims:
        c["confirmed_value"] = None
        if not c.get("suggested_field"):
            continue
        src_doc_id = next((s.split("_", 1)[1] for s in c["source_ids"]
                          if s.startswith("doc_") and s != "doc_manual"), None)
        if src_doc_id is None:
            continue
        if src_doc_id not in _confirmed_cache:
            _confirmed_cache[src_doc_id] = repo.get_confirmed_readings(db_path, int(src_doc_id))
        confirmed = _confirmed_cache[src_doc_id].get(c["suggested_field"])
        if confirmed:
            c["confirmed_value"] = confirmed["value"]
        c["document_id"] = src_doc_id   # for the "Yes, the image shows this" form's action
    docs, names = docs_for_dispute(db_path, dispute_id)
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    dispute = normalize(raw, datetime.now(timezone.utc))
    checked_at = mark_paypal_read(dispute_id)

    no_rule_set_line = None
    try:
        checks, context = _checks_for(dispute, dispute_id, client, db_path) if package else ([], None)
    except UnknownCountryError:
        checks, context = [], None          # the page stays usable: no check is shown as verified, and it says why
        no_rule_set_line = _no_rule_set_line()

    claim_verdicts = []
    if package and config.feature_enabled("claims") and context is not None:
        from copilot.features.claims.service import compose
        try:
            claim_verdicts = list(dict.fromkeys(c.verdict for c in compose(db_path, dispute_id, package_id, checks, dispute)))
        except ValueError:
            claim_verdicts = []

    adversarial_review = None
    adversarial_review_error = None
    if package and config.feature_enabled("claims"):
        if context is None:
            adversarial_review_error = "Source comparison is unavailable; review the case sources."
        else:
            try:
                from copilot.features.claims.adversarial import adversarial_review as build_adversarial_review
                adversarial_review = build_adversarial_review(db_path, dispute_id, package_id, checks, dispute)
            except (OSError, TypeError, ValueError, sqlite3.Error):
                adversarial_review_error = "Source comparison is unavailable; review the case sources."

    manual_fields = repo.get_manual_fields(db_path, dispute_id)
    if package:
        package = {**package, "review_flags": _open_flags(package, claims, manual_fields)}
    approval = repo.get_latest_valid_approval(db_path, package_id) if package else None
    all_documents = repo.list_documents(db_path, dispute_id)
    documents = [d for d in all_documents if not d["excluded"]]
    attachments = repo.list_attachments(db_path, package_id) if package else []
    attached_ids = {a["document_id"] for a in attachments}
    record = status.send_record(db_path, dispute_id, package) if package else status.NO_RECORD
    send_summary = (_send_summary(db_path, dispute_id, package, claims, attachments, approval, record)
                    if package else None)
    approval_stale = bool(send_summary and send_summary["approval_stale"])
    if approval_stale:
        approval = None   # the page agrees with the server - this approval would be refused at send
    approval_valid = approval is not None
    sent_version = bool(package) and status.is_sent_record(record)              # the rule the write routes refuse by
    send_state = send_state_for_record(record, approval_valid)
    # The banner's words: a stored attempt keeps the persistent send banner (read from the attempt registry, shown in
    # the fixed time format); a package with no attempt is just "Not sent yet" / "Approved, not sent yet".
    if package and record.state != "none":
        if send_state["state"] == "confirmed":
            send_status = f"Received by PayPal on {timefmt.display(record.confirmed_at)}"
        elif send_state["state"] in ("partial", "later-file"):
            send_status = status.evidence_status(record, approval_valid).label
        elif send_state["state"] == "result-uncertain":
            # one state, one title: the words of the result page (routes_send.UNCERTAIN_HEADLINE)
            send_status = status.EVIDENCE_TABLE["attempted"][1]
        else:
            send_status = timefmt.localize_text(send_service.seller_words(
                send_status_banner(db_path, dispute_id, package, claims, approved=approval_valid),
                next_step.seller_evidence_listed(dispute)))
    else:
        send_status = status.evidence_status(record, approval_valid).label
    csrf_token = new_csrf_token()
    rules = None if no_rule_set_line else cached_rules()
    # Published claim-level exclusions matched by code on the dispute PayPal just returned (`raw`, the GET
    # above; dispute_channel is not kept by normalize()). Information only: the checks above are computed without it.
    eligibility_notes = notes_for(raw, rules) if package else []
    # checklist.py's own per-field messages (unmodified) already say a
    # field is "not legible" one row at a time; this names which uploaded FILE could not be read at all,
    # so the vendor knows to type the values below or upload a text PDF instead, not just that something
    # somewhere is unverifiable.
    unreadable_documents = [d for d in documents if not d["extraction_ok"]]
    # checklist.py's own seller_message (unmodified) never
    # says HOW a value was obtained - a check backed by a vendor-confirmed image reading or a manual
    # entry must never read as if PayPal or an independently-read document verified it. Presentation
    # only: one extra note per check, computed here from check.sources, checklist.py itself untouched.
    documents_by_id = {str(d["id"]): d for d in documents}
    check_notes = {}
    for chk in checks:
        notes = []
        for src in chk.sources:
            if not src.startswith("document:"):
                continue
            doc_key = src.split(":", 1)[1]
            if doc_key == "manual":
                notes.append("Entered by you")
            elif doc_key in documents_by_id:
                d = documents_by_id[doc_key]
                if not d["extraction_ok"] and repo.get_confirmed_readings(db_path, d["id"]):
                    notes.append(f"Confirmed by you from {d['filename']}")
        if notes:
            check_notes[chk.id] = " / ".join(dict.fromkeys(notes))   # de-duplicated, order kept
    doc_names = {str(d["id"]): d["filename"] for d in all_documents}
    source_documents = {str(d["id"]): d for d in all_documents}
    for c in claims:
        c["source_labels"] = _claim_sources(c, doc_names)
        c["source_documents"] = [source_documents[sid.split("_", 1)[1]]
                                  for sid in (c.get("source_ids") or [])
                                  if sid.startswith("doc_") and sid.split("_", 1)[1] in source_documents]
        c["basis_label"] = _CLAIM_BASIS_LABELS.get(c["basis"], c["basis"])
        if c.get("suggested_field"):
            c["reading_label"] = _FIELD_LABELS.get(c["suggested_field"]) or field_words.field_word(c["suggested_field"])
            c["reading_value"] = _reading_value(c["suggested_field"], c.get("confirmed_value") or c.get("suggested_value"))
            c["reading_file"] = doc_names.get(str(c.get("document_id")), "your image")
    delivery_block_items = (_delivery_review_block_items(db_path, dispute_id, claims, checks)
                            if package and not no_rule_set_line else [])
    # Presentation of the exact loaded policy; no rule change or extra GET.
    total = context.get("total_amount_cents") if context else None
    currency = context.get("total_currency") if context else None
    signature_limit = rules.signature_limits.get(str(currency).upper()) if rules else None
    signature_required = bool(rules and rules.signature_required and signature_limit
                              and type(total) is int and total > signature_limit["limit_minor"])

    carrier_required = config.feature_enabled("carrier_required")
    carrier_missing_flag = False
    if package and config.feature_enabled("carrier_required"):     # the flag call guards the feature import (export closure)
        from copilot.features.carrier_required.service import carrier_missing
        carrier_missing_flag = carrier_missing(send_summary["carrier"])
    bilingual = None
    if package and config.feature_enabled("bilingual"):             # the flag call guards the feature import (export closure)
        from copilot.features import bilingual as bilingual_feature
        bilingual = bilingual_feature.view(db_path, package["id"])
    transit_delivered_guidance = None
    if package and config.feature_enabled("transit_delivered_guide"):
        from copilot.features.transit_delivered_guide.service import guidance_for
        transit_delivered_guidance = guidance_for((manual_fields.get("delivery_status") or {}).get("value"))
    clauses = None
    if package and config.feature_enabled("clauses"):               # the flag call guards the feature import (export closure)
        from copilot.features.clauses.service import clauses_for
        clauses = clauses_for(rules, checks)
    has_tracking = bool(send_summary and send_summary["tracking_number"])
    has_conflict = any(c.state == "conflict" for c in checks)
    chip = status.package_status(record, approval_valid, has_conflict=has_conflict,
                                 missing_required=bool(package) and (not has_tracking or carrier_missing_flag))
    other_unconfirmed = bool(send_summary and send_summary["has_unconfirmed"]
                             and record.state not in ("reserved", "attempted", "partial"))
    non_empty_claims = sum(1 for c in claims if (c.get("text") or "").strip())
    collision = (status.statement_collision(db_path, dispute_id, package)
                 if package and record.state == "none" else None)
    # the earlier unconfirmed send(s) reached PayPal without a file (a true partial): the words must not tell the
    # seller to tick "the earlier send is not there" (it is there, incomplete). Words only; which rows may be marked is the
    # send service's rule and does not change.
    earlier_partial = bool(send_summary and send_summary["earlier_partial"])
    step = (next_step.for_package(
        dispute_id=dispute_id, package=package, norm=dispute, claims_count=non_empty_claims,
        files_count=len(attachments), has_tracking=has_tracking, carrier_missing=carrier_missing_flag,
        approval_valid=approval_valid, record=record, other_unconfirmed=other_unconfirmed,
        review_flags=tuple(package["review_flags"]) if package else (), hosted=config.hosted_demo(),
        approval_stale=approval_stale, collision=collision, earlier_partial=earlier_partial,
        send_notes=send_summary["notes"] if send_summary and approval_valid else None,
        send_tracking=(f"{send_summary['tracking_number']} ({send_summary['carrier']})" if send_summary and send_summary["carrier"]
                       else send_summary["tracking_number"] if send_summary else None),
        send_files=tuple(send_summary["attachments"]) if send_summary else (),
        send_fingerprint=send_summary["fingerprint"] if send_summary else None,
        conflict_line=blocking_conflict_line(checks)) if package else None)

    needed = ["delivery_status", "delivery_date", "postal_code", "city", "state"]
    signature_check = [c for c in checks if c.id == "signature"]
    if signature_check and signature_check[0].state != "not_applicable":
        needed.append("signature")
    present = _present_fields(db_path, dispute_id) if package else set()
    groups = _shipment_groups(needed=needed, carrier_required=carrier_required, present=present)
    needed_missing = [n for n in ["carrier"] + needed if n not in present] if package else []
    shipment_open = bool(form_errors) or not has_tracking or carrier_missing_flag or bool(needed_missing)

    reviewing = bool(config.feature_enabled("outcome") and dispute.get("stage") == "CHARGEBACK"
                     and dispute.get("status") == "UNDER_REVIEW")
    check_rows = _check_rows(checks, dispute_id, package_id, documents_by_id, reviewing)
    _settle_case_request_rows(check_rows, approval_valid and bool(send_summary and send_summary["sendable"]))
    form_values = form_values or {}
    # the tracking registered for the sale is offered, never saved or prefilled behind the seller's
    # back; the link (?use_tracking=1) only fills the inputs of this render, and Save shipment details stays the only write.
    tracker_note, use_tracking = None, False
    if package and not sent_version:
        on_file = _tracker_on_file(db_path, dispute)
        package_number = send_summary["tracking_number"] if send_summary else None
        if on_file is not None and on_file["number"] != (package_number or ""):
            use_tracking = request.query_params.get("use_tracking") == "1" and not form_values
            tracker_note = {**on_file, "package_number": package_number, "used": use_tracking}
            if use_tracking:
                form_values = {"tracking_number": on_file["number"], "carrier": on_file["carrier_code"],
                               "carrier_other_name": on_file["carrier_other"]}
                shipment_open = True
    document_proposal = None
    if package and not sent_version and not form_errors and config.feature_enabled("seller_shortcuts"):
        proposal = _document_proposal(db_path, dispute_id, all_documents, bool(non_empty_claims))
        if proposal:      # shown in the inputs of this render only: Save shipment details and Add this statement stay the writes
            details_saved = bool((manual_fields.get("tracking_number") or {}).get("value"))
            proposed_values = {}
            if not details_saved and not any(k in form_values for k in ("tracking_number", "carrier")):
                proposed_values.update({"tracking_number": proposal["tracking"], "carrier": proposal["carrier_code"],
                                        "carrier_other_name": proposal["carrier_other"]})
                shipment_open = True
            if proposal["statement"] and "new-claim" not in form_values:
                proposed_values["new-claim"] = proposal["statement"]
            if proposed_values:
                form_values = {**form_values, **proposed_values}
                document_proposal = {**proposal, "details": "tracking_number" in proposed_values,
                                     "statement_proposed": "new-claim" in proposed_values}
    stored_carrier = (manual_fields.get("carrier") or {}).get("value", "")
    carrier_current = form_values["carrier"] if "carrier" in form_values else stored_carrier
    done = (_done_info(request.query_params, db_path=db_path, dispute_id=dispute_id, package=package, claims=claims,
                       attachments=attachments, documents=all_documents, record=record, approval=approval,
                       manual_fields=manual_fields)
            if package and not form_errors else None)
    # Files of the response: every document the seller can still see, plus any attached file that is left out of the
    # checks (it is still sent, so it is listed as sent).
    file_rows = []
    for d in all_documents:
        if d["excluded"] and d["id"] not in attached_ids:
            continue
        file_rows.append({**d, "attached": d["id"] in attached_ids})
    dossier = None
    dossier_error = None
    evaluation_manifest = None
    evaluation_error = None
    if package and (config.feature_enabled("evidence_dossier") or config.feature_enabled("evaluation_workbench")):
        try:
            from copilot.features.evidence_dossier.service import assemble_case
            dossier = assemble_case(raw, dispute, claims, all_documents, attachments, manual_fields,
                                    config.uploads_dir())
        except (OSError, TypeError, ValueError) as exc:
            dossier_error = str(exc)
    if package and config.feature_enabled("evaluation_workbench"):
        try:
            from copilot.features.evaluation_workbench.service import case_manifest
            if dossier is None:
                raise ValueError(dossier_error or "The source-backed case dossier is unavailable.")
            evaluation_manifest = case_manifest(dispute_id, package, dossier)
        except (OSError, TypeError, ValueError) as exc:
            evaluation_error = str(exc)
    sale_readiness = None
    sale_readiness_error = None
    sale_capture = next((c for c in (dispute.get("seller_transaction_ids") or [])
                         if isinstance(c, str) and _CAPTURE_ID.fullmatch(c)), None)
    if package and config.feature_enabled("sale_readiness") and sale_capture:
        try:
            from copilot.features.sale_readiness import service as readiness
            terms = readiness.load_case_terms(dispute_id, sale_capture)
            saved_tracking = None
            if config.feature_enabled("fulfilment"):
                from copilot.features.fulfilment import store as fulfilment_store
                existing_file = fulfilment_store.get_file(sale_capture)
            else:
                existing_file = None
            if existing_file:
                try:
                    tracker = (json.loads(existing_file.get("request_json") or "{}").get("trackers") or [{}])[0]
                    saved_tracking = {"tracking_number": tracker.get("tracking_number"), "carrier": tracker.get("carrier")}
                except (TypeError, ValueError, IndexError):
                    saved_tracking = None
            sale_context = {name: context.get(name) for name in
                            ("total_amount_cents", "total_currency", "recipient_name", "shipping_address")} if context else {}
            sale_readiness = readiness.case_view(dispute_id, sale_capture, sale_context, all_documents,
                                                  attachments, terms, saved_tracking)
        except (OSError, TypeError, ValueError) as exc:
            sale_readiness_error = str(exc)
    ctx = {"adversarial_review": adversarial_review, "adversarial_review_error": adversarial_review_error,
           "dispute_id": dispute_id, "package": package, "claims": claims, "checks": checks, "claim_verdicts": claim_verdicts,
          "compact_case": compact_case_header(dispute, raw),
          "signature_required":signature_required,
          "delivery_scan_pending":any(c.id == "delivery_status" and c.action == "check_status" for c in checks),
          "policy_source": policy_source(),
          "form_values": form_values, "form_errors": form_errors or {}, "payment_date": payment_date,
          "check_notes": check_notes,
          "eligibility_notes": eligibility_notes, "eligibility_effect": ELIGIBILITY_EFFECT_SENTENCE,
          "context_limitations": [lim.message for lim in context.limitations] if context else [],
          "manual_fields": manual_fields, "manual_field_names": MANUAL_FIELD_NAMES,
          "rules_source_title": rules.source_title if rules else None,
          "rules_source_url": rules.source_url if rules else None, "no_rule_set_line": no_rule_set_line,
          "approval": approval, "csrf_token": csrf_token, "names": names,
          "delivery_block_items": delivery_block_items,
          "documents": documents, "attachments": attachments, "attached_ids": attached_ids,
          "file_rows": file_rows, "unreadable_documents": unreadable_documents, "send_status": send_status,
          "sale_readiness": sale_readiness, "sale_readiness_error": sale_readiness_error,
          "sale_readiness_words": case_workspace.sale_readiness_words(sale_readiness),
          "sale_capture": sale_capture,
          "checked_at": checked_at, "send_state": send_state, "record": record, "sent_version": sent_version,
          "sent_view": _sent_view(db_path, package, record) if package else None,
          "partial": {"text": (next_step.partial_listed_text(next_step.partial_names(record))
                               if next_step.seller_evidence_listed(dispute)
                               else next_step.partial_text(next_step.partial_names(record)))},
          "later": {"text": next_step.later_file_text(record) + ".", "wont": next_step.will_not_send_text(record),
                    "removed": next_step.removed_text(record)},
          "chip": chip, "step": step, "done": done, "other_unconfirmed": other_unconfirmed,
          "same_statements_pending": bool(collision is not None and collision.unconfirmed),
          "earlier_partial": earlier_partial, "once_per_dispute": next_step.ONCE_PER_DISPUTE,
          "offered": next_step.can_provide(dispute), "not_asking": next_step.NOT_ASKING_NOTE,
          "listed": next_step.seller_evidence_listed(dispute),
          "abandoned_partial": status.abandoned_partial(record), "resolution_center_name": links.RESOLUTION_CENTER_NAME,
          "has_tracking": has_tracking, "carrier_required": carrier_required, "carrier_missing": carrier_missing_flag,
          "shipment_fields": SHIPMENT_FIELDS, "shipment_selects": SHIPMENT_SELECTS, "shipment_dates": SHIPMENT_DATES,
          "shipment_needed": needed, "shipment_groups": groups, "shipment_open": shipment_open,
          "shipment_filled": any(isinstance(v, dict) and v.get("value") for v in (manual_fields or {}).values()),
          "transit_delivered_guidance": transit_delivered_guidance,
          "carrier_current": carrier_current, "carrier_other": form_values.get("carrier_other_name", ""),
          "tracking_current": form_values.get("tracking_number",
                                              (manual_fields.get("tracking_number") or {}).get("value", "")),
          "not_inr": bool(dispute.get("reason")) and dispute.get("reason") != INR_REASON,
          "check_groups": [(title, [c for c in checks if c.state == state])
                           for state, title in _CHECK_GROUP_TITLES],
          "tracker_note": tracker_note, "document_proposal": document_proposal,
          "send_summary": send_summary, "checks_count": _checks_count(checks) if package else None,
          "check_labels": {c.id: seller_label(c.label_for_seller) for c in checks},
          "check_rows": check_rows, "bilingual": bilingual, "clauses": clauses,
          # what PayPal's record listed when the send was confirmed (lane RX's SendRecord.readback; _paypal_record.html)
          "readback_label": (humanize_evidence_type(record.readback.get("evidence_type"))
                             if getattr(record, "readback", None) else None),
          "evidence_dossier": dossier, "evidence_dossier_error": dossier_error,
          "evaluation_manifest": evaluation_manifest, "evaluation_error": evaluation_error}
    response = templates.TemplateResponse(request, "package_review.html", ctx,
                                          status_code=400 if form_errors else 200 if package else 404,
                                          headers={"Cache-Control": "no-store"})
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/disputes/{dispute_id}/packages/{package_id}/attach/{document_id}", response_class=HTMLResponse)
def attach_document(request: Request, dispute_id: str, package_id: int, document_id: int,
                    csrf_token: str = Form(...)):
    """'Attach to the response' (the first real send must be
    representative of the demo, evidence plus a document, not JSON-only). Re-validates type/size via
    content_type.py even though upload already did (defense in depth: a stored row's content_type/size
    are trusted here only as metadata, this is a cheap, real re-check against the actual limits)."""
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    package, refusal = _own_package(request, dispute_id, package_id)
    if refusal is not None:
        return refusal
    doc, refusal = _own_document(request, dispute_id, document_id)
    if refusal is not None:
        return refusal
    if _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package_id)
    back = dict(back_url=_package_url(dispute_id, package_id) + "#attachments", back_label="Go back to this package")
    if config.feature_enabled("evidence_dossier"):
        # Current bytes only: no shared lock with writers or the later send.
        import hashlib
        from copilot.core.raw_storage import read_raw
        digest = doc.get("sha256")
        original = None
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            try:
                original = read_raw(config.uploads_dir(), digest)
            except OSError:
                original = None
        if (not isinstance(original, bytes) or hashlib.sha256(original).hexdigest() != digest
                or type(doc.get("size_bytes")) is not int or len(original) != doc["size_bytes"]):
            return refusal_page(request, 409, "Cannot attach this file",
                                "The original file is missing or changed. Upload a new original and review it.", **back)
    if doc["content_type"] not in ALLOWED_CONTENT_TYPES:
        return refusal_page(request, 400, "Cannot attach this file", problem_message(PROBLEM_TYPE), **back)
    attached_now = [a for a in repo.list_attachments(db_path, package_id) if a["document_id"] != document_id]
    if any(a["filename"] == doc["filename"] for a in attached_now):
        # PayPal lists attached files by NAME only, so two files with one name could never be confirmed
        # separately by the read-back (one listed name would confirm both). Refused before anything is written.
        return refusal_page(request, 400, "Cannot attach this file", DUPLICATE_NAME_MESSAGE.format(name=doc["filename"]),
                            **back)
    other_sizes = [a["size_bytes"] for a in attached_now]
    problems = validate_call_total(other_sizes + [doc["size_bytes"]])
    if problems:
        return refusal_page(request, 400, "Cannot attach this file", problems[0], **back)
    repo.attach_document(db_path, package_id, document_id)
    repo.log_action(db_path, "vendor", "attach_document", dispute_id,
                    {"package_id": package_id, "document_id": document_id})
    cleared = _clear_approval(db_path, dispute_id, package_id)
    return _done_redirect(dispute_id, package_id, "attached", "attachments", cleared=cleared, doc=document_id)


@router.post("/disputes/{dispute_id}/packages/{package_id}/detach/{document_id}", response_class=HTMLResponse)
def detach_document(request: Request, dispute_id: str, package_id: int, document_id: int,
                    csrf_token: str = Form(...)):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    package, refusal = _own_package(request, dispute_id, package_id)
    if refusal is not None:
        return refusal
    doc, refusal = _own_document(request, dispute_id, document_id)
    if refusal is not None:
        return refusal
    if _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package_id)
    repo.detach_document(db_path, package_id, document_id)
    repo.log_action(db_path, "vendor", "detach_document", dispute_id,
                    {"package_id": package_id, "document_id": document_id})
    cleared = _clear_approval(db_path, dispute_id, package_id)
    return _done_redirect(dispute_id, package_id, "detached", "attachments", cleared=cleared, doc=document_id)


@router.post("/disputes/{dispute_id}/packages/{package_id}/claims", response_class=HTMLResponse)
def add_claim(request: Request, dispute_id: str, package_id: int, text: str = Form(default=""),
             csrf_token: str = Form(...), client=Depends(get_client)):
    """Manual mode: the vendor adds a claim by hand. Always basis=seller_statement - it is the
    vendor's own statement, never presented as independently verified."""
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
    if not text.strip():
        return render_package(request, dispute_id, package_id, client,
                              form_values={"new-claim": text},
                              form_errors={"new-claim": "Enter a statement before saving."})
    existing = repo.list_claims(db_path, package_id)
    next_index = _next_claim_index(existing)                                    # never reuse an index in use
    repo.insert_claims(db_path, package_id, [{"evidence_index": 0, "claim_index": next_index,
                                              "text": _normalise_statement(text),
                                              "source_ids": [], "basis": "seller_statement"}])
    repo.log_action(db_path, "vendor", "add_claim", dispute_id, {"package_id": package_id})
    cleared = _clear_approval(db_path, dispute_id, package_id)
    claims = repo.list_claims(db_path, package_id)
    newest = max(claims, key=lambda c: c["id"])
    position = [c["id"] for c in claims].index(newest["id"]) + 1
    return _done_redirect(dispute_id, package_id, "statement_added", "statements", cleared=cleared, n=position)


@router.post("/disputes/{dispute_id}/packages/{package_id}/claims/{claim_id}", response_class=HTMLResponse)
def edit_claim(request: Request, dispute_id: str, package_id: int, claim_id: int, text: str = Form(default=""),
              csrf_token: str = Form(...), client=Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    package, refusal = _own_package(request, dispute_id, package_id)
    if refusal is not None:
        return refusal
    if claim_id not in {c["id"] for c in repo.list_claims(db_path, package_id)}:
        return refusal_page(request, 404, "Statement not found", "That statement could not be found in this package.",
                            back_url=_package_url(dispute_id, package_id) + "#statements",
                            back_label="Go back to this package")
    if _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package_id)
    if not text.strip():
        key = f"claim-{claim_id}"
        return render_package(request, dispute_id, package_id, client,
                              form_values={key: text},
                              form_errors={key: "Enter a statement before saving."})
    repo.mark_claim_edited(db_path, claim_id, _normalise_statement(text))
    repo.log_action(db_path, "vendor", "edit_claim", dispute_id,
                    {"package_id": package_id, "claim_id": claim_id})
    # Any edit invalidates a prior approval of this package (the fingerprint was
    # computed over the old content).
    cleared = _clear_approval(db_path, dispute_id, package_id, log=True)
    return _done_redirect(dispute_id, package_id, "statement_saved", f"claim-{claim_id}", cleared=cleared,
                          claim=claim_id)


@router.post("/disputes/{dispute_id}/packages/{package_id}/claims/{claim_id}/remove", response_class=HTMLResponse)
def remove_claim(request: Request, dispute_id: str, package_id: int, claim_id: int, csrf_token: str = Form(...)):
    """The seller takes one statement out of a draft version. Never on a sent version; the approval, if any, is
    cleared (the content changed) and the removal is audited."""
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    db_path = config.db_path()
    package, refusal = _own_package(request, dispute_id, package_id)
    if refusal is not None:
        return refusal
    claims = repo.list_claims(db_path, package_id)
    position = next((i + 1 for i, c in enumerate(claims) if c["id"] == claim_id), None)
    if position is None:
        return refusal_page(request, 404, "Statement not found", "That statement could not be found in this package.",
                            back_url=_package_url(dispute_id, package_id) + "#statements",
                            back_label="Go back to this package")
    if _is_sent_version(db_path, dispute_id, package):
        return _sent_version_refusal(request, dispute_id, package_id)
    if not repo.delete_claim(db_path, package_id, claim_id):
        return refusal_page(request, 404, "Statement not found", "That statement could not be found in this package.",
                            back_url=_package_url(dispute_id, package_id) + "#statements",
                            back_label="Go back to this package")
    repo.log_action(db_path, "vendor", "remove_claim", dispute_id, {"package_id": package_id, "claim_id": claim_id})
    cleared = _clear_approval(db_path, dispute_id, package_id, log=True)
    return _done_redirect(dispute_id, package_id, "statement_removed", "statements", cleared=cleared, n=position)


def _delivery_block_items(claims: list, unsupported: list, checks: list, extracted_docs: list, documents: list) -> list:
    """One sentence per blocked statement, in statement order, from presentation.delivery_block_message. The
    statement is found by its text; the kind says which rule blocked it."""
    names = {str(d["id"]): d["filename"] for d in documents}
    untrusted = untrusted_document_ids(checks, extracted_docs)
    unreadable = {str(d.doc_id) for d in extracted_docs
                  if d.doc_id != "manual" and any((f or {}).get("confidence") == "illegible"
                                                   for f in (d.fields or {}).values())}
    items = []
    for problem in unsupported:
        number = next((i + 1 for i, c in enumerate(claims) if c["text"] == problem["text"]), None)
        if number is None:
            items.append(problem["text"])
            continue
        claim = claims[number - 1]
        cited = [sid.split("_", 1)[1] for sid in claim.get("source_ids", []) if sid.startswith("doc_")]
        if problem.get("reason") == "untrusted_document":
            doc_id = next((d for d in cited if d in untrusted), None)
            filename = names.get(doc_id, "a document")
            row = next((d for d in documents if str(d["id"]) == doc_id), None)
            kind = "unreadable" if doc_id in unreadable or (row is not None and not row["extraction_ok"]) else "untrusted"
            message = presentation.delivery_block_message(number, problem["matched_phrase"], kind, filename)
        elif problem.get("label") == "claim":
            message = presentation.delivery_block_message(number, problem["matched_phrase"], "conflict")
        else:
            message = presentation.delivery_block_message(number, problem["matched_phrase"], "unsupported")
        items.append(message or problem["text"])
    return list(dict.fromkeys(items))


def _delivery_review_block_items(db_path, dispute_id: str, claims: list, checks: list) -> list:
    """Read-only presentation of the same unsupported delivery conditions checked at approval."""
    out_for_check = {"proposed_evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "",
                                             "tracking_info": [],
                                             "claims": [{"text": c["text"], "source_ids": c["source_ids"]}
                                                        for c in claims]}]}
    source_text, _names = docs_for_dispute(db_path, dispute_id)
    extracted_docs = extracted_docs_for(db_path, dispute_id)
    untrusted = untrusted_document_ids(checks, extracted_docs)
    unsupported = (find_unsupported_delivery_assertions(out_for_check,
                                                        {"paypal_dispute": "{}", **source_text}, untrusted) +
                   find_delivery_claims_citing_conflicting_sources(claims, checks) +
                   find_delivery_claims_citing_untrusted_documents(claims, checks, extracted_docs))
    if not unsupported:
        return []
    return _delivery_block_items(claims, unsupported, checks, extracted_docs,
                                 repo.list_documents(db_path, dispute_id))


@router.post("/disputes/{dispute_id}/packages/{package_id}/approve", response_class=HTMLResponse)
def approve_package(request: Request, dispute_id: str, package_id: int, csrf_token: str = Form(...),
                    sufficiency_override: str = Form(default=""), conflict_note: str = Form(default=""),
                    client: SandboxClient = Depends(get_client)):
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
    claims = repo.list_claims(db_path, package_id)
    # dispute_id is already validated above; the templates autoescape it when it is printed, so nothing here
    # trusts the path segment as markup.
    back_url = f"/disputes/{dispute_id}/packages/{package_id}"
    back = dict(back_url=back_url, back_label="Go back to this package")
    if len(conflict_note.strip()) > NOTE_MAX_LENGTH:
        return refusal_page(request, 400, "This package cannot be approved yet", NOTE_TOO_LONG, **back)

    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    dispute = normalize(raw, datetime.now(timezone.utc))
    try:
        checks, _context = _checks_for(dispute, dispute_id, client, db_path)
    except UnknownCountryError:
        return refusal_page(request, 400, "This package cannot be approved yet", _no_rule_set_line(), **back)

    # A delivery/signature/receipt assertion with no source backing it blocks approval outright - the
    # vendor must fix the text (or the model must reformulate it); this is never overridable, unlike the
    # "no proof of delivery at all" advisory, which the vendor may proceed past.
    out_for_check = {"proposed_evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "",
                                            "tracking_info": [],
                                            "claims": [{"text": c["text"], "source_ids": c["source_ids"]}
                                                      for c in claims]}]}
    docs, _names = docs_for_dispute(db_path, dispute_id)
    sources = {"paypal_dispute": "{}", **docs}
    extracted_docs = extracted_docs_for(db_path, dispute_id)
    if config.feature_enabled("carrier_required"):
        from copilot.features.carrier_required.service import MESSAGE, carrier_missing
        tracking_number, carrier = pick_tracking(extracted_docs)
        if tracking_number and carrier_missing(carrier):
            return refusal_page(request, 400, "This package cannot be approved yet", MESSAGE, **back)
    untrusted = untrusted_document_ids(checks, extracted_docs)
    unsupported = (find_unsupported_delivery_assertions(out_for_check, sources, untrusted) +
                  find_delivery_claims_citing_conflicting_sources(claims, checks) +
                  find_delivery_claims_citing_untrusted_documents(claims, checks, extracted_docs))
    if unsupported:
        return refusal_page(
            request, 400, "This package cannot be approved yet",
            # the Markup variant (Spanish words marked lang="es" in a translate="no" span)
            presentation.DELIVERY_WORDS_HINT_HTML,
            items=_delivery_block_items(claims, unsupported, checks, extracted_docs,
                                        repo.list_documents(db_path, dispute_id)), **back)

    if any(a["excluded"] for a in repo.list_attachments(db_path, package_id)):
        return refusal_page(request, 400, "This package cannot be approved yet", send_service.LEFT_OUT_ATTACHED_BLOCK, **back)

    # A checklist conflict cannot pass silently into approval - the vendor must explicitly
    # review it (fix the data, remove the document, or approve anyway while noting the discrepancy).
    conflicts = [c for c in checks if c.state == "conflict"]
    note = conflict_note.strip() or None
    if conflicts and not note:
        docs_table = repo.list_documents(db_path, dispute_id)
        doc_ids_in_conflict = sorted({src.split(":", 1)[1] for c in conflicts for src in c.sources
                                      if src.startswith("document:") and src.split(":", 1)[1] != "manual"})
        return refusal_page(
            request, 400, "This package conflicts with PayPal's data",
            "Cannot approve: at least one check conflicts with PayPal's own data. Review the difference first:",
            items=[f"{c.label_for_seller}: {c.seller_message}" for c in conflicts],
            conflict_form={"documents": [d for d in docs_table if str(d["id"]) in doc_ids_in_conflict
                                         and not d["excluded"]],
                           "dispute_id": dispute_id, "package_id": package_id, "csrf_token": csrf_token}, **back)
    if note:
        existing = repo.list_claims(db_path, package_id)
        repo.insert_claims(db_path, package_id, [{"evidence_index": 0, "claim_index": _next_claim_index(existing),
                                                  "text": note, "source_ids": [], "basis": "seller_statement"}])
        repo.log_action(db_path, "vendor", "approve_despite_conflict", dispute_id,
                        {"package_id": package_id, "conflicts": [c.id for c in conflicts], "note": note})

    flags = (_open_flags(package, repo.list_claims(db_path, package_id), repo.get_manual_fields(db_path, dispute_id))
             if package else [])
    override_text = sufficiency_override.strip() or None
    if flags and not override_text:
        return refusal_page(
            request, 400, "This package has open notices",
            "This package has open notices that need an explicit decision before you can approve it. Go back and "
            "either resolve them or confirm you want to proceed anyway, with a short note explaining why.",
            items=list(flags), **back)

    # The real fingerprint once there is enough to actually send; otherwise a placeholder that can
    # never equal a real one, so copilot/web/routes_send.py correctly refuses until re-approved with
    # tracking present - approving a claims-only draft before tracking is known is still allowed.
    all_claims = repo.list_claims(db_path, package_id)   # re-read: approve_despite_conflict may have added one
    tracking_number, carrier = pick_tracking(extracted_docs_for(db_path, dispute_id))
    attachment_shas = _attachment_shas(db_path, package_id)
    try:
        req_input = build_request_input(all_claims, tracking_number, carrier)
        approval_sha256 = approval_fingerprint_with_attachments(dispute_id, "provide_evidence", req_input,
                                                                 attachment_shas)
    except ValueError:
        approval_sha256 = f"not-sendable-yet-{package_id}"

    approval_id = repo.insert_approval(db_path, package_id, approval_sha256=approval_sha256,
                                       approved_by="vendor", sufficiency_override=override_text)
    repo.log_action(db_path, "vendor", "approve_package", dispute_id,
                    {"package_id": package_id, "approval_id": approval_id,
                     "sufficiency_override": bool(override_text)})
    return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}?done=approved#next-step", status_code=303)
