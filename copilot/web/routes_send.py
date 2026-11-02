"""Send route. This IS the real send path - the same code that would execute a real POST
to PayPal's sandbox - built and tested exclusively against fake clients in this build. By project policy this
route is never invoked against a real dispute's real SandboxClient without the repository owner's prior, explicit,
per-content approval given in the development chat (not the in-app Approve step); nothing in the code itself withholds that call once approval exists - the rule is
procedural, not a missing line of code.

The actual send mechanics (approval-fingerprint match, attempt registry, fresh-GET validation,
reconciliation, the attachment-landed check) live in copilot/core/send_service.py (moved there 2026-10-05
so copilot/mcp_server.py's send tool can call the exact same logic directly, without mounting this HTTP
route through a TestClient). This module is now only the HTTP boundary: CSRF verification, the FastAPI
dependency wiring, and rendering a SendOutcome as an HTMLResponse the way this route has always looked.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.disputes import SandboxClient, humanize_evidence_type
from copilot.core.send import build_request_input, pick_tracking, sha256_json
from copilot.core.send_recovery import recover_dispute
from copilot.db import repo
from copilot.core.send_recovery import PARTIAL_TITLES, PENDING_MESSAGE, RECEIVED_MESSAGE, partial_message
from copilot.web.routes_disputes import ReadProbe, csrf_expired_page, get_client, last_paypal_read_label, templates, refusal_page
from copilot.web import next_step, send_results, status, timefmt
from copilot.web import status as status_view
from copilot.web.routes_package import send_state_for_record
from copilot.web.security import CSRF_COOKIE_NAME, new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()

MAX_PACKAGE_ID = 2 ** 31 - 1   # a package id is a small database row id; anything outside 1..this cannot name a package


def _package_not_found(request: Request, dispute_id: str):
    """The 404 for a package id no package can have (zero, negative or beyond the database's range): answered before the
    id reaches a query, where an out-of-range integer would raise instead of finding nothing."""
    return refusal_page(request, 404, "Package not found", "That package could not be found for this dispute.",
                        back_url=f"/disputes/{dispute_id}")


class _SnapshotStore:
    """A thin proxy over the attempt store whose `list_by_dispute` returns the snapshot the route already
    read. recover_dispute() lists the attempts once and makes one GET per qualifying attempt in that order, so handing
    it the route's own snapshot makes the pairing GET -> attempt true by construction (an attempt that appears in the
    registry between the route's read and recover_dispute's read can no longer shift it). Everything else (write,
    read, ...) is forwarded untouched; copilot/core/send_recovery.py is not modified."""

    def __init__(self, store, snapshot: list):
        self._store, self._snapshot = store, snapshot

    def list_by_dispute(self, dispute_id: str) -> list:
        return [dict(a) for a in self._snapshot]

    def __getattr__(self, name):
        return getattr(self._store, name)


def _result_state(title: str, message: str, status: int) -> tuple[str, str]:
    """(kind, data-state) for a result page. Uncertain is its own state - amber, not an error and not a
    success - and the only ones that offer Check again are uncertain and partial. The
    mapping reads the outcome the send service already decided (title/status), never PayPal text."""
    if title == RECEIVED_MESSAGE or (status == 409 and title == "Package already received"):
        # The send service preserves a previously confirmed row; this is not a new send or an unknown result.
        return "ok", "confirmed"
    if title in PARTIAL_TITLES:
        return "warn", "partial"
    if title in (PENDING_MESSAGE, "Result pending — check again", "Outcome pending") or status == 202:
        return "warn", "result-uncertain"
    if status in (502, 503) and "could not be reached" in message:
        return "error", "paypal-unreachable"
    if status == 502 and "PayPal rejected" in message:
        return "error", "rejected"          # PayPal's own refusal (a confirmed, known rejection)
    if status == 409 and "marked as not received" in message:
        return "error", "abandoned"
    if status == 409 and message == send_service.SETTLING_MESSAGE:
        return "warn", "settling"           # a wait, not a refusal: PayPal is still updating the dispute
    if status >= 400:
        # A refusal made HERE (not approved, approval out of date, no link, another send in
        # flight...) is not PayPal rejecting anything. All blocked states show red, with that distinction in words.
        return "error", "blocked"
    return "info", "info"


# Result state -> the attempt state the shared status vocabulary has a seller label for (copilot/web/status.py).
_STATUS_CODE = {"confirmed": "confirmed", "recovered": "confirmed", "partial": "partial", "result-uncertain": "attempted",
                "cannot-check": "attempted", "earlier-version-pending": "attempted", "send-in-progress": "reserved",
                "rejected": "refused", "blocked": "blocked", "abandoned": "abandoned", "later-file": "later_file"}
UNCERTAIN_TITLES = (PENDING_MESSAGE, "Result pending — check again", "Outcome pending")
UNCERTAIN_HEADLINE = "Send attempted - not confirmed by PayPal"
UNCERTAIN_BODY = ("Proofie asked PayPal three times and did not get a confirmation yet. "
                  "Proofie will not send it again on its own.")


def _resolve(title: str, message: str, dispute_id: str, package_id: int, status_code: int = 200,
             technical: str | None = None, kind: str | None = None, state: str | None = None,
             last_read: str | None = None, retry_check: bool = False, note: str | None = None,
             listed: bool | None = None) -> dict:
    """Title/message come from SendOutcome (built from PayPal response text, exception messages, and
    attachment filenames - none of it trusted); technical is raw PayPal/exception detail. This decides what the page
    will say (headline, state, kind) and returns it as a plain record: the POST routes store it (send_results) and the
    result page only renders it. No PayPal call and no write happens here."""
    if kind is None or state is None:
        kind, state = _result_state(title, message, status_code)
    if state == "confirmed" and status_code == 200 and title == RECEIVED_MESSAGE:
        title = ("PayPal already has this evidence" if message.startswith("PayPal already has this exact content")
                 else "Evidence sent to PayPal")
    # the send path's own precondition text, in the seller's words. `listed` is what the caller's own fresh read said
    # (True only from the send route, which just read PayPal's list); a stored banner or a refusal made without a read
    # (None) only says what happened when Proofie last tried.
    message = send_service.seller_words(message, listed)
    if title in UNCERTAIN_TITLES and state == "result-uncertain":
        if title == PENDING_MESSAGE:
            message = UNCERTAIN_BODY
        title = UNCERTAIN_HEADLINE
        if last_read is None:
            last_read = last_paypal_read_label(dispute_id)
    return {"http_status": status_code, "title": timefmt.localize_text(title),
            "message": timefmt.localize_text(message), "technical": technical, "kind": kind, "state": state,
            "note": timefmt.localize_text(note) if note else None, "retry_check": bool(retry_check),
            "last_read": last_read}


def _render_record(record: dict, dispute_id: str, package_id: int, csrf_token: str | None = None) -> HTMLResponse:
    """The result page for one stored record. Reads the attempt registry for the read-back time of a confirmed send;
    never calls PayPal and never writes. Carries its CSRF token because the pages that offer Check again embed that
    POST form: the caller's cookie token is reused (rotating it here would make the previous page's forms fail with 403
    after the browser's Back); a fresh one is issued only when the request carried none."""
    db = config.db_path()
    package = repo.get_package(db, package_id)
    if package and package["dispute_id"] != dispute_id:
        package = None
    state = record["state"]
    # The time stored with the outcome. An outcome saved before that column existed has none: it is read from
    # the package's own send record (audit-mapped, so a later edit of the shared shipment details cannot lose it).
    confirmed_at = record.get("confirmed_at")
    readback = None
    if state in ("confirmed", "recovered") and package:
        sent = status.send_record(db, dispute_id, package)
        if confirmed_at is None:
            confirmed_at = sent.confirmed_at
        # The documents PayPal listed when the send was confirmed, kept with the attempt (a registry read, never PayPal).
        if sent.readback:
            readback = {**sent.readback, "evidence_label": humanize_evidence_type(sent.readback.get("evidence_type"))}
    label = None
    code = _STATUS_CODE.get(state)
    if code:
        view = status.evidence_status(status.SendRecord(state=code))
        label, label_kind = view.label, view.kind
    issue_cookie = csrf_token is None
    if issue_cookie:
        csrf_token = new_csrf_token()
    body = templates.env.get_template("result.html").render(
        title=record["title"], message=record["message"], dispute_id=dispute_id, package_id=package_id,
        package=package, technical=record["technical"], kind=record["kind"], state=state, csrf_token=csrf_token,
        last_read=record["last_read"], retry_check=record["retry_check"], confirmed_at=confirmed_at,
        readback=readback, note=record["note"], status_label=label, status_kind=label_kind if code else "info")
    response = HTMLResponse(body, status_code=record["http_status"])
    response.headers["Cache-Control"] = "no-store"
    if issue_cookie:
        set_csrf_cookie(response, csrf_token)
    return response


def _page(title: str, message: str, dispute_id: str, package_id: int, status: int = 200,
          technical: str | None = None, kind: str | None = None, state: str | None = None,
          csrf_token: str | None = None, last_read: str | None = None,
          retry_check: bool = False, note: str | None = None, listed: bool | None = None) -> HTMLResponse:
    """The result page rendered directly (tests, and a caller that must not store the outcome). The send, the check and
    the confirm-not-received routes answer through _stored() instead: post, redirect, get."""
    record = _resolve(title, message, dispute_id, package_id, status, technical, kind, state, last_read,
                      retry_check, note, listed)
    return _render_record(record, dispute_id, package_id, csrf_token)


def _stored(title: str, message: str, dispute_id: str, package_id: int, status: int = 200,
            technical: str | None = None, kind: str | None = None, state: str | None = None,
            csrf_token: str | None = None, last_read: str | None = None,
            retry_check: bool = False, note: str | None = None, listed: bool | None = None) -> RedirectResponse:
    """Keep this outcome and answer 303 to the page that shows it. csrf_token is accepted so the callers keep
    their signature; the result page takes the token from the browser's own cookie."""
    record = _resolve(title, message, dispute_id, package_id, status, technical, kind, state, last_read,
                      retry_check, note, listed)
    if record["state"] in ("confirmed", "recovered"):
        package = repo.get_package(config.db_path(), package_id)
        if package and package["dispute_id"] == dispute_id:
            record["confirmed_at"] = status_view.send_record(config.db_path(), dispute_id, package).confirmed_at
    rid = send_results.save(config.db_path(), dispute_id, package_id, record)
    return RedirectResponse(f"/disputes/{dispute_id}/packages/{package_id}/result/{rid}", status_code=303)


@router.get("/disputes/{dispute_id}/packages/{package_id}/result/{rid}", response_class=HTMLResponse)
def show_result(request: Request, dispute_id: str, package_id: int, rid: str):
    """Shows one stored outcome of a send or a check. Read only: no PayPal call, no write. An id that is unknown, or
    that belongs to another dispute or package, gets the same neutral page with a way back to the package."""
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    record = (send_results.load(config.db_path(), rid, dispute_id, package_id)
              if 1 <= package_id <= MAX_PACKAGE_ID else None)
    if record is None:
        response = refusal_page(request, 404, "Result not found",
                                "There is no stored result at this address. Nothing was changed and nothing was sent.",
                                back_url=f"/disputes/{dispute_id}/packages/{package_id}",
                                back_label="Open the evidence package")
        response.headers["Cache-Control"] = "no-store"
        return response
    return _render_record(record, dispute_id, package_id, request.cookies.get(CSRF_COOKIE_NAME))


@router.post("/disputes/{dispute_id}/packages/{package_id}/send", response_class=HTMLResponse)
def send_package(request: Request, dispute_id: str, package_id: int, csrf_token: str = Form(...),
                 client: SandboxClient = Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    # validate dispute_id (untrusted path segment) BEFORE it reaches a PayPal GET/POST path or the
    # HTML below - the same expression copilot/mcp_server.py validates its own dispute_id argument against.
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    if not 1 <= package_id <= MAX_PACKAGE_ID:
        return _package_not_found(request, dispute_id)
    outcome = send_service.send_package(client, config.db_path(), dispute_id, package_id)
    return _stored(outcome.title, outcome.message, dispute_id, package_id, outcome.http_status,
                   technical=outcome.technical, note=outcome.note, listed=True)


@router.post("/disputes/{dispute_id}/packages/{package_id}/check", response_class=HTMLResponse)
def check_package(request: Request, dispute_id: str, package_id: int, csrf_token: str = Form(...),
                  client: SandboxClient = Depends(get_client)):
    """'Check again' - re-verifies with PayPal via GET only, exactly what recover_dispute() already
    does on every ordinary dispute-page view (copilot/web/routes_disputes.py); this route exists so the
    package-review screen has its own explicit button for it, right next to the banner it updates, without
    the vendor having to navigate back to the dispute page first. Never calls send_service.send_package -
    there is no code path here that can ever POST to PayPal."""
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    if not 1 <= package_id <= MAX_PACKAGE_ID:
        return _package_not_found(request, dispute_id)
    db_path = config.db_path()
    # package_id is a path segment independent of dispute_id - never trust it belongs
    # to this dispute without checking (same reasoning as confirm-not-received's own check).
    package = repo.get_package(db_path, package_id)
    if package is None or package["dispute_id"] != dispute_id:
        return refusal_page(request,404,"Package not found","That package could not be found for this dispute.",back_url=f"/disputes/{dispute_id}")
    # What this page may claim is decided PER ATTEMPT. The attempts that belong to this package are its
    # current content's attempt and the attempts of its earlier versions (recognised through the audit entry the
    # send path writes when it registers an attempt); another package's attempts never speak for this one.
    # Which attempt speaks for THIS package is decided through the audit log: the attempt of the current
    # content counts only when it is this package's (another package's attempt with the same statements never does),
    # and a confirmed attempt the audit maps to this package still counts after a shared edit changed the content hash.
    registered = {e["detail"].get("request_id"): e["detail"].get("package_id")
                  for e in repo.list_audit(db_path, dispute_id) if e["action"] == "send_attempt_registered"}
    sha = send_service.content_sha(db_path, dispute_id, package_id)
    confirmed_own = status_view.send_record(db_path, dispute_id, package).attempt
    if confirmed_own is not None and confirmed_own["state"] == "confirmed":
        sha = confirmed_own["package_sha256"]

    def mine(attempt) -> bool:
        owner = registered.get(attempt.get("request_id"), package_id)
        return owner == package_id

    store = SQLiteAttemptStore(db_path)
    probe = ReadProbe(client)
    try:
        attempts_before = store.list_by_dispute(dispute_id)
        recover_dispute(probe, _SnapshotStore(store, attempts_before), dispute_id, db_path)
        attempts_after = {a["package_sha256"]: a for a in store.list_by_dispute(dispute_id)}
    finally:
        store.close()
    before = next((a for a in attempts_before if a["package_sha256"] == sha and mine(a)), None) if sha else None
    before_state = before["state"] if before else None
    after = attempts_after.get(sha) if before else None
    # recover_dispute() asks PayPal about an attempt that is 'sent_unconfirmed' or 'abandoned' WITH a stored
    # request, one GET each, in this same order: so the i-th GET belongs to the i-th such attempt.
    qualifying = [a["package_sha256"] for a in attempts_before
                  if a["state"] in ("sent_unconfirmed", "abandoned") and a.get("request_input") is not None]
    if len(probe.outcomes) == len(qualifying):
        answered = dict(zip(qualifying, probe.outcomes))
    else:                       # the pairing does not hold: never guess - NO qualifying attempt counts as answered,
        answered = {q: False for q in qualifying}                  # which the page reports as "Could not check"
    asked_about_this = before is not None and sha in qualifying
    this_answered = answered.get(sha, False)
    csrf = request.cookies.get(CSRF_COOKIE_NAME)
    earlier = [a for a in attempts_before
               if a["package_sha256"] != sha and a["state"] in ("sent_unconfirmed", "pending")
               and a.get("request_id") and registered.get(a["request_id"]) == package_id
               and attempts_after.get(a["package_sha256"], {}).get("state") in ("sent_unconfirmed", "pending")]

    def could_not_check():
        # recover_dispute() swallows a failed GET and leaves the attempt "pending" - reporting that
        # as the answer would claim a check that never happened. Say Proofie could not ask; still GET-only.
        return _stored("Could not check", "Proofie could not check PayPal just now, so whether PayPal has this "
                     "evidence is still not known. Nothing was changed and nothing was sent.",
                     dispute_id, package_id, 502, kind="error", state="paypal-unreachable",
                     csrf_token=csrf, last_read=last_paypal_read_label(dispute_id), retry_check=True)

    if asked_about_this and not this_answered:
        return could_not_check()
    claims = repo.list_claims(db_path, package_id)
    approval = repo.get_latest_valid_approval(db_path, package_id)
    status = send_service.send_status_banner(db_path, dispute_id, package, claims,
                                             approved=approval is not None) or "Not sent yet"
    # The kind of the "nothing pending" box comes from this package's stored send record, the same mapping the package
    # page draws (send_state_for_record); the banner words above are shown, never parsed.
    checked = send_state_for_record(status_view.send_record(db_path, dispute_id, package), approval is not None)
    # a confirmed record whose files are not the files attached now is worded from the same
    # effective state every page uses: PayPal has the statements, not the file added afterwards. Never "Received".
    record_now = status_view.send_record(db_path, dispute_id, package)
    later_file = status_view.effective_state(record_now) == "later_file"
    removed = next_step.removed_text(record_now)

    def later_file_result():
        n = package.get("version")
        text = (f"PayPal's record shows package v{n}. {next_step.later_file_text(record_now)}. "
                f"{next_step.will_not_send_text(record_now)}" + (f" {removed}" if removed else "")
                + " Nothing was sent now.")
        return _stored("Confirmed in PayPal; a file was added afterwards", text, dispute_id, package_id, 200,
                       kind="warn", state="later-file", csrf_token=csrf)

    if asked_about_this:
        # A GET for this content ran and PayPal answered: the post-GET titles are earned here and only here.
        if after is not None and after["state"] == "confirmed":
            if later_file:
                return later_file_result()
            return _stored("Received by PayPal", status + (f" {removed}" if removed else ""), dispute_id, package_id,
                           200, kind="ok", state="recovered", csrf_token=csrf)
        if record_now.state == "partial":
            # PayPal lists the statements and not every file: the same partial answer every page gives
            names = next_step.partial_names(record_now)
            return _stored(partial_message(names), next_step.partial_text(names), dispute_id, package_id, 200,
                           kind="warn", state="partial", csrf_token=csrf)
        return _stored("Still not confirmed by PayPal", status, dispute_id, package_id, 200, kind="warn",
                     state="result-uncertain", csrf_token=csrf)
    if before_state == "pending":
        # The POST is still in flight (or its owner crashed): nothing was compared with PayPal's record.
        return _stored("A send is in progress", "A send is in progress; wait and check again. "
                     "Nothing was compared with PayPal's record just now.", dispute_id, package_id, 200,
                     kind="warn", state="send-in-progress", csrf_token=csrf)
    if before_state == "sent_unconfirmed":
        # no stored request to compare, so no GET could be made for this attempt.
        return _stored("Nothing to compare with PayPal", "No check against PayPal could be made: the earlier "
                     "attempt has no stored request to compare with PayPal's record, so its result is "
                     "still not known. " + status, dispute_id, package_id, 200, kind="warn",
                     state="cannot-check", csrf_token=csrf)
    if before_state == "confirmed":
        # Settled earlier by a PayPal GET; this click asked PayPal nothing new, so no "just checked" wording.
        if later_file:
            return later_file_result()
        return _stored("Already recorded as received", status + (f" {removed}" if removed else ""), dispute_id,
                       package_id, 200, kind="ok", state="confirmed", csrf_token=csrf)
    # The same statements (and files) were sent under ANOTHER package: this package has no attempt of its own, but its
    # content hash has a registry row. "Check again" on the page's collision step asks PayPal about that row and answers
    # from it, never "Nothing pending".
    twin = next((a for a in attempts_before if sha and a["package_sha256"] == sha and not mine(a)
                 and a["state"] in ("confirmed", "sent_unconfirmed")), None)
    if twin is not None:
        owner = repo.get_package(db_path, registered.get(twin.get("request_id")) or 0)
        where = f"package v{owner['version']}" if owner else "an earlier version"
        twin_after = attempts_after.get(sha) or twin
        if twin["state"] == "sent_unconfirmed" and twin.get("request_input") is not None and not answered.get(sha, False):
            return could_not_check()
        if twin_after["state"] == "confirmed":
            return _stored("Received by PayPal", f"PayPal's record shows {where}, which has the same statements. "
                           "Nothing was sent now.", dispute_id, package_id, 200, kind="ok", state="recovered",
                           csrf_token=csrf)
        reason = str(twin_after.get("reason") or "")
        if reason.startswith(status_view.MISSING_FILE_PREFIX):
            names = [n for n in reason[len(status_view.MISSING_FILE_PREFIX):].split(",") if n]
            return _stored(partial_message(names), next_step.partial_text(names), dispute_id, package_id, 200,
                           kind="warn", state="partial", csrf_token=csrf)
        return _stored("Still not confirmed by PayPal", f"The send of {where}, which has the same statements, is still "
                       "not confirmed by PayPal. Nothing was sent now.", dispute_id, package_id, 200, kind="warn",
                       state="result-uncertain", csrf_token=csrf)
    if earlier:
        # an attempt for an EARLIER VERSION of this package is still waiting for PayPal. This version has no
        # attempt of its own, but "nothing pending" would be false for the package as a whole.
        if any(a["package_sha256"] in qualifying and not answered.get(a["package_sha256"], False) for a in earlier):
            return could_not_check()
        # only claim that PayPal "has not shown it yet" when a GET for that earlier attempt really ran and
        # was answered; otherwise say only that it has not been confirmed.
        looked = all(a["package_sha256"] in qualifying and answered.get(a["package_sha256"], False) for a in earlier)
        verb = "PayPal has not shown it yet" if looked else "it has not been confirmed yet"
        return _stored("An earlier version of this package is still waiting for PayPal",
                     f"A previous version of this package was sent and {verb}, so its result is still not known. "
                     "This version has not been sent. Use Check again, or edit nothing until PayPal answers. " + status,
                     dispute_id, package_id, 200, kind="warn", state="earlier-version-pending", csrf_token=csrf)
    return _stored("Nothing pending to check with PayPal", f"Nothing pending to check with PayPal. "
                 f"Status of this package: {status}", dispute_id, package_id, 200, kind=checked["kind"],
                 state="nothing-pending", csrf_token=csrf)


SAME_STATEMENTS_MARK_BASE = ("The earlier send has the same statements as this package, so it cannot be marked from "
                             "here yet.")
SAME_STATEMENTS_MARK_MESSAGE = (SAME_STATEMENTS_MARK_BASE + " Change a statement in this package first, then mark it as "
                                "not received.")
MARKED_NO_READ = "The package page shows what you can do next."


OWN_SEND_NOT_CONFIRMED_MESSAGE = ("This package's own send is not confirmed yet. There is no earlier send to mark. "
                                  "Use Check again.")


def _waiting_row_owner(db_path, dispute_id: str, package_sha: str) -> tuple[bool, int | None]:
    """(waiting, owner): whether the registry row for these exact statements is still unconfirmed (read after the
    reconciliation, so a row PayPal just confirmed does not count) and the package that registered its POST (None when
    no audit entry maps it). Read only."""
    store = SQLiteAttemptStore(db_path)
    try:
        row = store.read(dispute_id, package_sha)
    finally:
        store.close()
    if not row or row.get("state") != "sent_unconfirmed" or not row.get("request_id"):
        return False, None
    return True, send_service.registered_attempts(db_path, dispute_id).get(row["request_id"], (None, {}))[0]


def _same_statements_row_waiting(db_path, dispute_id: str, package_id: int, package_sha: str) -> bool:
    """True when the registry row for this package's exact statements is still unconfirmed and belongs to ANOTHER package."""
    waiting, owner = _waiting_row_owner(db_path, dispute_id, package_sha)
    return waiting and owner not in (None, package_id)


def _own_row_waiting(db_path, dispute_id: str, package_id: int, package_sha: str) -> bool:
    """True when that row is unconfirmed and belongs to THIS package: there is no earlier send to mark."""
    waiting, owner = _waiting_row_owner(db_path, dispute_id, package_sha)
    return waiting and owner == package_id


@router.post("/disputes/{dispute_id}/packages/{package_id}/confirm-not-received", response_class=HTMLResponse)
def confirm_not_received(request: Request, dispute_id: str, package_id: int, csrf_token: str = Form(...),
                         confirmed: str = Form(default=""), client: SandboxClient = Depends(get_client)):
    """The vendor's explicit, human-only acknowledgement - "I
    checked PayPal's Resolution Center: the earlier attempt is not there" - required before a different
    package for this dispute can be sent while another package's attempt is still 'sent_unconfirmed' (it
    may have actually reached PayPal; neither this route nor send_service ever assumes it did not).
    confirm_other_attempts_abandoned() reconciles via a fresh GET first and only abandons rows old enough
    to not plausibly be a POST still in flight - see its own docstring. Marks them
    'abandoned', never 'confirmed'/'failed' - the truth of what PayPal actually did with it stays unknown;
    this only records that a human looked and chose to proceed anyway. No MCP tool calls this - it is
    web-UI only, by design."""
    if not verify_csrf(request, csrf_token):
        return csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return bad_id
    if not 1 <= package_id <= MAX_PACKAGE_ID:
        return _package_not_found(request, dispute_id)
    db_path = config.db_path()
    # package_id is a path segment independent of dispute_id - never trust it belongs
    # to this dispute without checking (it could name a package under a DIFFERENT dispute entirely).
    package = repo.get_package(db_path, package_id)
    if package is None or package["dispute_id"] != dispute_id:
        return refusal_page(request,404,"Package not found","That package could not be found for this dispute.",back_url=f"/disputes/{dispute_id}")
    if confirmed.strip().lower() not in ("on", "true", "1", "yes"):
        return refusal_page(request,400,"Confirm the PayPal check first",
                            "You must check the box confirming you looked in PayPal's Resolution Center before this can proceed.",
                            back_url=f"/disputes/{dispute_id}/packages/{package_id}")
    claims = repo.list_claims(db_path, package_id)
    tracking_number, carrier = pick_tracking(extracted_docs_for(db_path, dispute_id))
    try:
        package_sha = sha256_json(build_request_input(claims, tracking_number, carrier))
    except ValueError:
        package_sha = f"not-sendable-yet-{package_id}"   # matches approve_package's own placeholder shape
    store = SQLiteAttemptStore(db_path)
    try:
        result = send_service.confirm_other_attempts_abandoned(client, store, db_path, dispute_id,
                                                                package_sha, actor="vendor")
    finally:
        store.close()
    closed = result["closed"]            # the rule / PayPal is not asking, from the read this very request made (or None)
    if result["abandoned"]:
        message = (f"{result['abandoned']} earlier unconfirmed attempt(s) for this dispute marked abandoned. "
                   + (closed or ("You can try sending this package now." if result["read"] else MARKED_NO_READ)))
    elif result["too_recent"]:
        message = send_service.ABANDON_TOO_RECENT_MESSAGE
    elif _same_statements_row_waiting(db_path, dispute_id, package_id, package_sha):
        # a row IS waiting, but it has this package's own statements: it is skipped here on
        # purpose (it is the same registry key), so say why and what to do instead of "Nothing was waiting".
        message = (f"{SAME_STATEMENTS_MARK_BASE} {closed}" if closed else SAME_STATEMENTS_MARK_MESSAGE if result["read"]
                   else f"{SAME_STATEMENTS_MARK_BASE} {MARKED_NO_READ}")
    elif _own_row_waiting(db_path, dispute_id, package_id, package_sha):
        # The unconfirmed row is this package's OWN send (the page offers the mark only for another package's
        # row, so this is a direct POST): say so and point to Check again. Nothing is written for it.
        message = OWN_SEND_NOT_CONFIRMED_MESSAGE
    else:
        message = "Nothing was waiting - there was no unconfirmed earlier attempt to mark abandoned."
    # Post-redirect-get: the answer is stored and shown by a GET (303), like the send and the check, so a reload of the page
    # repeats nothing. The abandon above happened once, on this POST.
    return _stored("Confirmed", message, dispute_id, package_id, 200, kind="info", state="acknowledged",
                   csrf_token=request.cookies.get(CSRF_COOKIE_NAME))
