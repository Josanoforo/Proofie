"""MCP server (stdio) that lets a seller's AI assistant work with Proofie.

Run:  .venv\\Scripts\\python.exe -m copilot.mcp_server

PayPal's own agent toolkit has list/get/accept-claim for disputes but no evidence tools (PayPal's agent-toolkit README, read 2026-10-16, re-read 2026-10-17).
This server fills that gap: read the file, see what is missing, prepare a
DRAFT. It never approves anything - approval is a human act in the web UI (copilot/web/routes_package.py).

Safety model (docs/MCP.md):
  * There is NO approve tool, ever.
  * There is NO send tool unless the env var COPILOT_MCP_ALLOW_SEND=true is set when the server starts.
    Even then, send_approved_package only calls copilot/core/send_service.py directly - the same plain
    Python function the web route (copilot/web/routes_send.py) calls after its own CSRF check - so both
    callers get identical guarantees (approval/fingerprint/attempt-registry/fresh-GET/link-validation/
    reconciliation) and only for a package a human already approved whose approval fingerprint still
    matches the current content. No automatic retry.
  * All PayPal access goes through the existing SandboxClient (sandbox host only, pilot/client.py).
  * Tool arguments are untrusted: dispute_id must match a strict format; no URL is ever followed.
  * Every call is audited as actor "system" with {"via": "mcp"} (copilot/db/repo.py audit_log).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from copilot import config
from copilot.core import auto_prep, send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist import UnknownCountryError, evaluate
from copilot.core.checklist_input import cached_rules, extracted_docs_for
from copilot.core.context import fetch_context
from copilot.core.disputes import (PayPalError, SandboxClient, fetch_all_disputes, humanize_deadline,
                                   humanize_reason, humanize_status, normalize)
from copilot.core.send import (PARTIAL_MESSAGE_MANY, approval_fingerprint_with_attachments, build_request_input,
                               pick_tracking, verify_after_with_attachments)
from copilot.core.send_recovery import partial_message
from copilot.db import repo
from copilot.web.routes_package import _open_flags
from copilot.web.security import DISPUTE_ID_RE, MAX_DISPUTE_ID_LEN

BASE_URL = "http://127.0.0.1:8000"   # where the web UI runs (copilot/app.py); review links point here
ALLOW_SEND_ENV = "COPILOT_MCP_ALLOW_SEND"
LOG_NAME_ENV = "COPILOT_MCP_LOG_NAME"   # optional: which evidence/<name>.jsonl this server appends to
SERVER_NAME = "proofie"                 # the MCP server id (D-2026-10-05-05); clients register it under this name
# DISPUTE_ID_RE/MAX_ID_LEN now live in copilot/web/security.py, so this server and the web routes
# (copilot/web/routes_send.py) validate an untrusted dispute_id against the exact SAME expression - never
# two hand-copied patterns that could silently drift apart.
MAX_ID_LEN = MAX_DISPUTE_ID_LEN

NEEDS_ACTION_STATES = ("missing", "present_unverifiable", "conflict")
APPROVAL_NOTE = ("This server cannot approve anything. Approval happens only in the web UI "
                 f"({BASE_URL}/disputes/<id>), by a human.")


TEXT_OMITTED_NOTE = ("Claim text and the per-check messages are left out because they quote values from the "
                     "seller's documents and MCP results reach the client's model provider. "
                     "Call get_dispute_file again with include_text=true to include them.")


class InvalidDisputeId(ValueError):
    pass


def send_enabled() -> bool:
    return os.environ.get(ALLOW_SEND_ENV, "").strip().lower() == "true"


def mcp_log_name() -> str:
    """The evidence log this server's PayPal calls are appended to (evidence/<name>.jsonl, through SandboxClient, which
    sanitises every line). Default `copilot_mcp`; the optional COPILOT_MCP_LOG_NAME lets a smoke test keep its own
    file. Only [a-z0-9_] up to 40 characters is accepted, so the variable can never point outside evidence/."""
    name = os.environ.get(LOG_NAME_ENV, "").strip()
    return name if re.fullmatch(r"[a-z0-9_]{1,40}", name) else "copilot_mcp"


def _make_client() -> SandboxClient:
    """One short-lived sandbox client per call, same as the web app's get_client. Tests replace this."""
    return SandboxClient(mcp_log_name())


def _audit(tool: str, dispute_id: str | None, **detail) -> None:
    repo.log_action(config.db_path(), "system", f"mcp_{tool}", dispute_id or "-", {"via": "mcp", **detail})


def _validated_id(tool: str, dispute_id) -> str:
    """The id is an untrusted tool argument: strict format only (PayPal ids look like PP-R-ABC-12345678).
    No slash, no scheme, no whitespace - so it can never smuggle a path or URL into a PayPal call."""
    if (not isinstance(dispute_id, str) or len(dispute_id) > MAX_ID_LEN
            or not DISPUTE_ID_RE.fullmatch(dispute_id)):
        _audit(f"{tool}_rejected_invalid_id", None, argument=str(dispute_id)[:60])
        raise InvalidDisputeId("Invalid dispute_id. Expected a PayPal dispute id such as PP-R-ABC-12345678.")
    return dispute_id


def _dispute_url(dispute_id: str) -> str:
    return f"{BASE_URL}/disputes/{dispute_id}"


def _package_url(dispute_id: str, package_id: int) -> str:
    return f"{BASE_URL}/disputes/{dispute_id}/packages/{package_id}"


def _paypal_error(exc: Exception) -> dict:
    status = getattr(exc, "status", None)
    if isinstance(exc, PayPalError) and status == 404:
        return {"error": "PayPal could not find that dispute. It may not exist, or may not belong to this "
                         "account."}
    return {"error": "PayPal could not be reached or refused the request right now. Try again in a moment."}


def _get_dispute(client, dispute_id: str) -> dict:
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "mcp_show_dispute")
    return raw


def _dispute_summary(norm: dict) -> dict:
    return {"dispute_id": norm["dispute_id"], "status": humanize_status(norm["status"]),
            "reason": humanize_reason(norm["reason"]), "amount": norm["amount_display"],
            "deadline": humanize_deadline(norm),
            "requested_from_you": norm["evidence_requested_from_seller"],
            "already_submitted_by_you": norm["evidence_submitted_by_seller"]}


def _checks(client, norm: dict, dispute_id: str):
    context = fetch_context(client, norm)
    checks = evaluate(context, extracted_docs_for(config.db_path(), dispute_id), cached_rules())
    return checks, context


def _check_json(c, include_text: bool = True) -> dict:
    """`message` carries values read from the seller's documents (tracking number, postal code, city, receiver), so
    get_dispute_file leaves it out unless `include_text=true`."""
    out = {"id": c.id, "what": c.label_for_seller, "state": c.state, "basis": c.basis,
           "basis_ref": c.basis_ref, "message": c.seller_message, "action": c.action, "risk": c.risk}
    if not include_text:
        del out["message"]
    return out


def _approval_state(db_path, dispute_id: str, package: dict) -> tuple[str, dict | None]:
    """('approved' | 'approved_but_edited' | 'not_approved', approval row). Recomputes the fingerprint
    exactly like routes_send.send_package, so 'approved' means the approval matches the CURRENT content."""
    approval = repo.get_latest_valid_approval(db_path, package["id"])
    if approval is None:
        return "not_approved", None
    claims = repo.list_claims(db_path, package["id"])
    tracking, carrier = pick_tracking(extracted_docs_for(db_path, dispute_id))
    try:
        req_input = build_request_input(claims, tracking, carrier)
    except ValueError:
        return "approved_but_edited", approval
    # the attachment list the SEND path and the approval build (repo.list_attachments, joined to
    # `documents`), so a vanished document row makes this say "approved_but_edited" exactly as the page and the send do
    fp = approval_fingerprint_with_attachments(dispute_id, "provide_evidence", req_input,
                                               [a["sha256"] for a in repo.list_attachments(db_path, package["id"])])
    return ("approved" if fp == approval["approval_sha256"] else "approved_but_edited"), approval


def _send_banner(db_path, dispute_id: str, package: dict, claims: list, approved: bool,
                 listed: bool | None = None) -> str:
    """`listed` is what the caller's own fresh read says (None: no read was made)."""
    return send_service.seller_words(send_service.send_status_banner(db_path, dispute_id, package, claims,
                                                                      approved=approved) or "Not sent yet", listed)


_APPROVAL_TEXT = {
    "not_approved": "Not approved yet. A human must review and approve it in the web UI.",
    "approved_but_edited": "Was approved, but the content changed since (or tracking is not known yet). "
                           "It must be approved again in the web UI.",
    "approved": "Approved by the seller in the web UI; the approval matches the current content.",
}


def _draft_json(db_path, dispute_id: str, include_text: bool = True, listed: bool | None = None) -> dict | None:
    packages = repo.list_packages(db_path, dispute_id)
    if not packages:
        return None
    package = packages[-1]
    claims = repo.list_claims(db_path, package["id"])
    state, _ = _approval_state(db_path, dispute_id, package)
    out = {"package_id": package["id"], "version": package["version"],
           "origin": "suggested by AI - check against the sources" if package["source"] == "ai"
                     else "typed or prepared manually",
           "open_notices": _open_flags(package, claims, repo.get_manual_fields(db_path, dispute_id)),
           "claim_count": len(claims),
           "approval": state, "approval_text": _APPROVAL_TEXT[state],
           "send_status": _send_banner(db_path, dispute_id, package, claims,
                                       approved=state in ("approved", "approved_but_edited"), listed=listed),
           "review_url": _package_url(dispute_id, package["id"])}
    if include_text:
        out["claims"] = [{"text": c["text"], "basis": c.get("basis"), "source_ids": c.get("source_ids", [])} for c in claims]
    return out


# ------------------------------------------------------------------------------------------------ tools
def list_disputes() -> dict:
    """List the seller's PayPal disputes (read-only GET to PayPal's sandbox). Shows plain-language status,
    the response deadline ONLY if PayPal gives one, and whether the copilot has prepared a draft. This
    server cannot approve or send anything; approval happens in the web UI."""
    _audit("list_disputes", None)
    client = _make_client()
    try:
        items, _pages = fetch_all_disputes(client)
    except (PayPalError, RuntimeError, httpx.TransportError) as exc:
        return _paypal_error(exc)
    now = datetime.now(timezone.utc)
    db_path = config.db_path()
    rows = []
    for raw in items:
        norm = normalize(raw, now)
        prep = repo.get_auto_prep(db_path, norm["dispute_id"])
        rows.append({**_dispute_summary(norm),
                     "auto_prep": {"status": prep["status"], "detail": prep.get("detail")} if prep
                     else {"status": "not_prepared", "detail": None},
                     "review_url": _dispute_url(norm["dispute_id"])})
    return {"count": len(rows), "disputes": rows, "note": APPROVAL_NOTE}


def get_dispute_file(dispute_id: str, include_text: bool = False) -> dict:
    """File for one dispute: PayPal's own context, the evidence checklist (state, basis and action per check) and
    the draft package with its approval and send status. By default it leaves out the claim text and the
    per-check messages, which quote values read from the seller's documents; pass include_text=true to get them.
    Read-only (GETs only). This server cannot approve; approval happens in the web UI."""
    dispute_id = _validated_id("get_dispute_file", dispute_id)
    _audit("get_dispute_file", dispute_id, include_text=bool(include_text))
    client = _make_client()
    try:
        norm = normalize(_get_dispute(client, dispute_id), datetime.now(timezone.utc))
        checks, context = _checks(client, norm, dispute_id)
    except (PayPalError, RuntimeError, httpx.TransportError) as exc:
        return _paypal_error(exc)
    except UnknownCountryError as exc:
        return {"error": f"Cannot check evidence against a known rule set: {exc}"}
    include_text = include_text is True
    return {"dispute": _dispute_summary(norm),
            "context_limitations": [lim.message for lim in context.limitations],
            "checklist": [_check_json(c, include_text) for c in checks],
            "draft": _draft_json(config.db_path(), dispute_id, include_text,
                                 listed=bool(norm.get("evidence_submitted_by_seller"))),
            "text_included": include_text,
            "text_note": None if include_text else TEXT_OMITTED_NOTE,
            "review_url": _dispute_url(dispute_id), "note": APPROVAL_NOTE}


def what_is_missing(dispute_id: str) -> dict:
    """Only the checklist items that need the seller to act (missing, cannot be verified, or in conflict
    with PayPal's data), each with its concrete action. Read-only. This server cannot approve; approval
    happens in the web UI."""
    dispute_id = _validated_id("what_is_missing", dispute_id)
    _audit("what_is_missing", dispute_id)
    client = _make_client()
    try:
        norm = normalize(_get_dispute(client, dispute_id), datetime.now(timezone.utc))
        checks, _ctx = _checks(client, norm, dispute_id)
    except (PayPalError, RuntimeError, httpx.TransportError) as exc:
        return _paypal_error(exc)
    except UnknownCountryError as exc:
        return {"error": f"Cannot check evidence against a known rule set: {exc}"}
    todo = [_check_json(c) for c in checks if c.state in NEEDS_ACTION_STATES]
    return {"dispute_id": dispute_id, "needs_action": todo, "count": len(todo),
            "summary": ("Nothing needs your action on the checklist." if not todo
                        else f"{len(todo)} item(s) need your action."),
            "review_url": _dispute_url(dispute_id), "note": APPROVAL_NOTE}


def prepare_draft(dispute_id: str) -> dict:
    """Run the same idempotent background preparation the inbox runs (PayPal context, checklist, draft
    package). It only DRAFTS: it never approves and never sends. One preparation per dispute and data
    version; calling again with nothing new does nothing. Approval happens in the web UI."""
    dispute_id = _validated_id("prepare_draft", dispute_id)
    _audit("prepare_draft", dispute_id)
    db_path = config.db_path()
    client = _make_client()
    try:
        norm = normalize(_get_dispute(client, dispute_id), datetime.now(timezone.utc))
    except (PayPalError, RuntimeError, httpx.TransportError) as exc:
        return _paypal_error(exc)
    if not auto_prep.should_prepare(norm):
        return {"dispute_id": dispute_id, "prepared": False,
                "message": "This dispute is not waiting for your response (or is excluded from automatic "
                           "preparation), so no draft was prepared.",
                "review_url": _dispute_url(dispute_id), "note": APPROVAL_NOTE}
    version_hash = auto_prep.compute_version_hash(db_path, norm)
    claim_token = repo.claim_auto_prep(db_path, dispute_id, version_hash)
    if not claim_token:
        prep = repo.get_auto_prep(db_path, dispute_id)
        return {"dispute_id": dispute_id, "prepared": False, "auto_prep": prep and prep["status"],
                "message": "Already prepared for the current data (nothing new was uploaded or changed on "
                           "PayPal's side).", "review_url": _dispute_url(dispute_id), "note": APPROVAL_NOTE}
    repo.log_action(db_path, "system", "auto_prep_claimed", dispute_id,
                    {"version_hash": version_hash, "via": "mcp"})
    try:
        auto_prep.prepare_dispute(dispute_id, norm, client, db_path=db_path, version_hash=version_hash,
                                  claim_token=claim_token)
    except Exception as exc:  # noqa: BLE001 - mirrors routes_disputes._run_auto_prep's last line of defense
        repo.log_action(db_path, "system", "auto_prep_crashed", dispute_id, {"error": str(exc), "via": "mcp"})
        repo.set_auto_prep_result(db_path, dispute_id, version_hash, claim_token, "failed",
                                  detail=f"Unexpected error: {exc}")
    prep = repo.get_auto_prep(db_path, dispute_id) or {}
    # "ready" alone does not mean there is anything TO review yet - an auto_manual shell with zero
    # claims is "started", not "ready" (same distinction the inbox makes, copilot/web/templates/
    # disputes_list.html).
    has_claims = bool(repo.list_claims(db_path, prep["package_id"])) if prep.get("package_id") else False
    status = prep.get("status")
    if status == "ready" and not has_claims:
        message = "Draft started, but nothing has been entered yet. Add details in the web UI."
    else:
        messages = {"ready": "Draft ready. A human must review and approve it in the web UI.",
                    "needs_documents": "No documents or typed details yet, so there is nothing to draft "
                                       "from. Upload a document in the web UI first.",
                    "failed": "The preparation could not finish; see the detail."}
        message = messages.get(status, "Preparation status unknown.")
    return {"dispute_id": dispute_id, "prepared": status == "ready" and has_claims,
            "auto_prep": status, "detail": prep.get("detail"), "message": message,
            "review_url": (_package_url(dispute_id, prep["package_id"]) if prep.get("package_id")
                           else _dispute_url(dispute_id)), "note": APPROVAL_NOTE}


def get_send_status(dispute_id: str) -> dict:
    """Where the evidence response stands: the local send-attempt registry plus a fresh GET from PayPal.
    It says "received by PayPal" ONLY when PayPal's own record shows the content (and every attached
    file); otherwise it says not sent, pending, or rejected. Read-only. This server cannot approve."""
    dispute_id = _validated_id("get_send_status", dispute_id)
    _audit("get_send_status", dispute_id)
    client = _make_client()
    try:
        raw = _get_dispute(client, dispute_id)
    except (PayPalError, RuntimeError, httpx.TransportError) as exc:
        return _paypal_error(exc)
    norm = normalize(raw, datetime.now(timezone.utc))
    store = SQLiteAttemptStore(config.db_path())
    try:
        attempts = store.list_by_dispute(dispute_id)
    finally:
        store.close()
    out = []
    for att in attempts:
        req = att.get("request_input")
        verdict = (verify_after_with_attachments(raw, req, att.get("attachment_filenames") or [])
                   if req else {"content_ok": False, "files_ok": False, "missing_files": []})
        # only trust a content match as confirmation of an attempt THIS app actually POSTed
        # (request_id set) - same rule as copilot/core/send_service.py's claim-conflict branch.
        has_request_id = bool(att.get("request_id"))
        if verdict["content_ok"] and verdict["files_ok"] and has_request_id:
            text, status = "Received by PayPal (PayPal's own record shows it).", "received_by_paypal"
        elif verdict["content_ok"] and has_request_id:
            many = partial_message(verdict["missing_files"]) == PARTIAL_MESSAGE_MANY
            text, status = (f"PayPal shows the details but not the attached {'files' if many else 'file'}. "
                            f"Check the {'attachments' if many else 'attachment'} in the web UI.", "partial")
        elif att["state"] == "blocked":
            # a local refusal (no link yet, a precondition check) is not PayPal rejecting
            # anything - never shown as "failed"/"rejected".
            reason = send_service.seller_words(str(att.get("reason") or "blocked"),
                                               bool(norm.get("evidence_submitted_by_seller"))).rstrip(".")
            text, status = f"Not sent: {reason}.", "blocked"
        elif att["state"] == "failed":
            text, status = "PayPal rejected this evidence (confirmed). See the web UI for details.", "failed"
        elif att["state"] == "abandoned":
            # a human explicitly confirmed PayPal's Resolution Center did not show it - never
            # claims to know what actually happened to it.
            text, status = ("A vendor confirmed PayPal's Resolution Center did not show this attempt, and "
                            "chose to proceed with a different package.", "abandoned")
        elif att["state"] in ("sent_unconfirmed", "pending", "confirmed"):
            # the button that re-checks lives on the DISPUTE page (it calls recover_dispute on
            # every view) - visiting/refreshing it is "Check again"; there is no separate per-attempt URL.
            text, status = ("Sent, but PayPal's record does not show it yet. Result pending: visit "
                            f"{_dispute_url(dispute_id)} and use Check again - it only re-checks with "
                            "PayPal, never sends again.", "pending")
        else:
            text, status = "Not sent yet.", "not_sent"
        out.append({"status": status, "message": text, "registry_state": att["state"],
                    "attempted_at": att["ts"]})
    return {"dispute_id": dispute_id, "attempts": out,
            "overall": out[-1]["status"] if out else "not_sent",
            "overall_message": out[-1]["message"] if out else "Not sent yet.",
            "dispute_status_at_paypal": humanize_status(norm["status"]),
            "already_submitted_by_you": norm["evidence_submitted_by_seller"],
            "review_url": _dispute_url(dispute_id), "note": APPROVAL_NOTE}


def send_approved_package(dispute_id: str) -> dict:
    """Send the evidence package a human ALREADY approved in the web UI, and only if its approval
    fingerprint still matches the current content. Uses the web app's own send route (same guarantees:
    attempt registry, fresh GET, link validation, reconciliation, no automatic retry). This server cannot
    approve anything; an unapproved or edited package is refused. Writes to PayPal's sandbox."""
    dispute_id = _validated_id("send_approved_package", dispute_id)
    _audit("send_approved_package", dispute_id)
    db_path = config.db_path()
    target = None
    for package in reversed(repo.list_packages(db_path, dispute_id)):
        state, approval = _approval_state(db_path, dispute_id, package)
        if approval is not None:
            target = (package, state)
            break
        # A package PayPal already confirmed is answered by the shared send service (no POST), even when a
        # change after sending cleared its approval; the message is the same as the web page's.
        if send_service.confirmed_attempt_for(db_path, dispute_id, package["id"]) is not None:
            target = (package, "approved")
            break
    if target is None:
        return {"sent": False, "message": "Refused: no package for this dispute has been approved by a "
                "human; review_url shows what can be done.", "review_url": _dispute_url(dispute_id)}
    package, state = target
    if state != "approved":
        return {"sent": False, "message": "Refused: the package changed since it was approved; review_url shows what "
                "can be done.", "review_url": _package_url(dispute_id, package["id"])}

    # Calls the same plain-Python service the web route uses (copilot/core/send_service.py): identical
    # guarantees (approval-fingerprint match, attempt registry, fresh GET, reconciliation), no HTTP
    # request to mount and no CSRF token to forge - there is no HTTP request here to protect in the
    # first place, so the web route's CSRF check (which stays in routes_send.py) simply does not apply.
    client = _make_client()
    outcome = send_service.send_package(client, db_path, dispute_id, package["id"])
    message = send_service.seller_words(outcome.message, True)         # the refusal came from this call's fresh read
    if outcome.note:
        message += " " + outcome.note            # "Changed after sending: ...; nothing was re-sent."
    historical_confirmation = (outcome.http_status == 409 and outcome.title == 'Package already received')
    if not outcome.sent and not historical_confirmation:
        message += (" Not confirmed as received by PayPal. Nothing is re-sent automatically; use "
                    "get_send_status or Check again in the web UI.")
    result = {"http_status": outcome.http_status, "title": outcome.title, "message": message,
              "review_url": _package_url(dispute_id, package["id"]), "sent": outcome.sent}
    _audit("send_approved_package_result", dispute_id, http_status=outcome.http_status, title=outcome.title)
    return result


def review_case_snapshot(dispute_id: str, package_id: int, snapshot: str) -> dict:
    """Compare local package and source bytes only; never approve, send or read provider state."""
    dispute_id = _validated_id("review_case_snapshot", dispute_id)
    if config.feature_enabled("presentation_mode"):      # the tool is registered only with the flag; the import follows the same rule
        from copilot.features.presentation_mode.service import compare_snapshot
    else:
        raise ValueError("review_case_snapshot is not available: the presentation_mode feature is off.")
    result = compare_snapshot(config.db_path(), config.uploads_dir(), dispute_id, package_id, snapshot)
    _audit("review_case_snapshot", dispute_id, package_id=package_id, result=result["status"])
    return result


# ------------------------------------------------------------------------------------------------ server
def build_server() -> MCPServer:
    """Builds the server. The send tool is registered ONLY when COPILOT_MCP_ALLOW_SEND=true at this moment;
    no approve tool exists in any configuration."""
    server = MCPServer(SERVER_NAME, instructions=(
        "Helps a seller prepare PayPal dispute evidence. It can list disputes, show what is missing and "
        "prepare a DRAFT. It cannot approve anything: the seller approves in the web UI at " + BASE_URL))
    for fn in (list_disputes, get_dispute_file, what_is_missing, get_send_status):
        server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))(fn)
    # prepare_draft saves a draft package in Proofie's local database (nothing at PayPal): not read-only, not destructive,
    # and a repeat adds no second effect of a different kind.
    server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))(prepare_draft)
    if config.feature_enabled("presentation_mode"):
        server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))(review_case_snapshot)
    if send_enabled():
        server.tool(annotations=ToolAnnotations(readOnlyHint=False))(send_approved_package)
    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
