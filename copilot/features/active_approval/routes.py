"""Local review routes and a guard for the passive approval endpoint."""
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from copilot import config
from copilot.core import carriers
from copilot.core.send import DEMO_PREFIX
from copilot.db import repo
from copilot.features.active_approval import reapproval, rules, service
from copilot.features.claims.model import safe_text
from copilot.features.claims.service import document_names
from copilot.web import presentation, status
from copilot.web.routes_disputes import compact_case_header, csrf_expired_page, get_client, refusal_page, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()


class PassiveApprovalGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if (config.feature_enabled("active_approval") and request.method == "POST"
                and re.fullmatch(r"/disputes/[^/]+/packages/[^/]+/approve/?", request.url.path)):
            return refusal_page(request,409,"Review each statement before approving",
                                "Review each statement on the active approval page before approving.",
                                back_url=request.url.path.rsplit("/",1)[0]+"/active-approval",
                                back_label="Review the statements")
        return await call_next(request)


def register(app):
    app.include_router(router)
    app.add_middleware(PassiveApprovalGuard)


@router.get("/disputes/{dispute_id}/packages/{package_id}/active-approval")
def page(request: Request, dispute_id: str, package_id: int, client=Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    try:
        review = service.read(client, config.db_path(), dispute_id, package_id)
    except service.ReviewError as exc:
        return refusal_page(request, 400, "Cannot review this package", str(exc))
    return render_review(request, dispute_id, package_id, review)


# Where each non-delivery block is fixed on the package page: (start of the message, link text, anchor).
_BLOCK_FIXES = (("Pick a carrier", "Choose the carrier", "#field-carrier"),
                ("A check conflicts", "Review the difference", "#checks"),
                ("A statement or source includes private", "Edit the statement", "#statements"),
                ("Add at least one statement", "Add a statement", "#statements"),
                ("A statement asserts delivery", "Edit the statement", "#statements"))


def _blocked_items(review, package_url):
    """What stops the approval, one item per cause: a delivery statement is named by its number and linked to that
    statement on the package page (found by claim id); the other causes link to where they are fixed."""
    items = []
    for detail in review.blocked_details:
        text = presentation.delivery_block_message(detail["number"], detail["phrase"], detail["kind"],
                                                   safe_text(detail["filename"]) if detail["filename"] else None)
        if text is None:
            continue
        items.append({"text": text, "link": f"Edit statement {detail['number']}",
                      "href": f"{package_url}#claim-{detail['claim_id']}"})
    named = bool(items)
    for message in review.blocked:
        if named and message.startswith("A statement asserts delivery"):
            continue                      # said above, statement by statement
        link, anchor = next(((link, anchor) for start, link, anchor in _BLOCK_FIXES if message.startswith(start)),
                            ("Open the package", "#next-step"))
        items.append({"text": safe_text(message), "link": link, "href": package_url + anchor})
    return items


def _send_summary(db_path, package_id, review):
    """What the send will carry, exactly as the shared send service builds it (core/send.build_request_input)."""
    request = review.request_input
    if not request:
        return None
    evidence = request["evidences"][0]
    tracking = (evidence["evidence_info"]["tracking_info"] or [{}])[0]
    code = tracking.get("carrier_name")
    other = tracking.get("carrier_name_other")
    carrier = other if code == "OTHER" and other else carriers.display_name(code)
    if code and code != "OTHER" and carrier != code:
        carrier = f"{carrier} (sent as {code})"
    full = (DEMO_PREFIX + " ".join(c["text"].strip() for c in repo.list_claims(db_path, package_id)
                                   if c.get("text", "").strip())).strip()
    files = [{"filename": a["filename"],
              "excluded": bool((repo.get_document(db_path, a["document_id"]) or {}).get("excluded"))}
             for a in repo.list_attachments(db_path, package_id)]
    return {"notes": evidence["notes"], "was_cut": len(full) > 2000, "tracking": tracking.get("tracking_number", ""),
            "carrier": carrier or "Not set", "files": files}


def _conflict_value_pairs(review, names):
    """Display two existing document readings for a conflicting claim; presentation-only data."""
    pairs = {}
    for claim in review.claims:
        if claim.verdict != "conflict":
            continue
        seen = set()
        sources = []
        for source in claim.sources:
            if (source.kind != "document" or source.check_state != "conflict" or source.value is None
                    or source.artifact_id in seen):
                continue
            seen.add(source.artifact_id)
            sources.append({"filename": names.get(source.artifact_id, "Document"),
                            "field": source.field, "value": source.value})
        if len(sources) >= 2 and sources[0]["value"] != sources[1]["value"]:
            pairs[claim.key] = sources[:2]
    return pairs


def render_review(request, dispute_id, package_id, review, *, decisions=None, error=None, status_code=200,
                  phrase="", override="", review_matches=False):
    decisions = decisions or {}
    confirmed = sum(decisions.get(c.key) == ("seller_statement" if c.verdict == "unverifiable" else "confirmed")
                    for c in review.claims)
    token = new_csrf_token()
    db_path = config.db_path()
    package_url = f"/disputes/{dispute_id}/packages/{package_id}"
    record = status.send_record(db_path, dispute_id, review.package)
    sent = status.is_sent_record(record)      # the approval of this version is spent (confirmed, also with a late file; partial)
    sent_view = None
    if sent:
        from copilot.web.routes_package import _sent_view      # lazy: the package page's own "What was sent" read
        sent_view = _sent_view(db_path, review.package, record)
    state = ("sent" if sent else "approved" if review.approval else "blocked" if review.blocked
             else "cleared" if review.cleared else "typed-required" if review.reasons else "idle")
    done = state in ("approved", "sent")
    sendable = bool(review.request_input) and not review.blocked
    send_fixes = []
    if done and not sent and not sendable:
        if not review.request_input:
            send_fixes.append({"text": "There is no tracking number.", "link": "Type the tracking number",
                               "href": f"{package_url}#field-tracking_number"})
        send_fixes += _blocked_items(review, package_url)
    unticked = next((f"claim-{c.key}" for c in review.claims
                     if decisions.get(c.key) != ("seller_statement" if c.verdict == "unverifiable" else "confirmed")), None)
    # the summary link goes to the control with the problem: the first unticked checkbox (never the list).
    error_field = ("approval-phrase" if error and review.reasons and "phrase" in error.lower() else
                   "approval-override" if error and review.flags and any(s in error.lower() for s in ("note", "notices")) else
                   unticked or "claim-list")
    if state not in ("idle", "typed-required", "cleared"):
        error_field = "approval-header"        # no form is rendered (blocked, approved or sent): link to the heading
    blocked_claim_ids = {d["claim_id"] for d in review.blocked_details}
    proposed = ""
    if (not override and state in ("idle", "typed-required", "cleared") and review.flags
            and config.feature_enabled("seller_shortcuts")):
        proposed = service.proposed_note(review)          # typed for the seller, who edits it or replaces it
    earlier = reapproval.earlier(db_path, dispute_id, package_id) if state in ("blocked", "idle", "typed-required", "cleared") else None
    names = document_names(config.db_path(), dispute_id, review.claims, redact_private=True)
    conflict_value_pairs = _conflict_value_pairs(review, names)
    one_control = bool(state in ("idle", "typed-required", "cleared") and review.claims and config.feature_enabled("seller_shortcuts"))
    response = templates.TemplateResponse(request, "features/active_approval/review.html",
                                          {"dispute_id": dispute_id, "package_id": package_id, "review": review,
                                           "state": state, "csrf_token": token, "rules": rules,
                                           "safe_text": safe_text, "decisions": decisions, "review_error": error,
                                           "error_field": error_field, "entered_phrase": phrase, "entered_override": override or proposed, "note_proposed": bool(proposed), "one_control": one_control,
                                           "confirmed_count": len(review.claims) if done else confirmed,
                                           "document_names": names, "conflict_value_pairs": conflict_value_pairs,
                                           "page_title": "Package approved" if done else "Review each statement and approve",
                                           "heading": "Package approved" if done else "Review each statement before approving",
                                           "summary": _send_summary(db_path, package_id, review),
                                           "blocked_items": _blocked_items(review, package_url) if state == "blocked" else [],
                                           "blocked_claim_ids": blocked_claim_ids,
                                           "has_delivery_block": bool(review.blocked_details), "earlier_approval": earlier,
                                           "send_record": record, "sent_view": sent_view, "sent_at": record.sent_at or record.confirmed_at,
                                           "sendable": sendable, "send_fixes": send_fixes,
                                           "context_limitations": review.context_limitations,
                                           "compact_case": compact_case_header(*review.case_display) if review.case_display else None,
                                           "tick_each": bool(error and review_matches and "every statement" in error)},
                                          status_code=status_code)
    set_csrf_cookie(response, token)
    return response


@router.post("/disputes/{dispute_id}/packages/{package_id}/active-approval")
async def submit(request: Request, dispute_id: str, package_id: int, client=Depends(get_client)):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    form = await request.form()
    if not verify_csrf(request, form.get("csrf_token")):
        return csrf_expired_page(request)
    decisions = [(key.removeprefix("claim-"), value) for key, value in form.multi_items() if key.startswith("claim-")]
    if len({key for key, _ in decisions}) != len(decisions):
        return refusal_page(request, 400, "Cannot approve", "Submit one decision per statement.")
    read_all = None
    if config.feature_enabled("seller_shortcuts") and "read_statements" in form:
        # one control: the seller read the N statements shown; the server makes the per-statement decisions, but only for exactly N
        if decisions:
            return refusal_page(request, 400, "Cannot approve", "Submit either the single approval or one decision per statement.")
        read_all = form.get("read_statements", "")
        read_all = int(read_all) if read_all.isdigit() else -1
    try:
        service.approve(client, config.db_path(), dispute_id, package_id, form.get("review_sha256", ""),
                        dict(decisions), form.get("phrase", ""), form.get("sufficiency_override", ""), read_all=read_all)
    except service.ReviewError as exc:
        try:
            review = service.read(client, config.db_path(), dispute_id, package_id)
        except service.ReviewError:
            return refusal_page(request, 400, "Cannot approve", str(exc))
        matches = form.get("review_sha256") == review.review_sha256
        selected = dict(decisions) if matches else {}
        return render_review(request, dispute_id, package_id, review, decisions=selected,
                             error=str(exc), status_code=400, phrase=form.get("phrase", ""),
                             override=form.get("sufficiency_override", ""), review_matches=matches)
    return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}?done=approved#next-step", status_code=303)
