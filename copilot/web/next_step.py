"""The one 'what do I do now' panel of a page.

DISPLAY ONLY. This module builds a description of the next step from facts the caller already read (the dispute
PayPal returned, stored rows, the send record from copilot.web.status). It has no POST handler, performs no write
and calls nothing. An Action only names a URL that an existing, separately guarded route serves: that route
re-checks everything itself. Nothing under copilot/core or copilot/features imports this module, and no Action it
returns posts to a send, register or resolution-register route from the dispute page (for_dispute); the single
send button of a package page (for_package, key 'send') posts to that page's own guarded /send route.

Copy is exact on purpose: public documents quote some of these labels (see presentation.LARGE_LABELS).
"""
from __future__ import annotations

from dataclasses import dataclass

from copilot import config
from copilot.core import send_service
from copilot.core.send import DEMO_PREFIX
from copilot.web import timefmt
from copilot.web.dispute_state import buyer_closed_raw
from copilot.web.presentation import LARGE_LABELS, READS_ONLY_NOTE
from copilot.web.status import abandoned_partial, effective_state, is_settling


@dataclass(frozen=True)
class Action:
    label: str
    href: str | None = None             # GET navigation: a link
    post: str | None = None             # POST target: a form with one button (guarded by that route)
    kind: str = "primary"               # primary | secondary
    large: bool = True
    sending: str | None = None
    sends: bool = False                 # True only for the single control that writes to PayPal
    hidden: str | None = None
    fields: tuple = ()


@dataclass(frozen=True)
class FixItem:
    text: str
    link: str
    href: str


@dataclass(frozen=True)
class NextStep:
    key: str
    heading: str
    body: str | None = None
    items: tuple = ()
    action: Action | None = None
    secondary: tuple = ()
    note: str | None = None
    data_state: str | None = None
    note_state: str | None = None
    preview_text: str | None = None          # exact text drawn above the actions (the send step restates what PayPal receives)
    preview: tuple = ()                      # (label, value) rows drawn under it
    technical: tuple = ()                    # (label, value) rows under a closed "Technical details" fold
    action_hint: str | None = None           # one line after the actions ("Editing clears your approval.")


def _act(label, *, href=None, post=None, kind="primary", sending=None, sends=False, hidden=None, fields=()):
    return Action(label, href=href, post=post, kind=kind, large=kind == "primary" or label in LARGE_LABELS,
                  sending=sending, sends=sends, hidden=hidden, fields=tuple(fields))


def _links(norm) -> dict:
    links = norm.get("action_links") if isinstance(norm, dict) else None
    return links if isinstance(links, dict) else {}


def _can_provide_evidence(norm) -> bool:
    return send_service.provide_evidence_offered(norm)


def _version(package) -> str:
    return str((package or {}).get("version", "")) if package else ""


def _state(record) -> str:
    """The record's state as the seller should see it (copilot.web.status.effective_state): a confirmed send whose
    attached files no longer match what PayPal received is `later_file`, never pending."""
    return effective_state(record)


# One helper for the grammar of every later_file sentence: was/were, is/are, it/them. Pages, the
# check result and the dispute summary all call these, so two files never read as "was added ... will not send it".
def _later_names(record) -> list:
    return [n for n in (getattr(record, "missing_files", ()) or ()) if n]


def _join_phrases(items: list) -> str:
    text = ", ".join(items)
    return text[:1].upper() + text[1:] if text.startswith("the ") else text


def later_file_text(record) -> str:
    """'b.pdf was added afterwards and is not in PayPal' / 'a.pdf, b.pdf were added afterwards and are not in PayPal'.
    A file with the name of one that was sent and replaced is told apart ('The label.pdf attached now ...')."""
    names = _later_names(record)
    if not names:
        return "A file was added afterwards and is not in PayPal"
    many = len(names) > 1
    removed = {n for n in (getattr(record, "removed_files", ()) or ()) if n}
    shown = [f"the {n} attached now" if n in removed else n for n in names]
    return f"{_join_phrases(shown)} {'were' if many else 'was'} added afterwards and {'are' if many else 'is'} not in PayPal"


def will_not_send_text(record) -> str:
    """Proofie never sends an added file on its own (the file was never sent, so never 'again')."""
    return "Proofie will not send them on its own." if len(_later_names(record)) > 1 else "Proofie will not send it on its own."


def removed_text(record) -> str:
    """A file that was sent and is no longer attached. 'is in PayPal' only for a file PayPal's read listed;
    a file PayPal does not list, or that cannot be tied to one, 'was sent'. A name that a file attached now also has is
    qualified ('The earlier label.pdf ...')."""
    names = [n for n in (getattr(record, "removed_files", ()) or ()) if n]
    if not names:
        return ""
    unlisted = set(getattr(record, "removed_unlisted", ()) or ())
    later = set(_later_names(record)) if _state(record) == "later_file" else set()
    listed = [f"the earlier {n}" if n in later else n for n in names if n not in unlisted]
    sent_only = [f"the earlier {n}" if n in later else n for n in names if n in unlisted]
    parts = []
    if listed:
        many = len(listed) > 1
        parts.append(f"{_join_phrases(listed)} {'are' if many else 'is'} in PayPal and no longer attached here.")
    if sent_only:
        many = len(sent_only) > 1
        parts.append(f"{_join_phrases(sent_only)} {'were' if many else 'was'} sent and {'are' if many else 'is'} "
                     "no longer attached here.")
    return " ".join(parts)


def _later_file_body(n, when, record, *, listed: bool = False) -> str:
    removed = removed_text(record)
    return (f"PayPal's record showed package v{n}" + (f" at {when}" if when else "") + ". "
            + later_file_text(record) + "." + ("" if listed else " " + will_not_send_text(record))
            + (f" {removed}" if removed else ""))


NOT_ASKING_NOTE = send_service.NOT_ASKING_NOTE
ABANDONED_HEADING = "You marked the earlier send as not received."          # the one heading of both pages
# Proofie never sends evidence for a dispute where PayPal already lists evidence submitted by the seller (the send path
# refuses it before any POST). Every page that would otherwise promise a further send says this instead.
ONCE_PER_DISPUTE = send_service.ONCE_PER_DISPUTE
EVIDENCE_LISTED = "evidence-listed"        # the data-state of every step that says the rule (the page adds the link)


def seller_evidence_listed(norm) -> bool:
    """True when PayPal lists evidence submitted by the seller for this dispute; a missing key counts as False."""
    return bool(isinstance(norm, dict) and norm.get("evidence_submitted_by_seller"))

PARTIAL_HEADING = "Statements in PayPal; a file is missing."
PARTIAL_HEADING_MANY = "Statements in PayPal; files are missing."


def partial_names(record) -> list:
    """The files PayPal does not list for a true partial send (from the record, else the attempt's stored reason)."""
    named = [n for n in (getattr(record, "missing_files", ()) or ()) if n]
    if named:
        return named
    reason = str(((getattr(record, "attempt", None) or {}).get("reason")) or "")
    prefix = "missing_attachment:"
    return [n for n in reason[len(prefix):].split(",") if n] if reason.startswith(prefix) else []


# One count-aware helper for the partial sentences: file/files, it/them, is/are, on every page.
def partial_heading(names) -> str:
    return PARTIAL_HEADING_MANY if len(names) > 1 else PARTIAL_HEADING


def partial_files_text(names, *, labelled: bool = True) -> str:
    """'the file b.pdf' / 'the files a.pdf, b.pdf' / 'every file' (no name known)."""
    if not names:
        return "every file"
    label = ("the files " if len(names) > 1 else "the file ") if labelled else ""
    return f"{label}{', '.join(names)}"


def partial_missing_text(names, *, labelled: bool = False) -> str:
    """'When Proofie checked, PayPal showed your statements without b.pdf.' (without the labels) / '... without the files
    a.pdf, b.pdf.' (labelled). Told from the record of that check, never as what PayPal shows now."""
    return f"When Proofie checked, PayPal showed your statements without {partial_files_text(names, labelled=labelled)}."


def partial_wont_text(names) -> str:
    """Proofie sent the files once; it will not send them again on its own."""
    return "Proofie will not send them again on its own." if len(names) > 1 else "Proofie will not send it again on its own."


def partial_text(names, *, labelled: bool = False) -> str:
    return f"{partial_missing_text(names, labelled=labelled)} {partial_wont_text(names)}"


SEND_FINAL_SENTENCE = "Sending to PayPal cannot be undone; Proofie sends it once."
STOP_BODY = f"{ONCE_PER_DISPUTE} Nothing more can be sent from Proofie for this dispute."
# The way out for a true partial send while PayPal lists no seller evidence (a send is still possible there): it names the
# controls the package page really has (the tick box and "Start a new submission" under "Why is sending blocked?").
PARTIAL_COLLISION_ADVICE = ("Change a statement in this package (for example add one sentence), then tick the box under "
                            "Why is sending blocked? and press Start a new submission, then press Send evidence to PayPal.")
ABANDONED_PARTIAL_WAY_OUT = "To send this content, change it first: that makes a new version to review and approve."


def partial_listed_text(names) -> str:
    """What PayPal shows of a true partial send, count-aware, and the rule: the one wording of the package page, its banner
    and the dispute page while PayPal lists evidence from the seller."""
    return f"{partial_missing_text(names, labelled=True)} {ONCE_PER_DISPUTE}"


def _again(check_post, check_href, package_url):
    if check_post:
        return (_act("Check again", post=check_post, sending="Checking PayPal…", kind="secondary"),)
    return (_act("Check again", href=check_href or package_url, kind="secondary"),)
PARTIAL_COLLISION_HEADING = "PayPal showed these statements without every file."
PARTIAL_PAUSED_HEADING = "Sending is paused: an earlier send reached PayPal without every file."


def can_provide(norm) -> bool:
    """PayPal offers the seller a provide-evidence action on this read."""
    return _can_provide_evidence(norm)


def _tail(listed: bool, offered: bool):
    """What replaces a way out when no send is possible: the rule while the read lists seller evidence, otherwise (no
    provide-evidence action) that PayPal is not asking. None while a send is possible. The send core's own texts use the
    same function."""
    return send_service.no_way_out(listed, offered)


def _pending_step(record, names, *, listed: bool, check, note=None) -> NextStep:
    """The package's own send that PayPal has not confirmed (or confirmed only in part), on both pages."""
    state = _state(record)
    if state == "partial":
        body = partial_listed_text(names) if listed else partial_text(names)
        heading = partial_heading(names)
    else:
        body = ONCE_PER_DISPUTE if listed else "Proofie will not send it again on its own."
        heading = "Send attempted - PayPal has not confirmed it yet."
    return NextStep("pending", heading, body, action=check, data_state=EVIDENCE_LISTED if listed else None,
                    note=note or READS_ONLY_NOTE)


def _other_pending_step(*, listed: bool, offered: bool, earlier_partial: bool, why_href: str, check) -> NextStep:
    """An earlier version's send that PayPal has not confirmed pauses sending, on both pages. The way out (the mark under
    "Why is sending blocked?") exists only while a send is possible."""
    if listed:
        heading = ("An earlier send reached PayPal without every file." if earlier_partial
                   else "An earlier send is not confirmed yet.")
    else:
        heading = PARTIAL_PAUSED_HEADING if earlier_partial else "Sending is paused: an earlier send is not confirmed yet."
    item = (FixItem("An earlier send reached PayPal without every file." if earlier_partial
                    else "The earlier send is not confirmed.", "Why is sending blocked?", why_href),) if why_href else ()
    tail = _tail(listed, offered)
    if tail is not None:
        return NextStep("other_pending", heading, tail, items=item, action=check,
                        data_state=EVIDENCE_LISTED if listed else None)
    if earlier_partial:
        body = "Check PayPal first. Then follow the steps under “Why is sending blocked?”."
    else:
        body = ("Check PayPal first. If PayPal does not show the earlier send, follow the steps under "
                "“Why is sending blocked?”.")
    return NextStep("other_pending", heading, body, items=item, action=check)


def _claim_under_review(view) -> NextStep:
    return NextStep("claim_under_review", "PayPal is reviewing this claim.", "Proofie offers no seller action while the review lasts.",
                    secondary=(view,))


def _collision_step(collision, *, package_url: str, check_href: str | None = None, check_post: str | None = None,
                    listed: bool = False, offered: bool = True) -> NextStep:
    """The statements of this version are already in PayPal (or may be) from an earlier version: the send would be
    refused, so there is no Send button, only the way out. While no send is possible (`listed`: the read lists evidence from
    the seller; not `offered`: PayPal offers no provide-evidence action) there is no way out and the step says why; when a
    send is possible the earlier way out (change a statement, mark, send) stays. What PayPal held is told from the record."""
    where = f"package v{collision.earlier_version}" if collision.earlier_version else "an earlier send"
    names = ", ".join(n for n in collision.unsent if n)
    other = f" Not sent: {names}." if names else " This version carries a different set of files than that send."
    again = _again(check_post, check_href, package_url)
    reads = "Reads the dispute from PayPal. Sends nothing to PayPal."
    change = _act("Change a statement", href=f"{package_url}#statements")
    view = _act("View the dispute and PayPal's answer", href=package_url.rsplit("/packages/", 1)[0])
    tail = _tail(listed, offered)
    data_state = EVIDENCE_LISTED if listed else None
    if collision.state == "partial":
        gone = partial_files_text([n for n in collision.missing if n])
        shown = f"When Proofie checked, PayPal showed the statements of {where} without {gone}."
        if tail is not None:
            return NextStep("same_statements", PARTIAL_COLLISION_HEADING, f"{shown} {tail}", data_state=data_state,
                            secondary=again, note=reads)
        return NextStep("same_statements", PARTIAL_COLLISION_HEADING,
                        f"{shown} Its latest read does not list them. Proofie will not send them again with another "
                        f"file. {PARTIAL_COLLISION_ADVICE}", action=change, secondary=again, note=reads)
    if collision.same_files:
        # the same statements and the same files are the same submission, not a second one.
        if collision.state == "confirmed":
            if tail is not None:
                return NextStep("same_statements", "PayPal already has this exact package.", f"Nothing to send. {tail}",
                                data_state=data_state, action=view)
            return NextStep("same_statements", "PayPal already has this exact package.",
                            "Nothing to send. A changed statement makes a new submission.", action=change)
        head = f"{where[:1].upper()}{where[1:]} has the same statements and the same files, and PayPal has not confirmed it."
        if tail is not None:
            return NextStep("same_statements", "An earlier send of this exact package is not confirmed.",
                            f"{head} {tail}", data_state=data_state, secondary=again, note=reads)
        return NextStep("same_statements", "An earlier send of this exact package is not confirmed.",
                        f"{head} PayPal may already have it. Proofie will not send it again. "
                        + send_service.UNCONFIRMED_COLLISION_ADVICE, action=change, secondary=again, note=reads)
    if collision.state == "confirmed":
        held = f"When Proofie checked, PayPal held these statements from {where}, which carried other files.{other}"
        if tail is not None:
            return NextStep("same_statements", "PayPal held these statements.", f"{held} {tail}",
                            data_state=data_state, action=view)
        return NextStep("same_statements", "Next step: change a statement.",
                        f"{held} Its latest read does not list them. Proofie cannot send them again with another file.",
                        action=change, note="A changed statement makes a new submission; the earlier send stays as it is.")
    sent_before = f"These statements were sent before with {where} and PayPal has not confirmed them."
    if tail is not None:
        return NextStep("same_statements", "An earlier send of these statements is not confirmed.",
                        f"{sent_before}{other} {tail}", data_state=data_state, secondary=again, note=reads)
    return NextStep("same_statements", "An earlier send of these statements is not confirmed.",
                    f"{sent_before} PayPal may already have them.{other} Proofie will not send them again with another "
                    "file. " + send_service.UNCONFIRMED_COLLISION_ADVICE, action=change,
                    # "Check again" must ask PayPal: a POST to the package /check route, or a GET of
                    # the dispute page (which reconciles); never a plain reload of the package page.
                    secondary=again, note=reads)


def _unconfirmed(collision) -> bool:
    """The earlier send behind a collision is not confirmed (attempted, or partial)."""
    return getattr(collision, "state", None) in ("attempted", "partial")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ------------------------------------------------------------------------------------ the dispute page
def for_dispute(*, dispute_id, norm, raw=None, has_documents, latest_package=None, package_claims=0,
                package_has_tracking=False, approval_valid=False, record=None, auto_prep_status=None,
                hosted=False, collision=None, other_unconfirmed=False, earlier_partial=False) -> NextStep:
    norm = norm if isinstance(norm, dict) else {}
    base = f"/disputes/{dispute_id}"
    listed = seller_evidence_listed(norm)
    offered = _can_provide_evidence(norm)
    if buyer_closed_raw(raw):
        return NextStep("buyer_closed", "The buyer closed this case.",
                        "PayPal offers no seller action, so there is nothing to do.")
    if norm.get("status") == "UNDER_REVIEW" and norm.get("stage") == "CHARGEBACK" and config.feature_enabled("outcome"):
        return _claim_under_review(_act("Read PayPal's answer", href=f"{base}/outcome", kind="secondary"))
    if is_settling(norm):
        return NextStep("settling", "PayPal is updating this dispute.",
                        "This usually takes about a minute after a change. Nothing is sent from this page.",
                        action=_act("Check again", href=base))
    state = _state(record)
    n = _version(latest_package)
    package_url = f"{base}/packages/{(latest_package or {}).get('id')}"
    if state == "later_file":
        when = timefmt.display(getattr(record, "confirmed_at", None))
        tail = _tail(listed, offered)
        return NextStep("later_file", "Evidence sent to PayPal; a file was added afterwards.",
                        _later_file_body(n, when, record, listed=listed),
                        action=_act("Open the package", href=package_url),
                        note=tail if tail is not None
                        else "Prepare a new version to send it; PayPal's record of the sent version does not change.",
                        data_state=EVIDENCE_LISTED if listed else None)
    if state in ("reserved", "attempted", "partial"):
        return _pending_step(record, partial_names(record), listed=listed, check=_act("Check again", href=base),
                             note="Reads the dispute from PayPal. Sends nothing to PayPal.")
    if state != "confirmed":
        if latest_package is not None and collision is not None and _unconfirmed(collision) and state == "none":
            return _collision_step(collision, package_url=package_url, check_href=base, listed=listed, offered=offered)
        if other_unconfirmed:
            return _other_pending_step(listed=listed, offered=offered, earlier_partial=earlier_partial,
                                       why_href=f"{package_url}#why-blocked" if latest_package is not None else None,
                                       check=_act("Check again", href=base))
    # A confirmed latest version is read-only. When PayPal lists provide-evidence again, the required steps reopen (as
    # the dispute page's task rows do): the next step is a new version (upload, generate), never this one again.
    reopened = state == "confirmed" and offered and not listed
    if state == "confirmed" and not reopened:
        when = timefmt.display(getattr(record, "confirmed_at", None))
        package_text = f"package v{n}" if n else "the package"
        body = f"Proofie confirmed {package_text} in PayPal's record" + (f" at {when}." if when else ".")
        if removed_text(record):
            body += f" {removed_text(record)}"
        links = _links(norm)
        if ("make-offer" in links or "send-message" in links) and config.feature_enabled("resolution"):
            return NextStep("confirmed", "Evidence sent to PayPal.", body,
                            action=_act("Resolution options and buyer messages", href=f"{base}/resolution"),
                            note="PayPal still offers you an action on this dispute.")
        if offered:
            return NextStep("confirmed", "Evidence sent to PayPal.", body, note=ONCE_PER_DISPUTE,
                            data_state=EVIDENCE_LISTED)
        return NextStep("confirmed", "Evidence sent to PayPal.", body,
                        note="PayPal does not ask you for anything else right now.")
    if not offered:
        return NextStep("no_action", "PayPal is not asking for evidence on this dispute now.",
                        "Your packages stay readable below.")
    if state == "abandoned" and latest_package is not None:
        if abandoned_partial(record):
            return NextStep("abandoned", ABANDONED_HEADING,
                            f"When Proofie checked, PayPal showed the statements of package v{n} without the file. "
                            + (ONCE_PER_DISPUTE if listed else ABANDONED_PARTIAL_WAY_OUT),
                            data_state=EVIDENCE_LISTED if listed else None, action=_act("Open the package", href=package_url))
        if not listed:             # listed: the stop step below says the rule, as on the package page
            return NextStep("abandoned", ABANDONED_HEADING,
                            f"You told Proofie that package v{n} was not in PayPal's Resolution Center. To send this "
                            "content, change it first: that makes a new version to review and approve.",
                            action=_act("Open the package", href=package_url))
    if listed and state in ("none", "refused", "blocked", "abandoned"):
        # Nothing new can be sent while PayPal lists evidence from the seller: no upload, generate, approve or send step.
        # The same step as the package page: a confirmed collision first, otherwise the stop step.
        if collision is not None and state == "none" and latest_package is not None:
            return _collision_step(collision, package_url=package_url, check_href=base, listed=True)
        return NextStep("evidence_listed", "PayPal already has evidence from you.", STOP_BODY, data_state=EVIDENCE_LISTED,
                        secondary=((_act("Open the package", href=package_url, kind="secondary"),) if latest_package else ()))
    if auto_prep_status == "preparing":
        return NextStep("preparing", "Proofie is preparing a draft package.",
                        "This can take a few minutes. Reload this page to see it.",
                        secondary=(_act("Reload this page", href=base, kind="secondary"),))
    if latest_package is None or reopened:
        manual = _act("Start manually instead", post=f"{base}/packages?manual=1", kind="secondary")
        if not has_documents:
            return NextStep("upload", "Next step: upload the carrier receipt or shipping label.",
                            "The checks compare it with PayPal's data. Nothing is sent.",
                            action=_act("Upload a document", href=f"{base}/workspace/prepare#documents"), secondary=(manual,))
        note = ("Preparing the package. Keep this page open." if hosted
                else "Preparing with the assistant. This can take a few minutes. Keep this page open.")
        body = (f"PayPal asks for evidence again. Package v{n} stays as it was sent. Nothing is sent." if reopened
                else "Nothing is sent.")
        return NextStep("generate", "Next step: prepare the evidence package.", body,
                        action=_act("Generate evidence package", post=f"{base}/packages", sending="Preparing…"),
                        secondary=(manual,), note=note, note_state="generate-wait")
    pid = latest_package.get("id")
    package_url = f"{base}/packages/{pid}"
    if package_claims < 1 or not package_has_tracking:
        return NextStep("finish_package", f"Next step: finish evidence package v{n}.",
                        "Add the tracking number and at least one statement. Nothing is sent until you approve and send.",
                        action=_act(f"Continue with package v{n}", href=f"{package_url}#next-step"))
    if collision is not None and state == "none":
        return _collision_step(collision, package_url=package_url, check_href=base, listed=listed)
    if not approval_valid:
        href = f"{package_url}/active-approval" if config.feature_enabled("active_approval") else f"{package_url}#next-step"
        return NextStep("review", "Next step: check each statement and approve the package.",
                        f"Package v{n} is ready to review. Approving sends nothing.",
                        action=_act("Review each statement and approve", href=href))
    return NextStep("send", "Next step: send the approved package to PayPal.",
                    f"You approved package v{n}. Nothing has reached PayPal yet.",
                    action=_act("Continue to send evidence", href=f"{package_url}#next-step"))


# ------------------------------------------------------------------------------------ the package page
def for_package(*, dispute_id, package, norm, claims_count, files_count, has_tracking, carrier_missing,
                approval_valid, record, other_unconfirmed, review_flags=(), hosted=False,
                approval_stale=False, collision=None, earlier_partial=False, send_notes=None, send_tracking=None,
                send_files=(), send_fingerprint=None, conflict_line=None) -> NextStep:
    """`approval_stale` (optional, added to the contract's signature) is True when an approval existed and the
    content changed after it; it only selects the sentence of the review step. `earlier_partial` is True
    when every earlier unconfirmed send reached PayPal without a file: it only selects the words of the paused step."""
    norm = norm if isinstance(norm, dict) else {}
    base = f"/disputes/{dispute_id}"
    pid = (package or {}).get("id")
    url = f"{base}/packages/{pid}"
    n = _version(package)
    state = _state(record)
    listed = seller_evidence_listed(norm)
    offered = _can_provide_evidence(norm)
    if norm.get("status") == "UNDER_REVIEW" and norm.get("stage") == "CHARGEBACK" and config.feature_enabled("outcome"):
        # checked before the settling wait, as on the dispute page: a claim under review takes nothing from the seller
        return _claim_under_review(_act("Read PayPal's answer", href=f"{base}/outcome", kind="secondary"))
    if is_settling(norm):
        return NextStep("settling", "PayPal is updating this dispute.",
                        "Wait about a minute, then check again. Nothing can be sent while PayPal updates the dispute.",
                        action=_act("Check again", href=url))
    if state == "confirmed":
        when = timefmt.display(getattr(record, "confirmed_at", None))
        body = (f"PayPal's record showed package v{n}" + (f" at {when}" if when else "")
                + ", when Proofie checked. This version is read-only."
                + (f" {removed_text(record)}" if removed_text(record) else ""))
        secondary, note, data_state = (), None, None
        if listed:
            note, data_state = ONCE_PER_DISPUTE, EVIDENCE_LISTED
        elif offered:
            secondary = (_act("Prepare a new version", post=f"{base}/packages", kind="secondary"),)
            note = "A new version is a new submission to PayPal. The sent version stays as it is."
        return NextStep("confirmed", "Evidence sent to PayPal.", body,
                        action=_act("View the dispute and PayPal's answer", href=base), secondary=secondary, note=note,
                        data_state=data_state)
    if state == "later_file":
        when = timefmt.display(getattr(record, "confirmed_at", None))
        tail = _tail(listed, offered)
        secondary = ()
        if tail is None:
            secondary = (_act("Prepare a new version", post=f"{base}/packages", kind="secondary"),)
            tail = "A new version is a new submission to PayPal. The sent version stays as it is."
        return NextStep("later_file", "Evidence sent to PayPal; a file was added afterwards.",
                        _later_file_body(n, when, record, listed=listed),
                        action=_act("View the dispute and PayPal's answer", href=base), secondary=secondary, note=tail,
                        data_state=EVIDENCE_LISTED if listed else None)
    if state in ("reserved", "attempted", "partial"):
        return _pending_step(record, partial_names(record), listed=listed,
                             check=_act("Check again", post=f"{url}/check", sending="Checking PayPal…"))
    if collision is not None and _unconfirmed(collision) and state == "none":
        # the earlier unconfirmed attempt has the same statements as this package, so the mark
        # cannot act on it from here: the way out is to change a statement first (UNCONFIRMED_COLLISION_ADVICE).
        return _collision_step(collision, package_url=url, check_post=f"{url}/check", listed=listed, offered=offered)
    if other_unconfirmed:
        return _other_pending_step(listed=listed, offered=offered, earlier_partial=earlier_partial,
                                   why_href="#why-blocked",
                                   check=_act("Check again", post=f"{url}/check", sending="Checking PayPal…"))
    if norm and not offered:
        # PayPal offers no provide-evidence action (or the buyer closed the case): the page shows no
        # Send form, the same answer the dispute page gives for the same read.
        return NextStep("no_action", "PayPal is not asking for evidence on this dispute now.",
                        "Nothing can be sent from this page. The package stays readable.",
                        secondary=(_act("View the dispute", href=base, kind="secondary"),))
    if state == "abandoned" and abandoned_partial(record):
        if listed:
            return NextStep("abandoned", ABANDONED_HEADING,
                            f"When Proofie checked, PayPal showed its statements without the file. {ONCE_PER_DISPUTE}",
                            data_state=EVIDENCE_LISTED)
        return NextStep("abandoned", ABANDONED_HEADING,
                        f"When Proofie checked, PayPal showed its statements without the file. {ABANDONED_PARTIAL_WAY_OUT}",
                        secondary=(_act("Edit the statements", href="#statements", kind="secondary"),))
    if state == "abandoned" and not listed:
        return NextStep("abandoned", ABANDONED_HEADING,
                        "To send it, change it first: that makes a new version to review and approve.",
                        secondary=(_act("Edit the statements", href="#statements", kind="secondary"),))
    if collision is not None and state == "none":
        return _collision_step(collision, package_url=url, check_post=f"{url}/check", listed=listed)
    if listed and state in ("none", "refused", "blocked", "abandoned"):
        # Nothing can be sent while PayPal lists evidence from the seller: no approve or send step, only the rule.
        return NextStep("evidence_listed", "PayPal already has evidence from you.", STOP_BODY,
                        data_state=EVIDENCE_LISTED,
                        secondary=(_act("View the dispute", href=base, kind="secondary"),))
    if not approval_valid:
        if conflict_line and (not has_tracking or claims_count < 1):
            # v1.1.2: a check that does not match blocks the approval with or without a statement, so the page says it
            # here instead of asking for a statement or a tracking number first (those two steps hid it)
            return NextStep("conflict_blocks", "Next step: fix the check that does not match.", conflict_line,
                            action=_act("Review the difference", href="#checks"), data_state="conflict-blocks")
        if not has_tracking:
            return NextStep("no_tracking", "Next step: add the tracking number.",
                            "PayPal needs a tracking number before this package can be sent.",
                            action=_act("Add the tracking number", href="#field-tracking_number"))
        if claims_count < 1:
            return NextStep("no_statement", "Next step: add a statement.", "Write what you did, in your own words.",
                            action=_act("Add a statement", href="#statements"))
        active_approval = config.feature_enabled("active_approval")
        body = ("The content changed after you approved it. Review and approve again." if approval_stale
                else "Approving sends nothing. Sending is a separate step." if active_approval
                else "Approving sends nothing. Sending is a separate step. Approval applies only to this package version.")
        heading = "Next step: check each statement and approve the package."
        if active_approval:
            return NextStep("review_approve", heading, body,
                            action=_act("Review each statement and approve", href=f"{url}/active-approval"))
        return NextStep("approve_here", heading, body)
    if not has_tracking or carrier_missing:
        items, first = [], None
        if not has_tracking:
            items += [FixItem("There is no tracking number.", "Type the tracking number", "#field-tracking_number"),
                      FixItem("Or add a document that shows it.", "Add a document", f"{base}/workspace/prepare#documents")]
            first = _act("Add the tracking number", href="#field-tracking_number")
        if carrier_missing:
            items.append(FixItem("There is no carrier.", "Choose the carrier", "#field-carrier"))
            first = first or _act("Choose the carrier", href="#field-carrier")
        return NextStep("cannot_send", "Cannot send yet", items=tuple(items), action=first,
                        data_state=None if has_tracking else "send-needs-tracking")
    body = None
    if state == "refused":
        body = "PayPal did not accept the last attempt. Fix what the result page said before sending again."
    elif state == "blocked":
        reason = send_service.seller_words(str((getattr(record, "attempt", None) or {}).get("reason") or "").strip(),
                                           False).rstrip(".")
        body = ("The last attempt was stopped before it reached PayPal"
                + (f": {reason}." if reason else "."))
    # One plain sentence before the send button, after a refused or blocked attempt too. True: the send path claims the
    # content first and refuses a repeat itself ("Package already received", never a second POST).
    body = f"{body} {SEND_FINAL_SENTENCE}" if body else SEND_FINAL_SENTENCE
    preview_text, preview, technical, secondary = None, (), (), ()
    if send_notes:
        body = f"You are about to send this to PayPal, dispute {dispute_id}. {body}"      # the refused/blocked sentence stays after it
        # her own words first; the line the code adds before them, and the fingerprint, sit one click away (same content)
        prefix = DEMO_PREFIX.strip()
        own = send_notes[len(prefix):].lstrip() if send_notes.startswith(prefix) else send_notes
        preview_text = own or send_notes
        preview = (("Tracking", send_tracking or "not set"),
                   ("Files", ", ".join(send_files) if send_files else "none"))
        technical = ((("PayPal receives this line before your text", prefix),) if preview_text != send_notes else ()) + (
                     ("Fingerprint of the approved package", f"{send_fingerprint[:12]}… (SHA-256)" if send_fingerprint else "not available"),)
        secondary = (_act("Edit the package instead", href="#statements", kind="secondary"),)
    note = (f"Sends package v{n} ({_plural(int(claims_count), 'statement', 'statements')}, "
            f"{_plural(int(files_count), 'file', 'files')}) to PayPal once. Proofie cannot withdraw it.")
    return NextStep("send", "Next step: send the evidence.", body,
                    action=_act("Send evidence to PayPal", post=f"{url}/send", sending="Sending to PayPal…",
                                sends=True),
                    secondary=secondary, preview_text=preview_text, preview=preview, technical=technical,
                    action_hint="Editing clears your approval." if secondary else None, note=note, note_state="send-note")
