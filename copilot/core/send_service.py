"""Shared send logic: copilot/web/routes_send.py (the web route, with its own CSRF check) and copilot/mcp_server.py's send_approved_package
(an in-process trusted call - MCP tool arguments are validated separately, there is no HTTP request to
forge a CSRF token for) both call send_package() here and get IDENTICAL guarantees: approval-fingerprint
match against the CURRENT content, the attempt registry (atomic claim before any network call), a fresh
GET + link/precondition validation, reconciliation (bounded, never an automatic resend), and the
attachment-landed check (verify_after_with_attachments). Every one of the send route's safety points
still applies - they just live here now, not duplicated per caller.

This replaces the MCP server's earlier approach of mounting the FastAPI app with a locally-generated
CSRF token via TestClient and parsing the HTML response's <h1> title - fragile (depended on the exact
title string surviving future wording changes) and it let the server get
around its own CSRF protection from the inside. Now the web route is the only HTTP entry point that needs
CSRF at all; everything else calls this module's plain Python function directly.

send_status_banner() (the persistent "Sent — received by PayPal on <date>" / "Sent — result pending,
check again" / "Not sent yet" banner) is also here now, public, for the same reason -
it used to be copilot/web/routes_package.py's own private _send_status_banner, which copilot/mcp_server.py
had to reach into directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from contextlib import nullcontext
from datetime import datetime, timezone

import httpx

from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.disputes import PayPalError, normalize, readback_audit, write_audit
from copilot.core.raw_storage import read_raw
from copilot.core.send import (ALLOWED_SEND_HOST, MISSING_ATTACHMENT_PREFIX, approval_fingerprint_with_attachments,
                               build_multipart, build_request_input, check_preconditions, classify, pick_tracking,
                               readback_record, request_id_for, sha256_json, validate_send_link,
                               verify_after_with_attachments)
from copilot.core.send_recovery import (PENDING_MESSAGE, RECEIVED_MESSAGE, partial_message, reconcile_after_send,
                                        recover_dispute)
from copilot.db import repo

PENDING_RECLAIM_AFTER_SECONDS = 120   # how old a 'pending' row (claimed, but the
                                      # process died before ever reaching the POST just below it - no
                                      # network call was made) must be before it is safe to assume
                                      # abandoned and explicitly reclaim, rather than refuse forever.


@dataclass
class SendOutcome:
    """A caller-agnostic result: routes_send.py renders it as an HTMLResponse (same status/title/message
    it always showed); copilot/mcp_server.py puts the same fields straight into its JSON result. `sent`
    is True only for the terminal "Received by PayPal" success state - everything else (pending, partial,
    failed, refused) is False, matching the one check mcp_server.py's caller actually needs."""
    http_status: int
    title: str
    message: str   # plain text, no HTML markup - callers decide how to present it
    technical: str | None = None
    sent: bool = False
    note: str | None = None   # a secondary line ("Changed after sending: ...; nothing was re-sent."), never the headline


def _pending_age_seconds(existing: dict) -> float | None:
    try:
        ts = datetime.strptime(existing["ts"], "%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, KeyError, TypeError):
        return None   # cannot parse: be conservative, never treat as stale
    return (datetime.now(timezone.utc) - ts).total_seconds()


# Evidence is sent once per dispute: pilot/step5_send_evidence.py refuses a send while PayPal's fresh read lists evidence
# submitted by the seller. ONCE_PER_DISPUTE states the fact and the rule: the web layer shows it only where the read lists
# that evidence. ONCE_PER_DISPUTE_RULE is its second sentence. The messages below use neither: they answer after a send and
# must be true whether or not a later read still lists the evidence.
ONCE_PER_DISPUTE_RULE = "Proofie sends evidence once per dispute and will not send again."
ONCE_PER_DISPUTE = "PayPal already lists evidence from you for this dispute. " + ONCE_PER_DISPUTE_RULE
NOT_ASKING_NOTE = "PayPal is not asking for evidence now."


UNUSABLE_LINK_NOTE = "PayPal's provide-evidence link is not one Proofie can use."


def provide_evidence_offered(norm) -> bool:
    """PayPal offers the seller a provide-evidence action on this read: the normalised read says so, or the link is
    there. The one definition behind every wording decision (pages, answers, tools); validate_send_link stays the gate of
    the POST and is not used to choose words."""
    if not isinstance(norm, dict):
        return False
    links = norm.get("action_links")
    return bool(norm.get("can_provide_evidence")) or (isinstance(links, dict) and "provide-evidence" in links)


def no_way_out(listed: bool, offered: bool, usable: bool = True) -> str | None:
    """The one rule for what a message says in place of a way out: the rule while the read lists evidence from the
    seller; otherwise that PayPal is not asking when it offers no provide-evidence action; otherwise, when it offers the
    action with a link Proofie cannot use, that; None while a send is possible (the message may then name its way out).
    Callers decide `offered` with provide_evidence_offered(read)."""
    if listed:
        return ONCE_PER_DISPUTE
    if not offered:
        return NOT_ASKING_NOTE
    return None if usable else UNUSABLE_LINK_NOTE


# The two precondition problems of the send path (pilot check_preconditions) that carry the PayPal enum names or a Python
# list; they are told in plain words wherever a message quotes them. Presentation only: the send path's own text is unchanged.
PRECONDITION_PROBLEM = re.compile(
    r"(?:evidence already submitted by the seller(?:: \[[^\]]*\])?"
    r"|(?P<kind>\w+) is not among the evidence PayPal requested from the seller \(\[[^\]]*\]\))"
    r"(?:;\s*(?=(?P<next>\S)))?")
_REQUESTED_KIND_WORDS = {"PROOF_OF_FULFILLMENT": "proof of shipment or delivery", "PROOF_OF_REFUND": "proof of a refund",
                         "OTHER": "other supporting evidence"}


def seller_words(text, listed=None) -> str:
    """The seller's words for a refusal text that quotes the send path's own preconditions. "evidence already submitted by
    the seller: [...]": `listed` is what the caller's fresh read says: True -> the fact and the rule; False -> that PayPal
    listed it when Proofie last tried and lists none now; None (no fresh read) -> only what happened. "<TYPE> is not among
    the evidence PayPal requested from the seller ([...])" -> that PayPal did not ask for that kind of evidence, with no
    enum name and no list. Other text is kept."""
    if not isinstance(text, str):
        return text
    words = (ONCE_PER_DISPUTE if listed else
             "PayPal listed evidence from you when Proofie last tried; it lists none now." if listed is False else
             "PayPal listed evidence from you when Proofie last tried.")
    # the words end in a full stop: a following problem of the same message ("...; response deadline ...") starts a new
    # sentence, with a capital
    def plain(match):
        said = (words if match.group("kind") is None else
                "PayPal did not ask for " + _REQUESTED_KIND_WORDS.get(match.group("kind"), "this kind of evidence")
                + " in this dispute.")
        return said + (" \0" if match.group("next") else "")        # the marker: the next problem starts a sentence

    return re.sub("\0(.)", lambda m: m.group(1).upper(), PRECONDITION_PROBLEM.sub(plain, text))


ALREADY_RECEIVED_TITLE = "Package already received"
ALREADY_RECEIVED_MESSAGE = ("PayPal previously confirmed this exact package as received. "
                            "Nothing new was sent. Proofie does not send the same package twice.")
SETTLING_MESSAGE = "Not sent: PayPal is updating this dispute. Check again in a minute."


@dataclass(frozen=True)
class SentFiles:
    """The files one attempt attached, as that attempt recorded them. A file is the same file by its sha256, never by its
    name (two documents called label.pdf are two files). `shas` is None for an attempt that
    predates the sha records: it is then compared by name, as before. `known` is False for a legacy attempt with no
    record of its files at all: it counts as matching, the same rule as copilot/web/status.py's send_record."""
    names: tuple = ()
    shas: tuple | None = None
    known: bool = False


def registered_attempts(db_path, dispute_id: str) -> dict:
    """request_id -> (package_id, detail) from the audit entries the send path writes when it registers an attempt."""
    registered = {}
    for entry in repo.list_audit(db_path, dispute_id):
        if entry.get("action") == "send_attempt_registered" and isinstance(entry.get("detail"), dict):
            registered[entry["detail"].get("request_id")] = (entry["detail"].get("package_id"), entry["detail"])
    return registered


def sent_files_of(attempt: dict, detail: dict | None = None) -> SentFiles:
    """The attempt's own record first, then the audit entry written when it was registered."""
    detail = detail or {}
    shas = attempt.get("attachment_sha256")
    if not shas:
        audited = detail.get("attachment_sha256")
        shas = audited if isinstance(audited, list) else None
    names = attempt.get("attachment_filenames")
    if not names:
        audited = detail.get("attachments")
        names = audited if isinstance(audited, list) else None
    if shas is None and names is None:
        return SentFiles()
    return SentFiles(names=tuple(names or ()), shas=None if shas is None else tuple(shas), known=True)


def files_match(sent: SentFiles, current_rows) -> bool:
    """Do the files the attempt attached equal the package's files now (rows with filename and sha256)?"""
    if not sent.known:
        return True
    if sent.shas is not None:
        return sorted(sent.shas) == sorted(r["sha256"] for r in current_rows)
    return sorted(sent.names) == sorted(r["filename"] for r in current_rows)


def split_rows(sent: SentFiles, current_rows) -> tuple:
    """(carried, unsent): the package's attachment rows that the attempt carried, and those it did not (by sha256; by
    name for an attempt that predates the sha records). An attempt with no record of its files carried them all."""
    rows = list(current_rows)
    if not sent.known:
        return rows, []
    key, remaining = ("sha256", list(sent.shas)) if sent.shas is not None else ("filename", list(sent.names))
    carried, unsent = [], []
    for row in rows:
        if row[key] in remaining:
            remaining.remove(row[key])
            carried.append(row)
        else:
            unsent.append(row)
    return carried, unsent


def removed_files(sent: SentFiles, current_rows) -> list:
    """The names of the files the attempt carried that are not attached now (by sha256; by name for an attempt that
    predates the sha records). They are in PayPal and no longer attached here."""
    if not sent.known:
        return []
    rows = list(current_rows)
    if sent.shas is not None:
        remaining = [r["sha256"] for r in rows]
        names = list(sent.names) if len(sent.names) == len(sent.shas) else [None] * len(sent.shas)
        gone = []
        for sha, name in zip(sent.shas, names):
            if sha in remaining:
                remaining.remove(sha)
            else:
                gone.append(name or "a file")
        return gone
    remaining = [r["filename"] for r in rows]
    gone = []
    for name in sent.names:
        if name in remaining:
            remaining.remove(name)
        else:
            gone.append(name)
    return gone


def unsent_files(sent: SentFiles, current_rows) -> list:
    """The names of the package's files that the attempt did not carry."""
    return [row["filename"] for row in split_rows(sent, current_rows)[1]]


def attempt_attachments_match(db_path, dispute_id: str, attempt: dict, current_rows) -> bool:
    """Do the files this attempt attached equal the package's files now? `current_rows` are attachment rows
    (filename, sha256), as repo.list_attachments returns them."""
    detail = registered_attempts(db_path, dispute_id).get(attempt.get("request_id"), (None, {}))[1]
    return files_match(sent_files_of(attempt, detail), current_rows)


def content_sha(db_path, dispute_id: str, package_id: int) -> str | None:
    """The content hash the package would be sent under, or None while it is not sendable."""
    claims = repo.list_claims(db_path, package_id)
    tracking_number, carrier = pick_tracking(extracted_docs_for(db_path, dispute_id))
    try:
        return sha256_json(build_request_input(claims, tracking_number, carrier))
    except (ValueError, KeyError, TypeError):
        return None


def package_attempts(db_path, dispute_id: str, package: dict, attempts=None, registered=None) -> list:
    """The attempts of ONE package. They are mapped to it through the audit entry `send_attempt_registered`
    (detail.package_id -> request_id -> attempt row), so a later change of the shared shipment details cannot hide an
    earlier send, and another package's attempt with the same statements never speaks for this one; an attempt with no
    such entry falls back to the package's current content sha."""
    if attempts is None:
        store = SQLiteAttemptStore(db_path)
        try:
            attempts = store.list_by_dispute(dispute_id)
        finally:
            store.close()
    if registered is None:
        registered = registered_attempts(db_path, dispute_id)
    mine = [a for a in attempts if a.get("request_id") in registered
            and registered[a["request_id"]][0] == package["id"]]
    if not mine:
        current = content_sha(db_path, dispute_id, package["id"])
        if current is not None:
            mine = [a for a in attempts if a["package_sha256"] == current
                    and (a.get("request_id") not in registered or registered[a["request_id"]][0] == package["id"])]
    return mine


def pick_attempt(mine: list) -> dict | None:
    """The attempt that speaks for the package: a confirmed one, else the latest."""
    if not mine:
        return None
    confirmed = [a for a in mine if a["state"] == "confirmed"]
    return confirmed[-1] if confirmed else sorted(mine, key=lambda a: a.get("ts") or "")[-1]


def confirmed_attempt_for(db_path, dispute_id: str, package_id: int) -> dict | None:
    """The attempt PayPal confirmed for this package, when the files it carried (by sha256) are the files
    attached now; otherwise None. Found through the audit entry that registered it (so a later edit of the shared
    shipment details cannot hide it), else by the package's current content. Reads only; never needs an approval."""
    package = repo.get_package(db_path, package_id)
    if package is None:
        return None
    rows = repo.list_attachments(db_path, package_id)
    registered = registered_attempts(db_path, dispute_id)
    mine = [a for a in package_attempts(db_path, dispute_id, package, registered=registered)
            if a.get("state") == "confirmed"]
    for attempt in mine:
        detail = registered.get(attempt.get("request_id"), (None, {}))[1]
        if files_match(sent_files_of(attempt, detail), rows):
            return attempt
    if mine:
        return None
    # Not registered for this package: the same statements and tracking confirmed under another package (a new
    # version with identical content and files is "this exact package").
    sha = content_sha(db_path, dispute_id, package_id)
    if sha is None:
        return None
    store = SQLiteAttemptStore(db_path)
    try:
        attempt = store.read(dispute_id, sha)
    finally:
        store.close()
    if not attempt or attempt.get("state") != "confirmed":
        return None
    detail = registered.get(attempt.get("request_id"), (None, {}))[1]
    return attempt if files_match(sent_files_of(attempt, detail), rows) else None


SAME_STATEMENTS_TITLE = "Same statements, other files"
# The way out that works: a changed statement gives this package its own attempt row, so the earlier send
# can be marked as not received from this package's page and this package then goes out. Nothing here touches the
# earlier package's attempt record.
UNCONFIRMED_COLLISION_ADVICE = ("Change a statement in this package (for example add one sentence), then mark the "
                                "earlier send as not received on this package's page, then send this package.")


def same_statements_other_files(db_path, dispute_id: str, package_id: int, attempt: dict, rows,
                                closed: str | None = None) -> SendOutcome:
    """The refusal for statements PayPal already holds from an earlier send that carried other files: nothing is sent,
    the unsent file is named, and the earlier send is never reported as this package's."""
    owner, detail = registered_attempts(db_path, dispute_id).get(attempt.get("request_id"), (None, {}))
    unsent = unsent_files(sent_files_of(attempt, detail), rows)
    earlier = repo.get_package(db_path, owner) if owner and owner != package_id else None
    where = f"package v{earlier['version']}" if earlier else "an earlier send"
    confirmed = attempt.get("state") == "confirmed"
    held = ("PayPal confirmed these statements" if confirmed
            else "These statements were sent before and PayPal has not confirmed them")
    files = (f" Not sent: {', '.join(unsent)}." if unsent
             else " This package carries a different set of files than that send.")
    if confirmed:
        # "already holds" only while the read lists it; otherwise what Proofie saw when it checked
        holds = ("PayPal already holds these statements" if closed == ONCE_PER_DISPUTE
                 else "When Proofie checked, PayPal held these statements")
        advice = (f"{holds}, so Proofie cannot send them again with another file. "
                  + (closed or "Change a statement to make a new submission."))
    else:
        # nothing says PayPal holds them; it may, so find out what PayPal has first. Then the way out
        # is the one that works without touching the earlier attempt's record (only while a send is possible).
        advice = ("PayPal may already hold these statements, so Proofie will not send them again with another file. "
                  "Use Check again on the earlier version first. "
                  + (closed or "If PayPal's Resolution Center shows nothing for it, follow these steps. "
                     + UNCONFIRMED_COLLISION_ADVICE))
    return SendOutcome(409, SAME_STATEMENTS_TITLE,
                       f"{held} with {where}, which carried other files. Nothing was sent now.{files} {advice}")


def send_package(client, db_path, dispute_id: str, package_id: int) -> SendOutcome:
    package = repo.get_package(db_path, package_id)
    if package is None:
        return SendOutcome(404, "Not found", "That package could not be found.")
    # An exact package PayPal already confirmed is answered from the registry, before the approval is looked
    # at: a change after sending clears the approval, and that must not turn a repeat click into "Not approved".
    if confirmed_attempt_for(db_path, dispute_id, package_id) is not None:
        return SendOutcome(409, ALREADY_RECEIVED_TITLE, ALREADY_RECEIVED_MESSAGE)
    approval = repo.get_latest_valid_approval(db_path, package_id)
    if approval is None:
        return SendOutcome(400, "Not approved",
                           "This package is not approved yet.")

    claims = repo.list_claims(db_path, package_id)
    extracted_docs = extracted_docs_for(db_path, dispute_id)
    tracking_number, carrier = pick_tracking(extracted_docs)
    try:
        req_input = build_request_input(claims, tracking_number, carrier)
    except ValueError as exc:
        return SendOutcome(400, "Cannot send", f"{exc}.")

    attachment_rows = repo.list_attachments(db_path, package_id)
    if any(a["excluded"] for a in attachment_rows):
        return SendOutcome(400, "Cannot send", LEFT_OUT_ATTACHED_SEND)
    attachment_shas = [a["sha256"] for a in attachment_rows]
    current_fp = approval_fingerprint_with_attachments(dispute_id, "provide_evidence", req_input,
                                                        attachment_shas)
    if current_fp != approval["approval_sha256"]:
        return SendOutcome(400, "Approval out of date",
                           "This package changed since it was approved (or tracking was not known yet at "
                           "approval time), so the approval no longer covers it.")

    attachments = []
    uploads_dir = config.uploads_dir()
    for a in attachment_rows:
        data = read_raw(uploads_dir, a["sha256"])
        if data is None:
            return SendOutcome(400, "Cannot send",
                               f"The attached file \"{a['filename']}\" could not be found on disk. "
                               f"Detach and re-attach it, or re-upload it.")
        attachments.append({"filename": a["filename"], "content_type": a["content_type"], "data": data})

    attachment_filenames = [a["filename"] for a in attachments]
    package_sha = sha256_json(req_input)
    store = SQLiteAttemptStore(db_path)
    try:
        return _send_locked(client, store, db_path, dispute_id, package_id, req_input, package_sha,
                            approval, attachments, attachment_filenames, attachment_shas)
    finally:
        store.close()


def _sent_review_required(detail: str | None = None) -> SendOutcome:
    """The POST was answered with success but what PayPal shows or what changed locally needs the seller's look."""
    what = (f"a source changed while sending: {detail}" if detail
            else "this package was marked as not shown in PayPal while it was being checked")
    return SendOutcome(202, "Sent, review required",
                       f"Sent, but {what}. Check PayPal's record; nothing will be sent again automatically.")


SUPERSEDED_REASON = "superseded, never sent"
ABANDONED_REASON = "vendor confirmed PayPal's Resolution Center does not show it"
# The earlier attempt being marked can be a PARTIAL one (PayPal lists its statements but not a file; its own reason starts
# with MISSING_ATTACHMENT_PREFIX). "Does not show it" would be false for it, so its abandon record says what the vendor
# actually confirmed. Which rows may be abandoned does not depend on this.
ABANDONED_PARTIAL_REASON = "vendor confirmed PayPal's Resolution Center shows the statements without the attached file"
# THIS package's own content was itself marked abandoned (by a vendor's
# confirm-not-received action on some other package, before this one was ever edited again) - "wait and
# check again" is the wrong advice, since nothing is actually in flight; the content must change (a new
# version) before it can be claimed/sent again. Shared by every call site that discovers this.
# A file the seller left out of the checks is no longer compared with PayPal's data. While it is still attached it would be
# sent, so a wrong receipt could reach PayPal with its conflict hidden: approval and sending stop until she removes it.
LEFT_OUT_ATTACHED_BLOCK = ("A file you left out of the checks is still attached to this package, so it would be sent to PayPal. "
                           "Remove it from the package before approving.")
LEFT_OUT_ATTACHED_SEND = ("A file you left out of the checks is still attached to this package. Remove it from the package, "
                          "approve again, then send. Nothing was sent.")

_ABANDONED_OUTCOME = SendOutcome(409, "Not sent", "This exact package was marked as not received. To send "
                                 "again, edit it (this creates a new version).")
# A true partial send that was marked as not received. The text depends on the fresh read the caller holds: the way out
# only while a send is possible; while the read lists evidence from the seller it is the rule, and while PayPal offers no
# provide-evidence action it says PayPal is not asking.
_ABANDONED_PARTIAL_BASE = ("This exact package was marked as not received, and when Proofie checked, PayPal showed its "
                           "statements without the attached file. ")
_ABANDONED_PARTIAL_OUTCOME = SendOutcome(409, "Not sent", _ABANDONED_PARTIAL_BASE +
                                         "To send this content, change it first: that makes a new version.")
_ABANDONED_LISTED_OUTCOME = SendOutcome(409, "Not sent", "This exact package was marked as not received. " + ONCE_PER_DISPUTE)
_ABANDONED_PARTIAL_LISTED_OUTCOME = SendOutcome(409, "Not sent", _ABANDONED_PARTIAL_BASE + ONCE_PER_DISPUTE)


def _abandoned_outcome(attempt, closed: str | None = None) -> SendOutcome:
    partial = (attempt or {}).get("reason") == ABANDONED_PARTIAL_REASON
    if closed is None:
        return _ABANDONED_PARTIAL_OUTCOME if partial else _ABANDONED_OUTCOME
    if closed == ONCE_PER_DISPUTE:
        return _ABANDONED_PARTIAL_LISTED_OUTCOME if partial else _ABANDONED_LISTED_OUTCOME
    return SendOutcome(409, "Not sent", (_ABANDONED_PARTIAL_BASE if partial
                                         else "This exact package was marked as not received. ") + closed)


def _unconfirmed_other_text(closed: str | None, read: bool) -> str:
    """Another package's send PayPal has not confirmed. Before the fresh read (`read` False) the text promises nothing and
    points to Check again and the package page; after it, the way out is named only while a send is possible."""
    head = "Another send for this dispute has not been confirmed by PayPal yet"
    if closed:
        return f"{head}. {closed}"
    if not read:
        return (f"{head} - it may have actually reached PayPal. Use Check again first. The package page says what to do "
                "if PayPal's Resolution Center shows nothing for it.")
    return (f"{head} - it may have actually reached PayPal. Use Check again first; if PayPal's Resolution Center "
            "genuinely shows nothing for it, confirm that explicitly below before sending a different package.")


def _in_progress_other_text(closed: str | None, read: bool) -> str:
    """Another package's send is still in progress. Before the read the text points to Check again; after it, the way on
    ("before sending a different package") only while a send is possible, the rule or "not asking" otherwise."""
    head = "Another send for this dispute is still in progress."
    if closed:
        return f"{head} {closed}"
    if not read:
        return f"{head} Wait a moment and use Check again."
    return f"{head} Wait a moment and check again before sending a different package."


def _other_attempts_block(store, db_path, dispute_id: str, package_id: int, package_sha: str,
                          closed: str | None = None, read: bool = False) -> SendOutcome | None:
    """A SendOutcome to refuse with if some OTHER package's attempt for this dispute is still
    genuinely in flight; None if clear to proceed.

    Policy:
    (a) a stale 'pending' row (claimed, but old enough per PENDING_RECLAIM_AFTER_SECONDS that it never
        reached a POST) never blocks - it is neutralized to 'blocked'/SUPERSEDED_REASON and audited, so it
        stops showing up here on a later call too.
    (b) a 'sent_unconfirmed' row for a DIFFERENT package is NEVER silently ignored - it may have actually
        reached PayPal. It always blocks until either PayPal's own GET confirms/clears it (Check again,
        copilot/web/routes_send.py's check_package route, which calls recover_dispute - the SAME function
        this one calls first) or the vendor explicitly confirms, via confirm_other_attempts_abandoned()
        below, that they checked PayPal's Resolution Center themselves and it is not there. The model has
        no tool that can ever call that function - it is a human-only action."""
    for other in store.list_by_dispute(dispute_id):
        if other["package_sha256"] == package_sha:
            continue
        if other["state"] == "pending":
            age = _pending_age_seconds(other)
            if age is not None and age > PENDING_RECLAIM_AFTER_SECONDS:
                # supersede() is guarded on (state='pending', ts) - never a blind
                # write(), since the POST for this exact row could have actually gone out (and the row
                # become 'sent_unconfirmed') in the gap between list_by_dispute() above and this call.
                if store.supersede(dispute_id, other["package_sha256"], other["ts"], SUPERSEDED_REASON):
                    repo.log_action(db_path, "system", "send_attempt_superseded", dispute_id,
                                    {"package_id": package_id,
                                     "other_package_sha256": other["package_sha256"], "age_seconds": age})
                    continue
                fresh = store.read(dispute_id, other["package_sha256"])
                if fresh and fresh["state"] == "sent_unconfirmed":
                    return SendOutcome(409, "Result pending — check again", _unconfirmed_other_text(closed, read))
                continue   # someone else already superseded/reclaimed/confirmed/abandoned it - fine
            return SendOutcome(409, "Result pending — check again", _in_progress_other_text(closed, read))
        if other["state"] == "sent_unconfirmed":
            return SendOutcome(409, "Result pending — check again", _unconfirmed_other_text(closed, read))
    return None


ABANDON_TOO_RECENT_MESSAGE = "An attempt was sent moments ago; wait and check again."


class _LastRead:
    """Passes every call through and remembers the answer of the last GET, so the caller can choose its words by what that
    read lists. It adds no call and changes nothing."""

    def __init__(self, client):
        self._client, self.last = client, None

    def call(self, method, path, *args, **kwargs):
        answer = self._client.call(method, path, *args, **kwargs)
        if method == "GET":
            self.last = answer
        return answer

    def __getattr__(self, name):
        return getattr(self._client, name)


def confirm_other_attempts_abandoned(client, store, db_path, dispute_id: str, package_sha: str,
                                     actor: str = "vendor") -> dict:
    """The vendor's explicit, human-only action ("I checked PayPal's Resolution
    Center: the earlier attempt is not there").

    Reconciles FIRST (recover_dispute, the same one-GET-per-attempt check every other entry point already
    runs) - 'sent_unconfirmed' is written BEFORE the POST, so a send that is genuinely still in flight
    right now looks IDENTICAL to one that has been stuck for hours; without a check, a vendor clicking
    this a few seconds after someone else's click could abandon a POST that has not even returned yet.

    Only then abandons rows that are STILL 'sent_unconfirmed' and old enough
    (PENDING_RECLAIM_AFTER_SECONDS - the same window the stale-pending reclaim in _send_locked uses for "safe to assume this attempt is
    over") to not plausibly be a POST in flight right now; anything more recent is left untouched and
    counted separately, so the caller can tell the vendor to wait rather than silently doing nothing.
    abandon() (not write()) guards the UPDATE on the exact ts just read, so a row recover_dispute() just
    confirmed (or anything else that changed it) is never silently overwritten.

    Never writes 'confirmed' or 'failed' - the truth of what PayPal actually did with an abandoned
    attempt stays unknown; this only records that a human looked and chose to proceed anyway.
    recover_dispute() (called first, above) is the only place that can ever later promote an abandoned row
    to 'confirmed', if PayPal's own GET eventually shows it after all.

    No tool on copilot/mcp_server.py ever calls this - approval and this acknowledgement are both
    exclusively human, web-UI actions."""
    reader = _LastRead(client)
    recover_dispute(reader, store, dispute_id, db_path)
    abandoned, too_recent = 0, 0
    for other in store.list_by_dispute(dispute_id):
        if other["package_sha256"] == package_sha or other["state"] != "sent_unconfirmed":
            continue
        age = _pending_age_seconds(other)
        if age is None or age <= PENDING_RECLAIM_AFTER_SECONDS:
            too_recent += 1
            continue
        reason = str(other.get("reason") or "")
        partial = reason.startswith(MISSING_ATTACHMENT_PREFIX)
        # the files PayPal did not list, read from the row's own reason before abandon() replaces it
        missing_files = [n for n in reason[len(MISSING_ATTACHMENT_PREFIX):].split(",") if n] if partial else []
        if store.abandon(dispute_id, other["package_sha256"], other["ts"],
                         ABANDONED_PARTIAL_REASON if partial else ABANDONED_REASON):
            detail = {"other_package_sha256": other["package_sha256"]}
            if partial:
                detail.update(partial=True, missing_files=missing_files)
            repo.log_action(db_path, actor, "abandoned_by_vendor", dispute_id, detail)
            abandoned += 1
        # a False from abandon() means it moved on (recover_dispute just confirmed it, most likely) -
        # nothing to count as abandoned, and nothing wrong either; just not relevant any more.
    closed, read = None, False         # None: a send is possible on that read, or no usable read (then `read` is False)
    if isinstance(reader.last, dict):
        try:                           # wording only: it must never fail after the abandon writes above
            norm = normalize(reader.last, datetime.now(timezone.utc))
            closed = no_way_out(bool(norm.get("evidence_submitted_by_seller")), provide_evidence_offered(norm),
                                validate_send_link(norm) is None)
            read = True
        except (ValueError, AttributeError, TypeError):
            closed, read = None, False
    return {"abandoned": abandoned, "too_recent": too_recent, "read": read, "closed": closed}


def attempt_request_id(db_path, dispute_id: str, package_id: int, package_sha: str, attachment_shas) -> str:
    """The PayPal-Request-Id of this package's POST. It is the id derived from the statements and the tracking (the
    registry key), as always, unless ANOTHER package already registered an attempt under that id with OTHER files: the
    "same statements, other files" send over the row of an earlier attempt PayPal cleanly refused ('failed', nothing
    received; the one kind of row of another package that a send takes over). Reusing the earlier attempt's id could
    let PayPal treat it as the earlier request (not observed for provide-evidence), without these files, so that send's id also covers the set of attachment sha256: new, and deterministic for its own content (a
    retry of the same package after a refusal or a local block gets the same id again). A first send and a repeat of the
    same package keep the statements-only id; so does an earlier registration with no record of its files."""
    base = request_id_for(dispute_id, package_sha)
    owner, detail = registered_attempts(db_path, dispute_id).get(base, (None, {}))
    if owner in (None, package_id) or files_match(sent_files_of({}, detail), repo.list_attachments(db_path, package_id)):
        return base
    return request_id_for(dispute_id, f"{package_sha}:files:{sha256_json(sorted(attachment_shas or []))}")


def _send_locked(client, store, db_path, dispute_id: str, package_id: int, req_input: dict,
                 package_sha: str, approval: dict, attachments: list,
                 attachment_filenames: list, attachment_shas: list | None = None) -> SendOutcome:
    attachment_shas = list(attachment_shas) if attachment_shas is not None else []
    # Reconcile first, before anything else: anything a crash or restart left hanging for this dispute
    # (recover_dispute() never raises on a GET failure; it leaves what it could not check as still pending).
    recover_dispute(client, store, dispute_id, db_path)

    # never let a second package for the SAME dispute start while another one (a different
    # package_sha) is still in flight - two sends racing for one dispute is exactly the duplicate-evidence
    # risk the send guards exist to prevent, even across two different packages.
    blocked = _other_attempts_block(store, db_path, dispute_id, package_id, package_sha)
    if blocked is not None:
        return blocked

    # PayPal being unreachable for this fresh GET must never reach the claim/POST below - nothing is
    # claimed and nothing is sent, so the vendor can simply try again shortly.
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "pre_send_get",
                          log_extra=readback_audit(package_sha, approval["approval_sha256"]))
    except (PayPalError, httpx.TransportError):
        return SendOutcome(503, "Not sent", "PayPal could not be reached, nothing was sent.")
    norm = normalize(raw, datetime.now(timezone.utc))
    # The fresh read only chooses the words of the refusals below: the rule while the read lists evidence from the
    # seller, that PayPal is not asking while it offers no provide-evidence action, the way out only while a send is
    # possible.
    listed = bool(norm.get("evidence_submitted_by_seller"))
    closed = no_way_out(listed, provide_evidence_offered(norm), validate_send_link(norm) is None)
    # PayPal shows UNDER_REVIEW with no provide-evidence link for about a minute after any seller write. That is a
    # wait, not a refusal: nothing is claimed or recorded, so the next click after a minute starts clean.
    if validate_send_link(norm):
        from copilot.web.status import is_settling
        if is_settling(norm):
            return SendOutcome(409, "Not sent", SETTLING_MESSAGE)
    verify = verify_after_with_attachments(raw, req_input, attachment_filenames)

    # Claim FIRST, inspect only on failure (a double-click or two near-simultaneous requests must
    # never both reach the POST below). A read-then-conditionally-claim order - matching pilot's own
    # send(), which this module otherwise mirrors - has a real TOCTOU gap: two callers can each see "no
    # existing attempt", both fall through, and both POST. claim() is the atomic INSERT (BEGIN IMMEDIATE);
    # exactly one caller can ever win it for a given (dispute_id, package_sha).
    if not store.claim(dispute_id, package_sha):
        existing = store.read(dispute_id, package_sha)
        if existing is None:
            # claim() returned False without an IntegrityError (busy_timeout lock contention, not
            # "someone already has a row here") - nothing to reconcile against. Refuse cleanly instead of
            # crashing on existing["state"] below with a TypeError that would surface as a 500.
            return SendOutcome(409, "Not sent", "Could not register the attempt, try again.")
        # only trust PayPal's content match as confirmation of OUR OWN attempt when some registry
        # row for this exact package_sha actually recorded a POST (request_id set) - content that merely
        # looks right from a row that never POSTed (e.g. ours is still 'pending', about to attempt its own
        # first POST) must never be treated as "already sent", confirmed or partial.
        names_kw = {"attachment_filenames": attachment_filenames}   # what a write below stores as the attempt's files
        if existing.get("request_id") and existing["state"] in ("confirmed", "sent_unconfirmed", "abandoned"):
            # the registry key is the statements and the tracking, not the files. A row that
            # POSTed other files (by sha256, never by name) holds statements PayPal already has: this package cannot be
            # sent under it, and PayPal's name-based read-back must not be taken as proof that THESE files landed.
            # Refused here with the unsent file named: nothing is written, nothing is sent.
            other = registered_attempts(db_path, dispute_id).get(existing.get("request_id"), (None, {}))
            sent = sent_files_of(existing, other[1])
            rows = repo.list_attachments(db_path, package_id)
            if (other[0] not in (None, package_id) and not files_match(sent, rows)
                    and existing["state"] != "abandoned"):
                return same_statements_other_files(db_path, dispute_id, package_id, existing, rows, closed)
            if sent.shas is not None and not files_match(sent, rows):
                # the same package, a file added (or replaced) after that attempt: whatever PayPal lists by NAME may be
                # the old bytes, so it is never taken as proof that THESE files landed
                missing = unsent_files(sent, rows)
                partial = SendOutcome(206, partial_message(missing),
                                      "The shipment details were received, but at least one attached file was "
                                      "not. Nothing was sent again automatically.",
                                      technical=f"missing: {missing}")
                if existing["state"] == "confirmed":
                    if missing:
                        return partial
                    # every file attached now was carried by that confirmed send (a file was removed since): exact package
                    return SendOutcome(409, ALREADY_RECEIVED_TITLE, ALREADY_RECEIVED_MESSAGE)
                if missing and verify["content_ok"]:
                    return partial
            if removed_files(sent, rows):
                # a file the attempt carried is not attached now. PayPal's read is checked against EVERY
                # file the attempt carried (its own names), never only the files attached now, and the attempt's own file
                # record is never overwritten. Without a complete name record nothing can be verified: nothing is claimed.
                names_kw = {}
                if sent.names and (sent.shas is None or len(sent.names) == len(sent.shas)):
                    verify = verify_after_with_attachments(raw, req_input, list(sent.names))
                else:
                    verify = {"content_ok": False, "files_ok": False, "missing_files": [], "details": {}}
        if existing.get("request_id") and verify["content_ok"] and verify["files_ok"]:
            # 'confirmed' is the one state allowed to overwrite anything unconditionally
            # (PayPal's own truth always wins) - but if the row we are overwriting was 'abandoned', that
            # fact is worth keeping, not just silently replaced.
            if existing["state"] == "abandoned":
                repo.log_action(db_path, "system", "abandoned_attempt_later_confirmed_by_paypal",
                                dispute_id, {"package_id": package_id})
            store.write(dispute_id, package_sha, {"state": "confirmed", "request_input": req_input, **names_kw,
                                                  "readback": readback_record(verify["details"])})
            return SendOutcome(200, RECEIVED_MESSAGE,
                               "PayPal already has this exact content on file. Nothing new was sent.",
                               technical=str(verify["details"]), sent=True)
        if existing.get("request_id") and verify["content_ok"] and not verify["files_ok"]:
            # 'sent_unconfirmed' is NOT a terminal state, but writing it here still
            # must never DOWNGRADE a row some concurrent caller already settled (as 'confirmed' - PayPal's
            # GET landed first there too - or 'abandoned' - a vendor acted on it) between `existing` above
            # and this point. Write conditionally in the same transaction that reads the effective state;
            # the only terminal states ever guarded against
            # a downgrade into here are the two that could plausibly race with THIS exact package_sha.
            current = store.write_partial(dispute_id, package_sha,
                       {"state": "sent_unconfirmed", "request_input": req_input, **names_kw,
                        "reason": MISSING_ATTACHMENT_PREFIX + ",".join(verify["missing_files"])})
            if current and current["state"] == "confirmed" and attempt_attachments_match(
                    db_path, dispute_id, current, repo.list_attachments(db_path, package_id)):
                return SendOutcome(200, RECEIVED_MESSAGE,
                                   "PayPal already has this exact content on file. Nothing new was sent.",
                                   sent=True)
            if current and current["state"] == "abandoned":
                return _abandoned_outcome(current, closed)
            # the confirmed attempt carried other files than the ones attached now (a file was added after
            # the send): PayPal has the statements but not that file. Never "received", never a second POST.
            return SendOutcome(206, partial_message(verify["missing_files"]),
                               "The shipment details were received, but at least one attached file was "
                               "not. Nothing was sent again automatically.",
                               technical=f"missing: {verify['missing_files']}\n{verify['details']}")
        if existing["state"] == "confirmed":
            # A later GET may omit the evidence. Preserve the historical confirmation
            # and its consumed approval; this is not a pending or retryable attempt.
            return SendOutcome(409, ALREADY_RECEIVED_TITLE, ALREADY_RECEIVED_MESSAGE)
        age = _pending_age_seconds(existing)
        still_blocked = SendOutcome(409, "Outcome pending",
                                    "An attempt for this exact package is already in progress or was not "
                                    "yet confirmed. Wait a moment and check again rather than sending a "
                                    "second time.")
        if existing["state"] in ("failed", "blocked"):
            # A clean, known rejection (PayPal's, "failed") or a local refusal that already prevented any
            # POST ("blocked") - either way nothing was received, safe to explicitly reclaim (done
            # atomically, so two racing callers starting from the SAME stale row never both win). A
            # 'blocked' row (e.g. no link yet) must be retriable once the underlying reason is gone -
            # never a permanent dead end for that package_sha.
            if not store.reclaim(dispute_id, package_sha, existing["state"], existing["ts"]):
                return still_blocked
        elif existing["state"] == "pending" and age is not None and age > PENDING_RECLAIM_AFTER_SECONDS:
            # Claimed but the process died before ever reaching the POST (no network
            # call happened) - old enough that it is not a concurrent request right now, safe to reclaim
            # (atomically, same reasoning as the 'failed' branch above).
            if not store.reclaim(dispute_id, package_sha, "pending", existing["ts"]):
                return still_blocked
            repo.log_action(db_path, "system", "send_attempt_reclaimed_stale_pending", dispute_id,
                            {"package_id": package_id, "previous_ts": existing["ts"], "age_seconds": age})
        elif existing["state"] == "abandoned":
            return _abandoned_outcome(existing, closed)
        else:
            # pending/sent_unconfirmed, not stale, not yet confirmed: could be a genuine double-click
            # happening right now, or a crash left hanging (recover_dispute above already tried to settle
            # that via one GET). Either way, never silently retry-POST here.
            return still_blocked

    # re-check right after winning the claim/reclaim above - a THIRD caller could have started a
    # DIFFERENT package's send in the gap between the first check and this one (different package_sha, so
    # claim() above did not itself serialise against it). Our own just-claimed row is marked 'blocked'
    # rather than left 'pending' forever, and nothing POSTs.
    blocked = _other_attempts_block(store, db_path, dispute_id, package_id, package_sha, closed, read=True)
    if blocked is not None:
        store.write(dispute_id, package_sha, {"state": "blocked",
                                              "reason": "another package's send was found in progress "
                                                        "right after claiming"})
        return blocked

    link_err = validate_send_link(norm)
    problems = [] if link_err else check_preconditions(norm, req_input, dispute_id, None)
    if link_err or problems:
        # a LOCAL refusal (no link yet, or a precondition this app itself checks) is not PayPal
        # rejecting anything - 'failed' stays reserved for a real 4xx PayPal gives us (below, after an
        # actual POST). 'blocked' says so plainly wherever this attempt is shown again later (the banner,
        # get_send_status): "Not sent: <reason>", never "rejected by PayPal".
        reason = link_err or "; ".join(problems)
        store.write(dispute_id, package_sha, {"state": "blocked", "reason": reason, "request_input": req_input})
        repo.log_action(db_path, "system", "send_blocked_no_link" if link_err else "send_blocked_preconditions",
                        dispute_id, {"package_id": package_id, "reason": reason})
        return SendOutcome(409, "Not sent", reason)

    guard = nullcontext()
    review_errors = ()
    if config.feature_enabled("active_approval"):
        from copilot.features.active_approval.service import ReviewError, send_guard
        review_errors = (ReviewError,)
        try:
            guard = send_guard(client, db_path, dispute_id, package_id, approval["id"])
        except ReviewError as e:
            store.write(dispute_id, package_sha, {"state": "blocked", "reason": "active review required"})
            return SendOutcome(409, "Not sent", str(e))
        except (PayPalError, httpx.TransportError):
            store.write(dispute_id, package_sha, {"state": "blocked", "reason": "active review unavailable"})
            return SendOutcome(503, "Not sent", "The current sources could not be checked, nothing was sent.")

    request_id = attempt_request_id(db_path, dispute_id, package_id, package_sha, attachment_shas)
    repo.log_action(db_path, "system", "send_attempt_registered", dispute_id,
                    {"package_id": package_id, "approval_sha256": approval["approval_sha256"],
                     "request_id": request_id, "attachments": attachment_filenames,
                     "attachment_sha256": attachment_shas})
    # Written BEFORE the network call: a crash mid-POST leaves 'sent_unconfirmed', never silently retried.
    store.write(dispute_id, package_sha, {"state": "sent_unconfirmed", "request_id": request_id,
                                          "request_input": req_input,
                                          "attachment_filenames": attachment_filenames,
                                          "attachment_sha256": attachment_shas})

    link = norm["action_links"]["provide-evidence"]
    path = link["href"][len(f"https://{ALLOWED_SEND_HOST}"):]
    multipart = build_multipart(req_input, attachments)
    try:
        post_error = None
        post_status_ok = False
        with guard as reservation:
            active_reservation = config.feature_enabled("active_approval")
            if active_reservation:
                store.close()
            try:
                client.call("POST", path, "provide_evidence", multipart=multipart, request_id=request_id,
                            log_extra={"request_input": req_input,
                                      "attachments": attachment_filenames,
                                      **write_audit(package_sha, approval["approval_sha256"])})
                post_status_ok = True
            except (PayPalError, httpx.TransportError) as exc:
                post_error = exc
            finally:
                if active_reservation:
                    # Restore this object for reconciliation and the caller's existing close.
                    store.__init__(db_path)
        if reservation is not None and reservation.changed and (
                post_error is None or isinstance(post_error, httpx.TransportError) or classify(post_error) == "retryable"):
            if post_error is not None:
                detail = {"package_id": package_id, "error": type(post_error).__name__}
                if isinstance(post_error, PayPalError):
                    detail.update(http_status=post_error.status, issue=post_error.issue)
                repo.log_action(db_path, "system", "send_outcome_unknown", dispute_id, detail)
            changed_result = reconcile_after_send(client, store, dispute_id, package_sha, req_input,
                                                  attachment_filenames=attachment_filenames)
            unknown = SendOutcome(202, "Outcome unknown, review required",
                                  "The send outcome is unknown and a source changed while sending. "
                                  f"Changed: {reservation.change_description}. "
                                  "Review and check PayPal's record; nothing will be sent again automatically.")
            if changed_result.get("abandoned"):
                return unknown if post_error is not None else _sent_review_required(reservation.change_description)
            if changed_result["confirmed"]:
                # PayPal's own read-back confirms the send, so that is the headline. What changed while
                # sending (the approval is already invalidated; nothing is re-sent) is a separate, secondary note.
                repo.log_action(db_path, "system", "send_confirmed", dispute_id,
                                {"package_id": package_id, "details": changed_result["details"],
                                 "changed_after_sending": True})
                return SendOutcome(200, RECEIVED_MESSAGE, "PayPal now shows this evidence as received.",
                                   technical=str(changed_result["details"]), sent=True,
                                   note=reservation.note())
            if changed_result.get("files_ok") is False:
                # PayPal lists the statements but not an attached file: that is the partial outcome
                # (the same one every other page shows), with what changed as the secondary note.
                missing = changed_result.get("missing_files") or []
                return SendOutcome(206, partial_message(missing),
                                   "The shipment details were received, but at least one attached file was not. "
                                   "Nothing was sent again automatically.",
                                   technical=f"missing: {missing}\n{changed_result['details']}",
                                   note=reservation.note())
            if post_error is not None:
                return unknown
            return _sent_review_required(reservation.change_description)
        if post_error is not None:
            raise post_error
    except review_errors as e:
        store.write(dispute_id, package_sha, {"state": "blocked", "reason": "active review required"})
        return SendOutcome(409, "Not sent", str(e))
    except PayPalError as e:
        if classify(e) != "retryable":
            # A confirmed, known rejection (e.g. a validation error) - nothing was received.
            store.write(dispute_id, package_sha, {"state": "failed", "request_id": request_id,
                                                  "http_status": e.status, "request_input": req_input})
            repo.log_action(db_path, "system", "send_failed", dispute_id,
                            {"package_id": package_id, "http_status": e.status, "issue": e.issue})
            return SendOutcome(502, "Not sent",
                               "PayPal rejected this evidence. Review the technical details below, fix "
                               "the content if needed, and try again.",
                               technical=f"status={e.status} issue={e.issue} debug_id={e.debug_id}")
        # A 5xx/429 is ambiguous - PayPal may have processed it anyway. Treat exactly
        # like a timeout below: stays 'sent_unconfirmed', reconciled by GET, never re-POSTed here.
        repo.log_action(db_path, "system", "send_outcome_unknown", dispute_id,
                        {"package_id": package_id, "http_status": e.status, "issue": e.issue})
    except httpx.TransportError as e:
        repo.log_action(db_path, "system", "send_outcome_unknown", dispute_id,
                        {"package_id": package_id, "error": type(e).__name__})

    # Bounded reconciliation, never an automatic resend. Reached after a clean 2xx, AND
    # after a retryable PayPalError, AND after a transport error - all three are "uncertain, must confirm
    # via GET" outcomes, never "definitely not sent" (that is the non-retryable branch above, which
    # already returned).
    result = reconcile_after_send(client, store, dispute_id, package_sha, req_input,
                                  attachment_filenames=attachment_filenames)
    if result.get("abandoned"):
        # reconcile_after_send's own downgrade guard found this exact package_sha
        # abandoned by a concurrent confirm-not-received call (on a DIFFERENT package) while this send's
        # own POST was still being reconciled. When the POST itself was answered 2xx, "Not sent" would be
        # false (the request reached PayPal); say it was sent and needs a look. After a retryable error or a transport
        # error the outcome is simply unknown, as at the claim-conflict branch above.
        if post_status_ok:
            return _sent_review_required()
        return SendOutcome(409, "Outcome pending", "This exact package was marked as not received. The send result is "
                           "unknown; use Check again.")
    if result["confirmed"]:
        repo.log_action(db_path, "system", "send_confirmed", dispute_id,
                        {"package_id": package_id, "details": result["details"]})
        return SendOutcome(200, RECEIVED_MESSAGE, "PayPal now shows this evidence as received.",
                           technical=str(result["details"]), sent=True)
    if result["files_ok"] is False:
        # Content landed but at least one attached file did not (exactly what a wrong
        # multipart field name would produce) - a known, specific gap, not the generic ambiguity below.
        repo.log_action(db_path, "system", "send_missing_attachment", dispute_id,
                        {"package_id": package_id, "missing_files": result["missing_files"]})
        return SendOutcome(206, partial_message(result["missing_files"]),
                           "The shipment details were received, but at least one attached file was not. "
                           "Nothing was sent again automatically.",
                           technical=f"missing: {result['missing_files']}\n{result['details']}")
    repo.log_action(db_path, "system", "send_result_pending", dispute_id, {"package_id": package_id})
    return SendOutcome(202, PENDING_MESSAGE,
                       "The request was sent, but three checks (~5s, ~15s, ~30s apart) did not yet show "
                       "it in this dispute's record. PayPal may take a moment to show it - check "
                       "again in a bit; nothing will be sent again automatically.")


def _received_banner(attempt: dict) -> str:
    """'Received by PayPal on 16 Oct 2026, 10:10 pm (Mexico City time)' (the package page's display format).
    attempt_store._now() stamps the machine's own local time with its real UTC offset, so the stamp is an instant and
    timefmt.display converts it to Mexico City time, whatever machine wrote it. timefmt is imported at call time, the
    way web.status is above: this module has no import of the web layer at load. A stamp that cannot be read gives
    the plain 'Received by PayPal', never a made-up time."""
    from copilot.web import timefmt
    when = timefmt.display(attempt.get("ts"))
    return f"Received by PayPal on {when}" if when and when != "Not available" else "Received by PayPal"


def send_status_banner(db_path, dispute_id: str, package: dict | None, claims: list,
                       approved: bool = False) -> str | None:
    """A persistent banner on the review screen, read from the send-attempt registry
    and the last GET it recorded - never from which button happens to be on screen, so it survives
    closing the browser and coming back later. Recomputes the package's CURRENT package_sha the exact
    same way send_package() above does, so an attempt only ever matches when the content right now is
    byte-for-byte what was actually sent; an edit since the last send correctly shows "Prepared"/"Approved"
    again for the new content, never a stale status for the old one. Public (moved from copilot/web/
    routes_package.py's own _send_status_banner 2026-10-05) so copilot/mcp_server.py can call it directly
    instead of reaching into another module's private name.

    `approved` is whether the caller already has a non-invalidated approval row for this package (a soft distinction for
    this banner only - it does not need to be fingerprint-fresh the way an actual send does; a package
    pending re-approval after an edit still reads more usefully as "Approved" here than reverting all the
    way to "Prepared"). "Sending..." (the moment a POST is literally in flight) has no caller here - there
    is no live/polling channel in this app to observe that instant from a registry row read after the
    fact; it stays a front-end-only concern, not implemented."""
    if package is None:
        return None
    # the attempt of THIS package, found through the audit entry that registered it (the same mapping
    # as copilot/web/status.py's send_record), so a later edit of the shared shipment details never turns a sent
    # version back into "Prepared"; the content sha is only the fallback for an attempt with no such entry.
    attempt = pick_attempt(package_attempts(db_path, dispute_id, package))
    if attempt is None:
        return "Approved" if approved else "Prepared"
    if attempt["state"] == "confirmed":
        return _received_banner(attempt)
    if attempt["state"] in ("sent_unconfirmed", "pending"):
        return "Result pending — check again"
    if attempt["state"] == "blocked":
        # a local refusal (no link yet, a failed precondition check), never PayPal's own rejection.
        return f"Not sent: {attempt.get('reason') or 'blocked'}"
    if attempt["state"] == "failed":
        return "Failed (confirmed)"
    if attempt["state"] == "abandoned":
        # a human explicitly confirmed PayPal's Resolution Center did not show this content, for
        # THIS same package_sha (an edge case - abandonment normally targets an OTHER package's attempt).
        # An attempt marked while PayPal listed its statements without a file keeps the sentence its own reason states.
        if attempt.get("reason") == ABANDONED_PARTIAL_REASON:
            return ("Marked as not received: when Proofie checked, PayPal's Resolution Center showed the statements "
                    "without the attached file")
        return "Not sent: marked abandoned, PayPal's Resolution Center did not show it"
    return "Not sent yet"
