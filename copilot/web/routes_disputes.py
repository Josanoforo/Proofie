"""List and detail views: read-only GETs against the real PayPal sandbox via
copilot/core/disputes.py (pilot's SandboxClient/normalize/fetch_all_disputes, unchanged). PayPal is the
only source of truth for status/reason/amount/deadlines (docs/ARCHITECTURE.md): nothing here is cached
as if it were current beyond the request's own lifetime, and every page shows when it was checked.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from copilot import config
from copilot.web.dispute_state import buyer_closed_raw, BUYER_CLOSED_LABEL
from copilot.core import auto_prep, pool_scope
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.disputes import (SandboxClient, ScopedClient, fetch_all_disputes, humanize_deadline, humanize_evidence_type,
                                   humanize_reason, humanize_status, normalize)
from copilot.core.send_recovery import recover_dispute
from copilot.db import repo
from copilot.web import case_workspace
from copilot.web import ics, links, next_step, status, timefmt
from copilot.web.next_step import FixItem
from copilot.web.form_fields import date_words
from copilot.web import presentation
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, validate_dispute_id

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


class CredentialsMissing(Exception):
    """The seller has not put their own PayPal sandbox Client ID/Secret in .env yet (a fresh clone has none).
    Raised from get_client() so copilot/app.py can answer with a plain page that says what to do, instead of a
    bare "Internal Server Error" on the very first page a new user opens (found by a clean install)."""


def _is_placeholder(value: str) -> bool:
    """The shape of the .env.example values ("your-...-here"), decided by prefix and suffix only. The value is
    never printed or logged."""
    return value.startswith("your-") and value.endswith("-here")


def get_client() -> SandboxClient:
    # One short-lived client per request: simplest correct thing for a single local worker: no connection
    # pooling complexity, and SandboxClient itself is cheap to construct (loads .env, no network until a
    # call is made). A FastAPI dependency (not a bare helper) so tests can override it with a recorded/
    # mocked client via app.dependency_overrides, never touching the real sandbox by default.
    try:
        client = SandboxClient("copilot_web")
    except RuntimeError as exc:
        # decided by the condition itself - an empty Client ID or Secret in the environment - and not by the
        # wording of pilot/client.py's error. The client has already done its own loading of the project's .env by the
        # time it raises; this code never reads, creates or copies that file, it only looks at the two variables.
        if not os.environ.get("PAYPAL_CLIENT_ID") or not os.environ.get("PAYPAL_CLIENT_SECRET"):
            raise CredentialsMissing("PayPal sandbox credentials are not configured") from exc
        raise
    # An untouched copy of .env.example holds non-empty placeholders. Sent to PayPal they end in a 401 and a
    # page about rejected credentials. They count as missing, so the "credentials not set up" page appears. The client
    # has already loaded .env into the environment by now.
    if any(_is_placeholder(os.environ.get(name, "")) for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")):
        raise CredentialsMissing("PayPal sandbox credentials are still the .env.example placeholders")
    return ScopedClient(client) if pool_scope.active() else client      # hosted demo: only the judge pool's disputes


def path_dispute_id(request: Request) -> str | None:
    """The dispute id in a /disputes/{id}... path when it has PayPal's id shape (validate_dispute_id), else None. Used by the
    error and refusal pages for their breadcrumb and for the way back to the dispute."""
    import re
    from copilot.web.security import validate_dispute_id
    found = re.match(r"^/disputes/([^/]+)(?:/|$)", request.url.path)
    return found.group(1) if found and not validate_dispute_id(found.group(1)) else None


def refusal_page(request: Request, status: int, title: str, message: str, items: list | None = None,
                 back_url: str | None = None, back_label: str = "Back", conflict_form: dict | None = None):
    """A refusal on the main path (approve, upload, session expired) used to be a bare unstyled
    <p>. It renders through error.html now, with the same wording, the same status code and a way back. No PayPal
    read is claimed here: last_read stays empty and the page says nothing about it. The <title> of a POST refusal keeps
    the 'Error: ' prefix; the page's own h1 does not (error.html)."""
    if request.method == "POST" and not title.startswith("Error:"):
        title = "Error: " + title
    return templates.TemplateResponse(request, "error.html", {
        "state": "refused", "kind": "error", "title": title, "message": message, "items": items or [],
        "back_url": back_url, "back_label": back_label, "conflict_form": conflict_form, "last_read": None,
        "show_last_read": False, "retry_url": None, "dispute_id": path_dispute_id(request)}, status_code=status)


def csrf_expired_page(request: Request):
    """The 'session expired' page. Names cookies as a possible cause: with cookies blocked the check can
    never pass, so 'try again' alone would repeat forever. When the path holds a valid dispute id the page links back
    to that dispute (a GET), so the seller is never left without a way back."""
    dispute_id = path_dispute_id(request)
    return refusal_page(request, 403, "Your session expired",
                        "Your session expired before the form was sent. Nothing was sent. If it keeps happening, check that "
                        "your browser allows cookies for this site.",
                        back_url=f"/disputes/{dispute_id}" if dispute_id else None, back_label="Go back to the dispute")


def _checked_at_label() -> str:
    return timefmt.now_label()


def _status_text(raw: dict, norm: dict) -> str:
    return BUYER_CLOSED_LABEL if buyer_closed_raw(raw) else humanize_status(norm["status"])


# Stale data: when PayPal cannot be reached, the error page says when PayPal was last READ
# successfully in this process, so the vendor never takes an old screen for current. In memory on purpose:
# it is a display aid, never a source of truth for any decision (PayPal's own GET is).
# Keyed per dispute ("list" for the inbox) - a successful read of dispute A must never be
# shown as the last read of dispute B.
_LAST_PAYPAL_READ: dict = {}


def mark_paypal_read(key: str = "list") -> str:
    """Call after a successful PayPal read; returns the label so the page can show the same instant."""
    label = _checked_at_label()
    _LAST_PAYPAL_READ[key] = label
    return label


def last_paypal_read_label(key: str = "list") -> str | None:
    return _LAST_PAYPAL_READ.get(key)


class ReadProbe:
    """Wraps a client to record whether the PayPal GETs it made actually got an answer. recover_dispute()
    (copilot/core/send_recovery.py) deliberately swallows a failed GET and returns the attempt as still
    pending - safe, but it means "pending" can be reported without PayPal having been asked at all. This
    lets a page tell "we asked and it is still not there" apart from "we could not ask",
    without changing that function. It only forwards calls; it never adds one."""
    def __init__(self, client):
        self._client = client
        self.attempts = 0
        self.successes = 0
        # One entry per call, in call order (True = PayPal answered, False = it raised). recover_dispute() makes
        # exactly one GET per qualifying attempt, in list_by_dispute order, so a caller can map outcome -> attempt
        # by position; a test in tests/local pins that contract.
        self.outcomes: list[bool] = []

    def call(self, *args, **kwargs):
        self.attempts += 1
        try:
            result = self._client.call(*args, **kwargs)
        except Exception:
            self.outcomes.append(False)
            raise
        self.successes += 1
        self.outcomes.append(True)
        return result

    @property
    def unreachable(self) -> bool:
        return self.attempts > 0 and self.successes == 0


# ----------------------------------------------------------------------------- The dispute page
# Display only: every value below is read from PayPal's response, stored rows or the attempt registry. Nothing here
# permits or performs a write; the routes that write keep their own guards.
_REASON_SHORT = {"MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "item not received",
                 "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "item not as described",
                 "UNAUTHORISED": "unauthorized payment"}
PENDING_STATES = ("reserved", "attempted", "partial")
SENT_CONFIRMED_STATES = ("confirmed", "later_file")     # PayPal confirmed the statements (a file may have been added since)
DONE_KEYS = ("document_added", "document_excluded", "document_included")
DO_NOTHING_SENTENCE = ("If you do nothing, Proofie sends nothing. PayPal's help says sellers must respond within 10 "
                       "days of the dispute opening. If the buyer escalates without your response, PayPal resolves "
                       "it for the buyer (PayPal Help HELP1083). The \"Respond by\" date is the one PayPal gives "
                       "for this case.")
SAME_FILES_HELD_SENTENCE = "PayPal already has this exact package. Nothing to send."
_RC_WORDS = links.RESOLUTION_CENTER_LINK_TEXT.removeprefix("Open ")      # the words of SIGN_IN_SENTENCE that become the link
RESOLUTION_CENTER_SENTENCE = links.SIGN_IN_SENTENCE.partition(_RC_WORDS)   # (before, words, after): the link sits in the sentence
NOT_INR_SENTENCE = ("This dispute is not about a missing item. Proofie builds proof of shipment only, which might "
                    "not answer it. Answer this dispute in PayPal, or check every statement carefully.")


def _reason_short(norm: dict) -> str:
    return _REASON_SHORT.get(norm.get("reason"), "dispute")


def compact_case_header(norm, raw):
    """Display-only case identity from the read already used by the page."""
    now = datetime.now(timezone.utc)
    due = timefmt.due_display(norm, now)
    return {"norm": norm, "reason": _reason_short(norm),
            "status": status.dispute_status(norm, raw), "due": due}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _record_state(record) -> str:
    return status.effective_state(record)


def _package_info(db_path, dispute_id: str, package: dict) -> dict:
    """Everything the page shows about one package, from stored rows and the audit-mapped send record."""
    from copilot.web import routes_package
    claims = repo.list_claims(db_path, package["id"])
    attached = repo.list_attachments(db_path, package["id"])
    approval = repo.get_latest_valid_approval(db_path, package["id"])
    summary = routes_package._send_summary(db_path, dispute_id, package, claims, attached, approval)
    stale = bool(summary.get("approval_stale"))
    return {"package": package, "claims": claims, "attached": attached, "summary": summary,
            "approval_valid": approval is not None and not stale, "approval_stale": stale,
            "record": status.send_record(db_path, dispute_id, package),
            "has_tracking": bool(summary.get("tracking_number"))}


def _send_time(db_path, dispute_id: str, record):
    """When the send was made: the audit entry that registered the attempt, else the attempt's own stamp."""
    attempt = getattr(record, "attempt", None) or {}
    request_id = attempt.get("request_id")
    if request_id:
        for entry in repo.list_audit(db_path, dispute_id):
            if entry.get("action") == "send_attempt_registered" and (entry.get("detail") or {}).get("request_id") == request_id:
                return entry.get("ts")
    return attempt.get("ts")


def _missing_files(record) -> list:
    named = list(getattr(record, "missing_files", ()) or ())
    if named:
        return named
    reason = str(((getattr(record, "attempt", None) or {}).get("reason")) or "")
    if not reason.startswith(status.MISSING_FILE_PREFIX):
        return []
    return [name for name in reason[len(status.MISSING_FILE_PREFIX):].split(",") if name]


def _detail_summary(norm: dict, documents: list, packages: list, attempts: list, status_text: str | None = None,
                    checks=None, infos: dict | None = None, db_path=None) -> dict:
    """The seller-facing 'where things stand' groups. Everything is derived from PayPal's own read, the local rows and
    the attempt registry, never from model text. 'What was sent' reads the audit-mapped record of each package, so a
    later source change cannot flip it. Before the comparison is available the summary says it has not been checked."""
    infos = infos or {}
    dispute_id = norm.get("dispute_id")
    missing = []
    listed = next_step.seller_evidence_listed(norm)
    reviewing = bool(config.feature_enabled("outcome") and norm.get("stage") == "CHARGEBACK"
                     and norm.get("status") == "UNDER_REVIEW")
    if norm.get("can_provide_evidence") and not listed and not reviewing:
        if not documents:
            missing.append({"text": "No documents uploaded yet.", "href": f"/disputes/{dispute_id}/workspace/prepare#documents", "action": "Upload a document"})
        if not packages:
            missing.append({"text": "No evidence package yet.", "href": f"/disputes/{dispute_id}/workspace/prepare#package",
                            "action": "Generate or start a package"})
    unverifiable = [{"text": f"Proofie could not read {d['filename']}. Type the details yourself in the package.",
                     "href": f"/disputes/{dispute_id}/workspace/prepare#documents", "action": "See the document"}
                    for d in documents if not d["extraction_ok"] and not d["excluded"]]
    confirmed = [a for a in attempts if a["state"] == "confirmed"]
    conflicts = [c for c in checks or [] if c.state == "conflict"]
    latest = max(packages, key=lambda p: (p["version"], p["id"])) if packages else None
    latest_info = infos.get(latest["id"]) if latest else None
    latest_confirmed = bool(latest_info and _record_state(latest_info["record"]) in SENT_CONFIRMED_STATES)
    noun = "difference" if len(conflicts) == 1 else "differences"
    if checks is None:
        mismatch = ("Not checked yet: open a package to compare your documents with PayPal's data." if packages else
                    f"Not checked. {next_step.ONCE_PER_DISPUTE}" if listed else
                    "Not checked yet: start a package to compare your documents with PayPal's data.")
    elif not conflicts:
        mismatch = "No differences found in the fields checked just now."
    elif latest_confirmed:
        # only the latest package's own confirmed record makes this a note about a finished send.
        mismatch = (f"Checked after sending: {len(conflicts)} {noun} found in the fields checked just now. "
                    "Nothing was re-sent.")
    else:
        verb = "needs" if len(conflicts) == 1 else "need"
        mismatch = f"{len(conflicts)} {noun} {verb} review on the package page."
    sent = []                                    # what was sent, newest version first
    for package in sorted(packages, key=lambda p: (p["version"], p["id"]), reverse=True):
        info = infos.get(package["id"])
        if info is None and db_path is not None:
            info = infos[package["id"]] = _package_info(db_path, dispute_id, package)
        if info is None:
            continue
        state = _record_state(info["record"])
        n = package["version"]
        if state == "confirmed":
            counts = (f"{_plural(len(info['claims']), 'statement', 'statements')}, "
                      f"{_plural(len(info['record'].attachment_filenames), 'file', 'files')}")
            removed = next_step.removed_text(info["record"])
            sent_names = set(info["record"].attachment_filenames)
            left_out = [d for d in documents if not d["excluded"] and d["filename"] not in sent_names]
            not_part = (f" {_plural(len(left_out), 'uploaded document', 'uploaded documents')} "
                        f"{'was' if len(left_out) == 1 else 'were'} not part of this package." if left_out else "")
            sent.append({"state": "confirmed", "text": f"Package v{n} ({counts}), confirmed in PayPal's "
                         f"record at {timefmt.display(info['record'].confirmed_at) or 'a time Proofie did not record'}."
                         + not_part + (f" {removed}" if removed else "")})
        elif state == "later_file":
            removed = next_step.removed_text(info["record"])
            sent.append({"state": "confirmed", "text": f"Package v{n}, confirmed in PayPal's record at "
                         f"{timefmt.display(info['record'].confirmed_at) or 'a time Proofie did not record'}. "
                         f"{next_step.later_file_text(info['record'])}." + (f" {removed}" if removed else "")})
        elif state in PENDING_STATES:
            at = timefmt.display(_send_time(db_path, dispute_id, info["record"])) if db_path else ""
            # a true partial send says what PayPal lists and what it does not, never "not confirmed yet"
            tail = (f" {next_step.partial_missing_text(_missing_files(info['record']))}" if state == "partial"
                    else " PayPal has not confirmed it yet.")
            sent.append({"state": "pending", "text": f"Package v{n}: send attempted" + (f" at {at}" if at else "")
                         + "." + tail})
        elif latest is not None and package["id"] == latest["id"] and any(
                _record_state(i["record"]) in SENT_CONFIRMED_STATES for i in infos.values()):
            sent.append({"state": "not_sent", "text": f"Package v{n} (newer): Not sent. A confirmation covers only "
                                                     "the content sent then."})
    drift = None
    if latest_confirmed and conflicts:
        drift = (f"PayPal facts changed after you sent: {_plural(len(conflicts), 'difference', 'differences')}. "
                 + (next_step._tail(listed, next_step.can_provide(norm)) or "Nothing to do unless you prepare a new package."))
    return {"requested": [humanize_evidence_type(t) for t in (norm.get("evidence_requested_from_seller") or [])],
            "missing": missing, "unverifiable": unverifiable, "mismatch_note": mismatch, "sent": sent, "drift": drift,
            "confirmed": [{"at": a["ts"]} for a in confirmed]}


def _approve_href(base: str, package: dict | None, active_approval: bool) -> str:
    if not package:
        return base + "/workspace/prepare#package"
    pkg = f"{base}/packages/{package['id']}"
    return pkg + ("/active-approval" if active_approval else "#approval")


def _row(key, title, href, kind, label, status, mark="", hint="", blocker=None):
    """One task row. `blocker` = (text, link text, href): a blocked row is text with an inline link, never a link."""
    return {"key": key, "title": title, "href": href, "kind": kind, "label": label, "status": status, "mark": mark,
            "hint": hint, "blocked": status == "blocked", "blocker": blocker}


def _case_tasks(db_path, norm, documents, packages, attempts, infos=None):
    """The four guided steps (presentation.GUIDED_STEPS) from saved inputs and the audit-mapped send record; never
    grants a send permission. Done is for the sent version only; PayPal listing provide-evidence again reopens the
    required rows."""
    did = norm["dispute_id"]
    base = f"/disputes/{did}"
    steps = presentation.GUIDED_STEPS
    can_provide = bool(norm.get("can_provide_evidence"))
    latest = max(packages, key=lambda p: (p["version"], p["id"])) if packages else None
    info = None
    if latest:
        info = (infos or {}).get(latest["id"]) or _package_info(db_path, did, latest)
    record = info["record"] if info else None
    state = _record_state(record)
    if state == "later_file":
        state = "confirmed"                         # PayPal confirmed the send; a file added afterwards is not part of it
    listed = next_step.seller_evidence_listed(norm)
    reopened = state == "confirmed" and can_provide and not listed
    if reopened:
        state = "none"                              # PayPal asks for evidence again: the required rows reopen
    collision = (status.statement_collision(db_path, did, latest)
                 if latest and state == "none" and info and info["claims"] and info["has_tracking"] else None)
    pending_any = state in PENDING_STATES or (state == "none" and any(
        a["state"] in {"pending", "sent_unconfirmed"} for a in attempts))
    docs = [d for d in documents if not d["excluded"]]
    manual = repo.get_manual_fields(db_path, did)
    rows = []
    if docs:
        rows.append(_row("upload", steps[0], base + "/workspace/prepare#documents", "ok",
                         f"Done: {_plural(len(docs), 'document', 'documents')} added", "done"))
    elif manual:
        rows.append(_row("upload", steps[0], base + "/workspace/prepare#documents", "ok", "Done: typed details saved", "done"))
    else:
        rows.append(_row("upload", steps[0], base + "/workspace/prepare#documents", "todo", "Not started", "not_started",
                         hint="Or type the details yourself in the package."))
    n = latest["version"] if latest else None
    pkg_url = f"{base}/packages/{latest['id']}" if latest else None
    complete = bool(info and info["claims"] and info["has_tracking"])
    hint = "Includes the shipment details. Required to send."
    if reopened:
        # the sent version stays as it was; the open rows are about a NEW version.
        rows.append(_row("prepare", steps[1], base + "/workspace/prepare#package", "todo", f"Not started: a new version after v{n}",
                         "not_started", hint=hint))
    elif latest is None:
        if can_provide:
            rows.append(_row("prepare", steps[1], base + "/workspace/prepare#package", "todo", "Not started", "not_started", hint=hint))
        else:
            rows.append(_row("prepare", steps[1], base + "/workspace/prepare#package", "na", "Cannot start yet", "blocked", hint=hint,
                             blocker=("PayPal is not asking for evidence on this dispute now.", None, None)))
    elif complete:
        rows.append(_row("prepare", steps[1], pkg_url + "#next-step", "ok", f"Done: package v{n} prepared", "done",
                         hint=hint))
    else:
        rows.append(_row("prepare", steps[1], pkg_url + "#next-step", "info", f"In progress: package v{n}",
                         "in_progress", hint=hint))
    approve_href = _approve_href(base, latest, config.feature_enabled("active_approval"))
    if reopened:
        rows.append(_row("approve", steps[2], base + "/workspace/prepare#package", "na", "Cannot start yet", "blocked",
                         mark="Required to send",
                         blocker=("First", "prepare a new package version", base + "/workspace/prepare#package")))
    elif latest is None:
        rows.append(_row("approve", steps[2], base + "/workspace/prepare#package", "na", "Cannot start yet", "blocked",
                         mark="Required to send", blocker=("First", "prepare the evidence package", base + "/workspace/prepare#package")))
    elif not complete and state == "none":
        rows.append(_row("approve", steps[2], approve_href, "na", "Cannot start yet", "blocked", mark="Required to send",
                         blocker=("First", f"finish evidence package v{n}", pkg_url + "#next-step")))
    elif state == "confirmed":
        when = timefmt.display(_send_time(db_path, did, record))
        rows.append(_row("approve", steps[2], pkg_url, "ok",
                         "Done: approval used for the send" + (f" at {when}" if when else ""), "done",
                         mark="Required to send"))
    elif state in PENDING_STATES:
        rows.append(_row("approve", steps[2], pkg_url, "info", "Approved; outcome pending", "in_progress",
                         mark="Required to send"))
    elif info["approval_valid"]:
        rows.append(_row("approve", steps[2], pkg_url + "#next-step", "ok", f"Done: package v{n} approved", "done",
                         mark="Required to send"))
    else:
        rows.append(_row("approve", steps[2], approve_href, "todo", "Not started", "not_started",
                         mark="Required to send"))
    approve_blocker = ("First", steps[2][0].lower() + steps[2][1:], rows[2]["href"])
    if reopened:
        when = timefmt.display(record.confirmed_at)
        rows.append(_row("send", steps[3], pkg_url, "ok",
                         f"Done: package v{n} confirmed in PayPal's record" + (f" at {when}" if when else "")
                         + ". A new version needs its own send.", "done", mark="Required to send"))
    elif state == "confirmed":
        when = timefmt.display(record.confirmed_at)
        rows.append(_row("send", steps[3], pkg_url, "ok",
                         f"Done: package v{n} confirmed in PayPal's record" + (f" at {when}" if when else ""), "done",
                         mark="Required to send"))
    elif state in PENDING_STATES or pending_any or state in ("refused", "blocked", "abandoned"):
        view = status.evidence_status(record) if state != "none" else status.evidence_status(
            status.SendRecord(state="attempted"))
        rows.append(_row("send", steps[3], pkg_url or base, view.kind, view.label, "in_progress",
                         mark="Required to send"))
    elif not can_provide:
        rows.append(_row("send", steps[3], pkg_url or base + "/workspace/prepare#package", "na", "Cannot start yet", "blocked",
                         mark="Required to send",
                         blocker=("PayPal is not asking for evidence on this dispute now.", None, None)))
    elif collision is not None and collision.state == "confirmed":
        # PayPal held these statements from an earlier version when Proofie checked; the send would be refused
        # A collision also covers an earlier send with the SAME files (same_files); then PayPal holds the
        # exact package, not only its statements. Read with getattr so this page works before and after that change.
        held = (SAME_FILES_HELD_SENTENCE if getattr(collision, "same_files", False)
                else "When Proofie checked, PayPal held these statements from an earlier send.")
        if listed:
            rows.append(_row("send", steps[3], pkg_url, "na", "Not available", "blocked", mark="Required to send",
                             blocker=(f"{held} {next_step.ONCE_PER_DISPUTE}", links.RESOLUTION_CENTER_LINK_TEXT,
                                      links.RESOLUTION_CENTER_URL)))
        else:
            rows.append(_row("send", steps[3], pkg_url + "#statements", "na", "Change a statement", "blocked",
                             mark="Required to send",
                             blocker=(held, "Change a statement", pkg_url + "#statements")))
    elif latest is None or not complete or not info["approval_valid"]:
        rows.append(_row("send", steps[3], pkg_url or base + "/workspace/prepare#package", "na", "Cannot start yet", "blocked",
                         mark="Required to send", blocker=approve_blocker))
    else:
        rows.append(_row("send", steps[3], pkg_url + "#next-step", "todo", "Not started", "not_started",
                         mark="Required to send"))
    reviewing = bool(config.feature_enabled("outcome") and norm.get("stage") == "CHARGEBACK"
                     and norm.get("status") == "UNDER_REVIEW")
    if reviewing or not can_provide:
        # PayPal reviews the claim or asks for no evidence: no row that leads to a send says "Not started"
        why = (("PayPal is reviewing this claim.", None, None) if reviewing
               else ("PayPal is not asking for evidence on this dispute now.", None, None))
        for i in (1, 2, 3):
            if rows[i]["status"] == "not_started":
                row = rows[i]
                rows[i] = _row(row["key"], steps[i], row["href"], "na", "Cannot start yet", "blocked", mark=row["mark"],
                               hint=row["hint"], blocker=why)
    if listed and can_provide and state in ("none", "refused", "blocked", "abandoned"):
        # PayPal lists evidence from the seller: nothing new can be approved or sent, so the rows say the rule (the same
        # helper as the next step on this page and on the package page).
        rule = (next_step.ONCE_PER_DISPUTE, links.RESOLUTION_CENTER_LINK_TEXT, links.RESOLUTION_CENTER_URL)
        if latest is None:
            rows[1] = _row("prepare", steps[1], base + "/workspace/prepare#package", "na", "Not available", "blocked", hint=hint, blocker=rule)
        rows[2] = _row("approve", steps[2], approve_href, "na", "Not available", "blocked", mark="Required to send",
                       blocker=rule)
        if not (collision is not None and collision.state == "confirmed"):
            rows[3] = _row("send", steps[3], pkg_url or base + "/workspace/prepare#package", "na", "Not available", "blocked",
                           mark="Required to send", blocker=rule)
    for row in rows:
        row["word"] = {"done": "Done", "not_started": "Not started", "in_progress": "In progress",
                       "blocked": "Cannot start yet"}[row["status"]]
    return rows


def _done_banner(query, documents: list, dispute_id: str | None = None, sent_docs=(), can_change: bool = True):
    """The confirmation after an upload / leave-out / use-again, only when stored state matches the key. A leave-out
    banner also carries the undo (the existing include route, no new one) while the document can still be changed: never for
    a document PayPal holds, never on a page that offers no document changes."""
    key = query.get("done")
    raw_doc = query.get("doc", "")
    if key not in DONE_KEYS or not raw_doc.isdigit() or len(raw_doc) > 9:
        return None
    doc = next((d for d in documents if d["id"] == int(raw_doc)), None)      # documents of THIS dispute only
    if doc is None:
        return None
    if (key == "document_excluded") != bool(doc["excluded"]):
        return None
    text = presentation.done_message(key, filename=doc["filename"])
    if not text:
        return None
    undo = None
    if dispute_id and can_change and doc["id"] not in sent_docs:
        undo = presentation.done_undo(key, f"/disputes/{dispute_id}/documents/{doc['id']}/include", doc["filename"])
    return {"key": key, "text": text, "undo": undo}


def _auto_prep_status(db_path, dispute_id: str):
    row = repo.get_auto_prep(db_path, dispute_id)
    if row and row.get("status") == "preparing" and str(row.get("updated_at") or "") >= repo._cutoff(
            repo.AUTO_PREP_STALE_PREPARING_AFTER_SECONDS):
        return "preparing"
    return None


def _panel_step(*, dispute_id, norm, raw, documents, latest, info, still_pending, probe, checked_at, hosted,
                collision=None):
    """next_step.for_dispute plus the page's wording for the pending and confirmed states. The attempts that PayPal's answer
    left unconfirmed (`still_pending`) pause sending the way the package page's stored rows do."""
    from copilot.web import routes_package
    base = f"/disputes/{dispute_id}"
    record = info["record"] if info else None
    own_sha = ((record.attempt or {}).get("package_sha256") if record is not None else None)
    other_unconfirmed = bool(still_pending) and _record_state(record) not in PENDING_STATES
    earlier_partial = other_unconfirmed and routes_package._earlier_partial(still_pending, own_sha)
    step = next_step.for_dispute(
        dispute_id=dispute_id, norm=norm, raw=raw, has_documents=any(not d["excluded"] for d in documents),
        latest_package=latest, package_claims=len(info["claims"]) if info else 0,
        package_has_tracking=bool(info and info["has_tracking"]), approval_valid=bool(info and info["approval_valid"]),
        record=record, auto_prep_status=_auto_prep_status(config.db_path(), dispute_id), hosted=hosted,
        collision=collision, other_unconfirmed=other_unconfirmed, earlier_partial=earlier_partial)
    if step.key == "buyer_closed":
        return replace(step, data_state="buyer-closed")
    if step.key == "claim_under_review":
        return replace(step, data_state="claim-under-review")
    if step.key == "no_action":
        return replace(step, data_state="no-action-available")
    if step.key == "confirmed":
        when = timefmt.display(getattr(record, "confirmed_at", None))
        body = "Proofie confirmed your evidence in PayPal's record" + (f" at {when}." if when else ".")
        if norm.get("status") == "WAITING_FOR_BUYER_RESPONSE":
            body += " PayPal now waits for the buyer."
        return replace(step, body=body)
    if step.key not in ("pending", "other_pending"):
        return step
    stay = "Proofie will not send it again on its own."
    listed = next_step.seller_evidence_listed(norm)
    read_at = timefmt.display(checked_at)
    files = _missing_files(record)
    items = step.items
    if probe.unreachable:
        data_state = "recheck-failed"
        body = (f"PayPal did not answer at {read_at}. Proofie does not know yet whether PayPal has the evidence. {stay}")
    elif listed or step.key == "other_pending" and not next_step.can_provide(norm):
        return step                         # the step already says the rule, or that PayPal is not asking
    elif probe.attempts == 0:
        data_state = "recheck-not-possible"
        body = ("Proofie cannot tell whether this attempt reached PayPal. Check the dispute in PayPal's Resolution "
                "Center, then follow the steps on the package page.")
        if latest:
            items = (FixItem("The steps are on the package page:", "Why is sending blocked?",
                             f"{base}/packages/{latest['id']}#why-blocked"),)
    elif step.key == "other_pending":
        return step
    elif _record_state(record) == "partial" or files:
        data_state = "send-unconfirmed"
        body = (next_step.partial_text(files, labelled=True) if files
                else f"When Proofie checked, PayPal showed your statements without the file that was attached. {stay}")
    else:
        data_state = "send-unconfirmed"
        body = f"{stay} PayPal was read at {read_at}."
    return replace(step, body=body, data_state=data_state, items=items)


def _run_auto_prep(dispute_id: str, dispute_norm: dict, client: SandboxClient, version_hash: str,
                   claim_token: str) -> None:
    """The BackgroundTasks entry point - runs AFTER the inbox response is already sent, never blocking the
    page. Takes the SAME client the request used (reused, not reconstructed) so tests can override it via
    the usual get_client dependency - a client built fresh here with SandboxClient(...) directly could
    never be swapped for a fake, since BackgroundTasks scheduling sits outside FastAPI's Depends() system.
    prepare_dispute already records every expected failure into the auto_prep row/audit log; this wrapper
    is only a last line of defense so a truly unanticipated exception is never silently lost.

    `version_hash`/`claim_token` are the exact values list_disputes() already
    claimed with - passed straight through to repo.set_auto_prep_result() so a crash here can never
    clobber a newer (or reclaimed) claim's row."""
    db_path = config.db_path()
    try:
        auto_prep.prepare_dispute(dispute_id, dispute_norm, client, db_path=db_path,
                                  version_hash=version_hash, claim_token=claim_token)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        repo.log_action(db_path, "system", "auto_prep_crashed", dispute_id, {"error": str(exc)})
        try:
            repo.set_auto_prep_result(db_path, dispute_id, version_hash, claim_token, "failed",
                                      detail=f"Unexpected error: {exc}")
        except Exception:  # noqa: BLE001 - never let the last-resort handler itself crash the task
            pass


def _with_has_claims(db_path, prep: dict | None) -> dict | None:
    """The inbox needs to tell "ready with real content" apart from "ready, but still an empty
    auto_manual shell" - computed here, once, from the actual claims rather than threading extra state
    through claim_auto_prep/set_auto_prep_result (whether a package has claims can change later anyway,
    e.g. the vendor types something into it, so it must always be read fresh, not cached in auto_prep)."""
    if prep and prep.get("status") == "ready" and prep.get("package_id") is not None:
        prep = {**prep, "has_claims": bool(repo.list_claims(db_path, prep["package_id"]))}
    return prep


@router.get("/disputes", response_class=HTMLResponse)
def list_disputes(request: Request, background_tasks: BackgroundTasks,
                  client: SandboxClient = Depends(get_client)):
    items, pages = fetch_all_disputes(client)
    now = datetime.now(timezone.utc)
    db_path = config.db_path()
    rows = []
    for raw in items:
        norm = normalize(raw, now)
        prep = None
        # Schedule automatic background preparation for a new/changed WAITING_FOR_SELLER_
        # RESPONSE dispute (never a dispute listed in COPILOT_AUTOPREP_EXCLUDE). Idempotent -
        # claim_auto_prep only returns True once per (dispute_id, data version); this never blocks
        # rendering the inbox.
        # Hosted demo: only a pool case is ever prepared; the record case and anything else is read-only.
        if pool_scope.writable(norm.get("dispute_id")) and auto_prep.should_prepare(norm):
            version_hash = auto_prep.compute_version_hash(db_path, norm)
            claim_token = repo.claim_auto_prep(db_path, norm["dispute_id"], version_hash)
            if claim_token:
                # Audit the claim itself, not just its eventual outcome - without this, a claim
                # whose background task crashes before logging anything (and even auto_prep_crashed is
                # itself best-effort) would leave no trace that preparation was ever attempted at all.
                repo.log_action(db_path, "system", "auto_prep_claimed", norm["dispute_id"],
                                {"version_hash": version_hash})
                background_tasks.add_task(_run_auto_prep, norm["dispute_id"], norm, client, version_hash,
                                          claim_token)
                prep = {"status": "preparing"}
            else:
                prep = _with_has_claims(db_path, repo.get_auto_prep(db_path, norm["dispute_id"]))
        rows.append({"norm": norm, "raw": raw, "auto_prep": prep})
    account_cliff_line = None
    if config.feature_enabled("account_cliff"):
        try:
            # lazy: off by default; imported only when its flag is on
            from copilot.features import account_cliff
            account_cliff_line = account_cliff.inbox_line(client)
        except ImportError:
            account_cliff_line = None
    # one next step per case, the summary line and the order come from inbox_view (display only).
    from copilot.web import inbox_view
    urgency_fn = None
    if config.feature_enabled("deadlines"):
        try:
            # lazy: off by default; imported only when its flag is on
            from copilot.features.deadlines.service import urgency as urgency_fn
        except ImportError:
            urgency_fn = None
    view = inbox_view.build(rows, db_path, now, urgency=urgency_fn, detail_reader=client)
    exposure_context = None
    if config.feature_enabled("explainable_queue"):
        from copilot.features.explainable_queue.service import explain_cases, exposure_context as build_exposure_context
        webhook_rows = {}
        if config.feature_enabled("webhooks"):
            from copilot.features.webhooks import store as webhook_store
            for item in view["rows"]:
                did = item["norm"].get("dispute_id")
                if did:
                    webhook_rows[did] = webhook_store.events_for_dispute(did)
        queue_inputs = []
        for item in view["rows"]:
            did = item["norm"].get("dispute_id")
            packages_for_case = repo.list_packages(db_path, did) if did else []
            latest_for_case = max(packages_for_case, key=lambda p: (p["version"], p["id"])) if packages_for_case else None
            queue_inputs.append({**item, "documents": repo.list_documents(db_path, did) if did else [],
                                 "packages": packages_for_case,
                                 "claims": repo.list_claims(db_path, latest_for_case["id"]) if latest_for_case else [],
                                 "queue_status": item["status"].code,
                                 "status_label": item["status"].label,
                                 "events": webhook_rows.get(did, [])})
        explanations = explain_cases(queue_inputs, now.isoformat(), webhook_rows)
        exposure_context = (next(iter(explanations.values())).get("exposure_context")
                            if explanations else build_exposure_context(queue_inputs, now.isoformat()))
        for item in view["rows"]:
            item["queue_explanation"] = explanations.get(item["norm"].get("dispute_id"))
    selected_preparation_token = selected_preparation_csrf = None
    selected_preparation_cases = []
    if (config.feature_enabled("explainable_queue") and config.feature_enabled("local_autopilot")
            and config.feature_enabled("local_preparation") and config.feature_enabled("selected_preparation")):
        try:
            from copilot.features.local_autopilot import observations
            selected = [row for row in view["rows"] if pool_scope.writable(row["norm"].get("dispute_id"))][:observations.MAX_CASES]
            if selected:
                selected_preparation_token = observations.issue(db_path, [{"norm": row["norm"], "actions": []} for row in selected], observed_at=now.isoformat(), provenance="provider_read")
                selected_preparation_cases = [{"case_id": row["norm"]["dispute_id"]} for row in selected]
                selected_preparation_csrf = new_csrf_token()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            selected_preparation_token = selected_preparation_csrf = None
            selected_preparation_cases = []
    pool_scope_note = None
    if pool_scope.active() and not rows:
        pool_scope_note = pool_scope.POOL_EMPTY_REASON if pool_scope.pool_configured() else pool_scope.NO_POOL_REASON
    response = templates.TemplateResponse(request, "disputes_list.html",
                                          {"pool_scope_note": pool_scope_note, "pool_scope_link": pool_scope.pool_configured(), "selected_preparation_token": selected_preparation_token, "selected_preparation_csrf": selected_preparation_csrf,
                                           "selected_preparation_cases": selected_preparation_cases, "selected_preparation_scope": {"shown": len(selected_preparation_cases), "total": len(view["rows"])},
                                           "exposure_context": exposure_context, "rows": view["rows"], "summary": view["summary"],
                                           "needs_count": view["needs_count"], "pages": pages,
                                           "checked_at": mark_paypal_read("list"), "account_cliff": account_cliff_line,
                                           "sort_caption": inbox_view.SORT_CAPTION,
                                           "inaction": (inbox_view.INACTION_BEFORE, inbox_view.INACTION_LINK,
                                                        inbox_view.INACTION_AFTER, inbox_view.HELP1083_URL)})
    response.headers["Cache-Control"] = "no-store"
    if selected_preparation_csrf:
        set_csrf_cookie(response, selected_preparation_csrf)
    return response


def _paypal_timeline(raw, read_at, dispute_id):
    """PayPal's dated items for the dispute page, built from the one GET the page already made, plus the webhook notices
    already stored for this dispute when that feature is on (rows read from our own database, no PayPal call; only the
    verified ones become events). None with the flag off: the timeline package is then not even imported."""
    if config.feature_enabled("timeline"):
        from copilot.features.timeline import service as timeline
        notices = []
        if config.feature_enabled("webhooks"):
            from copilot.features.webhooks import store
            notices = timeline.webhook_events(store.events_for_dispute(dispute_id))
        return timeline.build(raw, read_at, verified=notices)
    return None


@router.get("/disputes/{dispute_id}", response_class=HTMLResponse)
def dispute_detail(request: Request, dispute_id: str, client: SandboxClient = Depends(get_client)):
    return _render_case(request, dispute_id, client, "overview")


@router.get("/disputes/{dispute_id}/workspace/{view}", response_class=HTMLResponse)
def dispute_workspace(request: Request, dispute_id: str, view: str, client: SandboxClient = Depends(get_client)):
    """One focused task view of the case (see case_workspace.VIEWS). Same single PayPal read as the overview."""
    if view not in case_workspace.VIEWS:
        bad_id = rejected_dispute_id(dispute_id)
        if bad_id:
            return bad_id
        return refusal_page(request, 404, "Page not found", "That part of the case does not exist.",
                            back_url=f"/disputes/{dispute_id}", back_label="Back to the case overview")
    return _render_case(request, dispute_id, client, view)


def _render_case(request: Request, dispute_id: str, client: SandboxClient, ws_view: str):
    # Validate dispute_id (untrusted path segment) before it reaches any PayPal call or HTML.
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    now = datetime.now(timezone.utc)
    norm = normalize(raw, now)
    checked_at = mark_paypal_read(dispute_id)
    db_path = config.db_path()
    documents = repo.list_documents(db_path, dispute_id)
    packages = repo.list_packages(db_path, dispute_id)

    # Reconcile any send attempt a crash/restart left 'sent_unconfirmed' before the vendor
    # takes any further action on this dispute (uploading, editing, approving, sending).
    store = SQLiteAttemptStore(db_path)
    try:
        # Detect a late-landing POST on a row the vendor had marked abandoned (recover_
        # dispute may promote it to 'confirmed' right here) by audit-entry id, rather than changing
        # recover_dispute's own return contract (every other caller ignores its return value already).
        audit_ids_before = {e["id"] for e in repo.list_audit(db_path, dispute_id)}
        probe = ReadProbe(client)
        still_pending = recover_dispute(probe, store, dispute_id, db_path)
        attempts = store.list_by_dispute(dispute_id)
    finally:
        store.close()
    late_confirmations = [e for e in repo.list_audit(db_path, dispute_id)
                          if e["id"] not in audit_ids_before
                          and e["action"] == "abandoned_attempt_later_confirmed_by_paypal"]

    csrf_token = new_csrf_token()
    status_text = _status_text(raw, norm)
    from copilot.web.routes_package import policy_source, _checks_for, _is_sent_version
    from copilot.core.checklist import UnknownCountryError
    checks = None
    if packages:
        try:
            checks, _ = _checks_for(norm, dispute_id, client, db_path)
        except UnknownCountryError:
            pass
    conflict = any(c.state == "conflict" for c in checks or [])
    verified = sum(1 for c in checks or [] if c.state == "verified")
    cannot_check = sum(1 for c in checks or [] if c.state == "present_unverifiable")
    total = sum(1 for c in checks or [] if c.state != "not_applicable")
    infos = {}
    for package in packages:
        info = infos[package["id"]] = _package_info(db_path, dispute_id, package)
        missing_required = not info["claims"] or not info["has_tracking"]
        view = status.package_status(info["record"], info["approval_valid"], conflict, missing_required)
        package["status_view"] = view
        package["checks_line"] = (f"{verified} of {total} {'check' if total == 1 else 'checks'} verified; "
                                  f"{cannot_check} cannot be checked."
                                  if checks is not None and total else None)    # same sentence as the package page
        package["source_words"] = "Written with AI (local model)" if package["source"] in ("ai", "auto_ai") \
            else "Written by you"
    latest = max(packages, key=lambda p: (p["version"], p["id"])) if packages else None
    latest_info = infos.get(latest["id"]) if latest else None
    # The sale checklist refuses a version that was already sent (409), so its link names a package only while the latest can change.
    readiness_package_id = str(latest["id"]) if latest and not _is_sent_version(db_path, dispute_id, latest) else ""
    hosted = bool(config.hosted_demo())
    # the latest version's statements may already be in PayPal from an earlier version that carried other files
    collision = (status.statement_collision(db_path, dispute_id, latest)
                 if latest_info and latest_info["record"].state == "none" else None)
    step = _panel_step(dispute_id=dispute_id, norm=norm, raw=raw, documents=documents, latest=latest,
                       info=latest_info, still_pending=still_pending, probe=probe, checked_at=checked_at, hosted=hosted,
                       collision=collision)
    rc3_reviewing = bool(config.feature_enabled("outcome") and norm.get("stage") == "CHARGEBACK"
                         and norm.get("status") == "UNDER_REVIEW")
    buyer_closed = buyer_closed_raw(raw)
    # Documents PayPal's record confirmed, by DOCUMENT (never by file name: two uploads can share one), from the
    # audit-mapped record of every package.
    # by the sha256 values the attempt carried, so a file that was removed from the package afterwards is still
    # shown as sent (PayPal holds it), and a file with the same name but other bytes is not.
    sent_docs = {}
    for info in infos.values():
        if info["record"].state == "confirmed":
            for document_id in info["record"].sent_document_ids:
                sent_docs.setdefault(document_id, info["record"].confirmed_at)
            for document in documents:
                if document["sha256"] in info["record"].sent_shas:
                    sent_docs.setdefault(document["id"], info["record"].confirmed_at)
            if not info["record"].sent_shas:
                # An attempt that predates the sha records knows its files by name only. A file it carried that
                # is detached now is matched to the dispute's documents by name, each name once (the by-name rule of
                # send_service.split_rows), so the table agrees with the summary line "was sent and is no longer attached".
                for name in info["record"].removed_files:
                    match = next((d for d in documents if d["filename"] == name and d["id"] not in sent_docs), None)
                    if match is not None:
                        sent_docs[match["id"]] = info["record"].confirmed_at
    outcome = raw.get("dispute_outcome") if isinstance(raw, dict) else None
    code = outcome.get("outcome_code") if isinstance(outcome, dict) else None
    decided = norm.get("status") == "RESOLVED" or (code is not None and code != "NONE")
    due = timefmt.due_display(norm, now)
    if due:
        due = {**due, "when": due["text"].removeprefix("Respond by ")}
    capture = next((c for c in (norm.get("seller_transaction_ids") or []) if isinstance(c, str)
                    and re.fullmatch(r"[A-Za-z0-9]{17}", c)), None)
    done = _done_banner(request.query_params, documents, dispute_id, sent_docs,
                        can_change=not rc3_reviewing and not buyer_closed)
    next_version = (latest["version"] + 1) if latest else None
    can_new_version = bool(latest and norm.get("can_provide_evidence") and not rc3_reviewing and not buyer_closed
                           and not next_step.seller_evidence_listed(norm))
    tasks = _case_tasks(db_path, norm, documents, packages, attempts, infos)
    queue_explanation = None
    if config.feature_enabled("explainable_queue"):
        from copilot.features.explainable_queue.service import explain_cases
        webhook_rows = {}
        if config.feature_enabled("webhooks"):
            from copilot.features.webhooks import store as webhook_store
            webhook_rows[dispute_id] = webhook_store.events_for_dispute(dispute_id)
        queue_action = next((t["title"] for t in tasks if not t["blocked"] and
                             t["status"] in {"not_started", "in_progress"}), None)
        if queue_action is None:
            queue_action = next((t["label"] for t in tasks if t["blocked"]), "No response task")
        queue_explanation = explain_cases([{"norm": norm, "raw": raw, "documents": documents,
                                            "packages": packages,
                                            "claims": repo.list_claims(db_path, latest["id"]) if latest else [],
                                            "queue_status": status.dispute_status(norm, raw).code,
                                            "status_label": status.dispute_status(norm, raw).label,
                                            "next_step": {"label": queue_action}}],
                                          now.isoformat(), webhook_rows).get(dispute_id)
    sale_readiness_view = None
    sale_readiness_error = None
    shipment_saved = False
    case_terms_saved = False
    if config.feature_enabled("sale_readiness") and capture:
        try:
            from copilot.features.sale_readiness import service as sale_readiness_service
            sale_terms = sale_readiness_service.load_case_terms(dispute_id, capture)
            latest_attachments = repo.list_attachments(db_path, latest["id"]) if latest else []
            saved_tracking = None
            if config.feature_enabled("fulfilment"):
                from copilot.features.fulfilment import store as fulfilment_store
                saved_file = fulfilment_store.get_file(capture)
            else:
                saved_file = None
            shipment_saved = bool(saved_file)
            case_terms_saved = sale_terms is not None
            if saved_file:
                try:
                    tracker = (json.loads(saved_file.get("request_json") or "{}").get("trackers") or [{}])[0]
                    saved_tracking = {"tracking_number": tracker.get("tracking_number"), "carrier": tracker.get("carrier")}
                except (TypeError, ValueError, IndexError):
                    saved_tracking = None
            sale_readiness_view = sale_readiness_service.case_view(dispute_id, capture, {}, documents,
                                                                    latest_attachments, sale_terms, saved_tracking)
        except (OSError, TypeError, ValueError) as exc:
            sale_readiness_error = str(exc)
    lesson_ready = False
    if config.feature_enabled("sale_lesson"):
        from copilot.features.sale_lesson.service import closed_outcome
        lesson_ready = closed_outcome(decided, code)
    seeded_case = False
    if (ws_view == "prepare" and config.feature_enabled("demo_documents_seeded") and not config.hosted_demo()
            and norm.get("can_provide_evidence") and not rc3_reviewing and not buyer_closed):     # one read, only where the link is drawn
        from copilot.features.demo_documents.seeded import offered as seeded_offered
        seeded_case = seeded_offered(client, norm)
    mcp_local_notes = []
    mcp_local_notes_error = None
    if config.feature_enabled("mcp_local_contracts"):
        try:
            from copilot.features.mcp_local_contracts.service import notes_for_case
            mcp_local_notes = notes_for_case(dispute_id)
        except (OSError, PermissionError, ValueError) as exc:
            mcp_local_notes_error = str(exc)
    local_autopilot_history = []
    local_autopilot_actions = []
    local_autopilot_evidence_count = 0
    local_autopilot_currency_supported = False
    local_autopilot_error = request.query_params.get("local_autopilot_error") == "1"
    local_autopilot_amount_error = request.query_params.get("local_autopilot_error") == "amount"
    local_autopilot_default_amount = ""
    if config.feature_enabled("local_autopilot"):
        try:
            from copilot.features.local_autopilot import service as autopilot_service
            from copilot.features.local_autopilot.routes import _store_path
            from copilot.features.resolution.order import allowed_actions as resolution_actions
            action_state = resolution_actions(norm)
            local_autopilot_actions = [name for name, available in
                                       (("offer", action_state.offer), ("message", action_state.message)) if available]
            local_autopilot_currency_supported = norm.get("currency") in autopilot_service.CENT_CURRENCIES
            from copilot.features.draft.facts import money_text
            from copilot.features.resolution.actions import cents_to_str
            local_autopilot_history = [{**row, "amount_text": money_text(row.get("amount_cents"), row.get("currency"))}
                                       for row in autopilot_service.load_inbox(_store_path())
                                       if row.get("case_id") == dispute_id]
            if type(norm.get("amount_cents")) is int and norm["amount_cents"] >= 0:   # the default offer is never above the disputed amount
                local_autopilot_default_amount = cents_to_str(norm["amount_cents"], norm.get("currency") or "USD")
            local_autopilot_evidence_count = sum(1 for row in documents if not row["excluded"] and
                row["extraction_ok"] and isinstance(row.get("extracted_text"), str) and row["extracted_text"].strip())
        except (OSError, PermissionError, ValueError) as exc:
            local_autopilot_error = True
    assistance_context = None
    if config.feature_enabled("local_autopilot"):
        try:
            from copilot.features.local_autopilot.assistance import case_context
            assistance_context = case_context(db_path, norm, local_autopilot_actions, now=now)
        except (OSError, ValueError, TypeError, KeyError):
            assistance_context = {"preparation": "unavailable", "recommendation": None,
                                  "reason": "Preparation data could not be read; continue manually.", "facts": []}
    ctx = {"assistance_context": assistance_context, "norm": norm, "status_text": status_text, "buyer_closed": buyer_closed, "rc3_reviewing": rc3_reviewing,
           "policy_source": policy_source(), "status_view": status.dispute_status(norm, raw),
           "reason_text": humanize_reason(norm["reason"]), "reason_short": _reason_short(norm),
           "due": due, "do_nothing": DO_NOTHING_SENTENCE, "not_inr_sentence": NOT_INR_SENTENCE,
           "decided": decided, "checked_at": checked_at, "step": step, "has_check_again": step.key in ("pending", "settling"),
           "summary": _detail_summary(norm, documents, packages, attempts, status_text=status_text, checks=checks,
                                      infos=infos, db_path=db_path),
           "tasks": tasks, "queue_explanation": queue_explanation,
           "sale_readiness_view": sale_readiness_view, "sale_readiness_error": sale_readiness_error,
           "shipment_saved": shipment_saved, "case_terms_saved": case_terms_saved, "lesson_ready": lesson_ready, "seeded_case": seeded_case,
           "mcp_local_notes": mcp_local_notes, "mcp_local_notes_error": mcp_local_notes_error,
           "local_autopilot_history": local_autopilot_history,
           "local_autopilot_actions": local_autopilot_actions,
           "local_autopilot_evidence_count": local_autopilot_evidence_count,
           "local_autopilot_currency_supported": local_autopilot_currency_supported,
           "local_autopilot_error": local_autopilot_error,
           "local_autopilot_amount_error": local_autopilot_amount_error,
           "local_autopilot_default_amount": local_autopilot_default_amount,
           "audit_timeline": repo.list_audit(db_path, dispute_id), "documents": documents, "packages": packages,
           "sent_docs": sent_docs, "pending_sends": still_pending, "late_confirmations": late_confirmations,
           "done": done, "capture": capture, "latest_package": latest, "readiness_package_id": readiness_package_id,
           "next_version": next_version,
           "can_new_version": can_new_version, "listed": next_step.seller_evidence_listed(norm),
           "once_per_dispute": next_step.ONCE_PER_DISPUTE, "csrf_token": csrf_token,
           "pending_new_version": step.key == "pending", "hosted": hosted, "due_calendar_link": ics.LINK_TEXT,
           "rc_sentence": RESOLUTION_CENTER_SENTENCE,
           "resolution_in_panel": bool(step.action and step.action.label == "Resolution options and buyer messages"),
           "paypal_timeline": _paypal_timeline(raw, now, dispute_id)}
    if config.feature_enabled("local_autopilot"):
        if config.feature_enabled("local_preparation"):
            try:
                from copilot.features.local_autopilot import observations
                ctx["preparation_observation_token"] = observations.issue(db_path, [{"norm": norm, "actions": local_autopilot_actions}], observed_at=now.isoformat(), provenance="provider_read")
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                ctx["preparation_observation_token"] = None
        if config.feature_enabled("gap_resolution") and latest and capture:
            try:
                from copilot.features.local_autopilot.gap_resolution import current_case
                ctx["gap_context"] = current_case(db_path, dispute_id, latest["id"], norm, capture_id=capture, checks=checks)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                ctx["gap_context"] = None
        if config.feature_enabled("case_reentry"):
            try:
                from copilot.features.case_reentry.projection import build as build_reentry
                ctx["case_reentry_view"] = build_reentry(case_id=dispute_id, tasks=tasks, packages=packages, audit_rows=ctx["audit_timeline"], package_states={pid: {"approval_valid": info.get("approval_valid")} for pid, info in infos.items()})
            except (OSError, ValueError, TypeError, KeyError):
                ctx["case_reentry_view"] = None
    ctx.update(_workspace_context(ws_view, dispute_id, ctx, packages, infos, checks, tasks, latest))
    response = templates.TemplateResponse(request, "dispute_detail.html" if ws_view == "overview" else "case_task.html", ctx)
    set_csrf_cookie(response, csrf_token)
    if pool_scope.active() and pool_scope.is_record(dispute_id) and not pool_scope.writable(dispute_id):
        _make_record_page_read_only(response, dispute_id)
    return response


def _make_record_page_read_only(response, dispute_id: str) -> None:
    """The hosted demo shows the record case to judges and never lets them act on it. The server already refuses every write on
    it (HostedPoolMiddleware, ScopedClient); this removes the controls so the page does not offer what would be refused: every
    form goes, links into the case's own task pages become plain text, and one sentence says why."""
    html = response.body.decode("utf-8")
    html = re.sub(r"<form\b.*?</form>", "", html, flags=re.S | re.I)
    html = re.sub(rf'<a\b[^>]*href="/disputes/{re.escape(dispute_id)}/[^"]*"[^>]*>(.*?)</a>', r"\1", html, flags=re.S | re.I)
    html = re.sub(r"(<main\b[^>]*>)", lambda m: m.group(1) + '<p class="action-note" data-state="record-case-read-only">'
                  + pool_scope.RECORD_NOTICE + "</p>", html, count=1)
    response.body = html.encode("utf-8")
    response.headers["content-length"] = str(len(response.body))


def _steps(rows):
    """[(label, done)] -> [{label, state}]: done steps are ticked, the first step not done is the current one."""
    out, current_set = [], False
    for label, done in rows:
        if done:
            out.append({"label": label, "state": "done"})
        elif not current_set:
            out.append({"label": label, "state": "current"})
            current_set = True
        else:
            out.append({"label": label, "state": "todo"})
    return out


def _task_steps(view, ctx, latest, latest_info):
    """The numbered steps of one task view, from data the page already has (documents, package, approval, send record)."""
    if view not in ("prepare", "review", "approve", "send"):
        return None
    info = latest_info or {}
    approved = bool(info.get("approval_valid"))
    state = _record_state(info["record"]) if info.get("record") is not None else "none"
    sent = state in SENT_CONFIRMED_STATES or state in PENDING_STATES
    if view == "prepare":
        return _steps([("Add your documents", bool(ctx.get("documents"))),
                       ("Enter the shipment details", bool(info.get("has_tracking"))),
                       ("Start or open the evidence package", bool(latest))])
    if view == "review":
        return _steps([("Read each statement beside its source", approved or sent),
                       ("Look at the checks and fix any difference", approved or sent)])
    if view == "approve":
        return _steps([("Check what will be sent", bool(info.get("summary", {}).get("sendable"))),
                       ("Confirm each statement and approve", approved or sent)])
    return _steps([("Send the approved package", sent), ("Read what PayPal confirmed", state in SENT_CONFIRMED_STATES)])


def _task_view_data(view, dispute_id, ctx, latest, latest_info, checks):
    """What the Review and Approve views show: each statement beside its source (the claims feature's own cards) and the
    exact content that would be sent. Display only: the approval, its guards and the send stay on their own routes."""
    data = {"review_cards": None, "approve_preview": None, "task_steps": _task_steps(view, ctx, latest, latest_info)}
    if not latest or not latest_info:
        return data
    if view == "review" and config.feature_enabled("claims") and checks is not None:
        from copilot.features.claims.model import safe_text
        from copilot.features.claims.service import compose, document_names
        try:
            claims = compose(config.db_path(), dispute_id, latest["id"], checks, ctx["norm"])
        except ValueError:
            return data
        data["review_cards"] = {"claims": claims, "safe_text": safe_text,
                                "document_names": document_names(config.db_path(), dispute_id, claims, redact_private=True)}
    if view == "approve":
        summary = latest_info["summary"]
        data["approve_preview"] = {"version": latest["version"], "claims": [c["text"] for c in latest_info["claims"]],
                                   "tracking": summary.get("tracking_number"), "carrier": summary.get("carrier"),
                                   "attachments": summary.get("attachments") or [], "sendable": summary.get("sendable")}
    return data


def _workspace_context(view, dispute_id, ctx, packages, infos, checks, tasks, latest):
    """What the left navigation and the task views add to the page context. Display only."""
    sale_view = ctx["sale_readiness_view"]
    judge_pages = []
    if view == "hackathon" and any(config.feature_enabled(flag) for flag in case_workspace.JUDGE_FLAGS):
        from copilot.features.local_demo_hub import service as hub
        judge_pages = [p for p in hub.page_entries(dispute_id, latest["id"] if latest else None) if p["enabled"]]
        if config.feature_enabled("judge_as_buyer"):
            judge_pages.append({"title": "Judge as buyer", "href": "/features/judge_as_buyer",
                                "description": "Walk the buyer's side of a case."})
    latest_info = infos.get(latest["id"]) if latest else None
    task_data = _task_view_data(view, dispute_id, ctx, latest, latest_info, checks)
    return {**task_data, "view": view, "view_title": case_workspace.VIEW_TITLES[view],
            "ws_url": lambda target: case_workspace.view_url(dispute_id, target),
            "nav": case_workspace.nav_items(dispute_id, view, tasks, latest, ctx["capture"]),
            "sale_readiness_words": case_workspace.sale_readiness_words(sale_view),
            "judge_pages": judge_pages,
            "claims_count": len(latest_info["claims"]) if latest_info else 0,
            "has_conflict": any(c.state == "conflict" for c in checks or [])}


@router.get("/disputes/{dispute_id}/due.ics")
def dispute_due_calendar(request: Request, dispute_id: str, client: SandboxClient = Depends(get_client)):
    """PayPal's own response due date as a calendar file. The same single read the dispute page
    makes, nothing written; no due date from PayPal gives a refusal page, never a made-up one."""
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    now = datetime.now(timezone.utc)
    due = timefmt.due_display(normalize(raw, now), now)
    if not due:
        return refusal_page(request, 404, ics.REFUSAL_TITLE, ics.REFUSAL_MESSAGE, back_url=f"/disputes/{dispute_id}",
                            back_label="Go back to the dispute")
    try:
        name = ics.file_name(dispute_id)
    except ValueError:
        return refusal_page(request, 400, "Check the dispute reference", "That dispute reference is not valid. Open the dispute from All disputes.",
                            back_url="/disputes", back_label="All disputes")
    body = ics.due_calendar(dispute_id, datetime.fromisoformat(due["iso"]), now)
    return Response(body, media_type=ics.MEDIA_TYPE, headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
