"""Prepare and register the seller's two PayPal writes (offer, message), in the only order the sandbox accepts.

Reads and writes go through the injected client (`client.call(method, path, op, ...)`). Every write needs (1) an
approval sha256 equal to the sha256 of the exact body, (2) a FRESH GET showing PayPal's own link for it, (3) a body of
the expected shape. It POSTs once and never retries; a 422 inside the settling window is stored as refused_422. After
a 2xx it waits for the dispute to settle, reads once more and stores what PayPal shows. The escalation and evidence
endpoints are never called from here: "defend" is a link to the existing package page."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from copilot import config
from copilot.web.dispute_state import buyer_closed_raw
from copilot.core.context import SRC_DISPUTE, Limitation, fetch_context
from copilot.core import paypal_words, write_guard
from copilot.core.disputes import PayPalError, normalize, readback_audit, write_audit
from step3_dispute import money_to_minor  # noqa: E402  (pilot/ is on sys.path via copilot.core.disputes)
from copilot.features.account_cliff.rules import HIGH_VOLUME_FEE_CENTS, STANDARD_FEE_CENTS
from copilot.features.resolution import actions as act
from copilot.features.resolution import order, store
from copilot.features.resolution.economics import EconomicsLine, economics_line
from copilot.features.resolution.settle import SettleResult, failure_message, wait_until_settled

ALLOWED_HOST = "api-m.sandbox.paypal.com"
READ_OP, READBACK_OP = "resolution_read", "resolution_readback"
WRITE = {"offer": ("resolution_offer", order.LINK_OFFER, "/make-offer"),
         "message": ("resolution_message", order.LINK_MESSAGE, "/send-message")}
REFUSED_422_TEXT = "PayPal was still updating this dispute; wait a minute and try again"
# Observed sandbox behaviour: after ANY seller write the dispute shows UNDER_REVIEW with only
# `self` for about 15 to 60 s. One GET per page load (and per "Check PayPal again") settles a pending row; there is no
# automatic refresh; after SETTLE_WINDOW_SECONDS (longer than the observed 60 s) a row that is still pending is marked sent_unconfirmed.
REFRESH_SECONDS = 10
SETTLE_WINDOW_SECONDS = 80
# A PayPal listing can lag, and a too-early "absent" would invite a duplicate: a missing message releases its send-once
# key only when the POST is older than the settle window AND at least this old.
RELEASE_MIN_AGE_SECONDS = write_guard.release_min_age(SETTLE_WINDOW_SECONDS)     # max(settle window, 10 minutes)
SETTLE_TIMEOUT_TEXT = f"PayPal is still updating this dispute after {SETTLE_WINDOW_SECONDS} seconds; refresh later"
PENDING_TEXT = "Sent to PayPal; waiting for PayPal to settle this dispute"
MESSAGE_FROM_SELLER = "SELLER"           # raw dispute messages[].posted_by of a message the seller sent
RESERVATION_SLACK_SECONDS = 2            # stored send times are whole seconds, a reservation's is fractional
CLOCK_TOLERANCE_SECONDS = 300            # time_posted is only a tie-breaker, never the proof (clock skew between machines)
ORIGIN_LABELS = {"ai_unedited": "AI text (local model), not edited",
                 "template": "Proofie's fixed template, not edited",
                 "ai_edited": "May include AI text (local model), edited by you",
                 "seller": "Written by you"}
_REQUEST_NS = uuid.UUID("0b8b5c4e-6f4a-4d57-8d0a-3c5f0e2c9a11")
FEE_LEVEL_TEXT = {      # the seller-facing wording of each fee level _fee can answer
    "high-volume fee (account cliff: at or above the threshold)": "high-volume (your account volume is at or above the threshold)",
    "standard fee (account cliff: below the threshold)": "standard (your account volume is below the threshold)",
    "standard fee assumed (account cliff could not be read)": "standard (assumed: Proofie could not read your account volume)",
    "standard fee assumed (account cliff feature off)": "standard (assumed: Proofie could not read your account volume)"}
BUYER_CLOSES_FEE_KNOWN = False       # PayPal's fee pages do not state this fee (checked 2026-10-06)


@dataclass(frozen=True)
class Preview:
    action: str
    body: dict | None
    sha256: str | None
    allowed: bool
    reason: str
    economics: EconomicsLine | None
    next_step_text: str
    limitations: tuple = ()
    origin: str = "seller"            # closed code of ORIGIN_LABELS (computed by the server, outside the hashed body)
    template_text: str = ""


@dataclass(frozen=True)
class Outcome:
    status: str          # rejected_hash | rejected_link | rejected_body | read_failed | already_sent | already_listed | sent_pending_settle | sent_unconfirmed | refused_422 | failed
    http_status: int | None
    readback: dict | None
    settle: SettleResult | None
    limitations: tuple = ()
    request_sha256: str = ""
    reason: str = ""
    existing: dict | None = None     # already_sent: the stored row of the earlier send (None while it is still in flight)
    row_id: int | None = None        # the stored row of this attempt (saved after a POST), for the sent page
    settling: bool = False           # rejected_link: PayPal was updating the dispute (UNDER_REVIEW, no links) when it was read
    technical: str = ""              # failed: the HTTP status and issue code PayPal gave, for Technical details only


@dataclass(frozen=True)
class CheckResult:
    """What one read-only check of PayPal changed. Nothing here ever sends."""
    checked: int = 0                 # rows examined (one GET each)
    confirmed: int = 0               # rows that PayPal's response now supports
    absent: int = 0                  # message rows PayPal's read does not list (a positive absence)
    unchanged: int = 0               # rows that stay as they were (PayPal still updating, or the read failed)
    error: str = ""                  # plain-English text when a read failed
    absent_row_ids: tuple = ()
    released_row_ids: tuple = ()     # the absent rows whose send-once key was released (old enough, normal read)
    offered: int = 0                 # offer rows PayPal read normally and still offers the make-offer option for (part of `unchanged`)

@dataclass(frozen=True)
class PageState:
    norm: dict | None
    actions: order.Actions
    next_step_text: str
    settling: bool
    economics: EconomicsLine | None
    history: list
    limitations: tuple = ()
    pending: bool = False
    template_text: str = ""
    buyer_closed: bool = False  # Display only, from the existing dispute GET; no action/read-back change.
    requested: dict | None = None  # PayPal's own offer.buyer_requested_amount, as read; only the offer form's proposal uses it


class _Recorder:
    """Passes calls through and keeps the Transaction Search answers (for PayPal's eligibility code)."""
    def __init__(self, client):
        self._client, self.search_bodies = client, []

    def call(self, method, path, op, **kw):
        body = self._client.call(method, path, op, **kw)
        if path.startswith("/v1/reporting/transactions"):
            self.search_bodies.append(body)
        return body


def _eligibility(bodies: list, txn_id: str | None) -> str | None:
    for body in bodies:
        rows = (body.get("transaction_details") or []) if isinstance(body, dict) else []
        for row in rows:
            info = row.get("transaction_info") or {}
            if info.get("transaction_id") == txn_id and info.get("protection_eligibility") not in (None, ""):
                return str(info["protection_eligibility"])
    return None


def _read(client, dispute_id: str, op: str = READ_OP, *, log_extra: dict | None = None):
    """(raw, norm, limitations); never raises for an expected PayPal failure."""
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", op,
                          **({"log_extra": log_extra} if log_extra is not None else {}))
    except Exception as exc:  # noqa: BLE001
        msg = failure_message(exc, "read the dispute")
        if msg is None:
            raise
        return None, None, (Limitation(SRC_DISPUTE, msg, paypal_words.technical_of(exc)),)
    return raw, normalize(raw, datetime.now(timezone.utc)), ()


def _readback(raw: dict, norm: dict) -> dict:
    outcome = raw.get("dispute_outcome") if isinstance(raw, dict) else None
    return {"stage": norm.get("stage"), "status": norm.get("status"), "links": sorted(norm.get("action_links") or {}),
            "outcome": outcome.get("outcome_code") if isinstance(outcome, dict) else None}


def _parse_time(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    for candidate in (text.replace("Z", "+00:00"), text):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            try:
                parsed = datetime.strptime(candidate, "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def message_evidence(raw, text: str, sent_at: str | None = None) -> tuple[bool, int]:
    """(listed, count) for one stored message. `listed` only when PayPal's own read lists a message posted
    by the seller whose stripped text equals the stored one; `time_posted` is only a tie-breaker (an equal text that
    PayPal dates clearly before this send, beyond CLOCK_TOLERANCE_SECONDS, is an older message and is not evidence for
    it). `count` is how many seller messages with this text PayPal lists. Message contents are never kept."""
    wanted = (text or "").strip()
    messages = raw.get("messages") if isinstance(raw, dict) else None
    sent = _parse_time(sent_at)
    listed, count = False, 0
    for item in messages if isinstance(messages, list) else []:
        if not isinstance(item, dict) or str(item.get("posted_by") or "").upper() != MESSAGE_FROM_SELLER:
            continue
        content = item.get("content")
        if not wanted or not isinstance(content, str) or content.strip() != wanted:
            continue
        count += 1
        posted = _parse_time(item.get("time_posted"))
        if posted is None or sent is None or posted >= sent - timedelta(seconds=CLOCK_TOLERANCE_SECONDS):
            listed = True
    return listed, count


def _stored_message_text(row: dict) -> str:
    try:
        body = json.loads(row.get("request_json") or "{}")
    except ValueError:
        return ""
    return body.get("message") if isinstance(body, dict) and isinstance(body.get("message"), str) else ""


def _evidence(row: dict, raw, final: dict | None, now: datetime) -> tuple[bool, dict | None]:
    """(confirmed, readback) for one stored row from one read of the dispute. An offer is confirmed when PayPal no longer
    lists the make-offer link; a message only when PayPal lists it. A failed read (final None) confirms nothing."""
    if final is None:
        return False, None
    readback = _readback(raw, final)
    readback["checked_at"] = now.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    if row["action"] == "offer":
        return not order.allowed_actions(final).offer, readback
    listed, count = message_evidence(raw, _stored_message_text(row), row.get("created_at"))
    readback["message_listed"], readback["message_count"] = listed, count
    return listed, readback


def _fee(client, norm: dict) -> tuple[int, str, bool]:
    """(fee cents, label, high_volume) by code: the account-cliff result when that feature is on, else the standard fee."""
    if config.feature_enabled("account_cliff"):
        from copilot.features.account_cliff import service as cliff   # lazy: same namespace, flag-guarded
        summary = cliff.compute(client)
        if summary.high_volume is True:
            return HIGH_VOLUME_FEE_CENTS.value, "high-volume fee (account cliff: at or above the threshold)", True
        if summary.high_volume is False:
            return STANDARD_FEE_CENTS.value, "standard fee (account cliff: below the threshold)", False
        return STANDARD_FEE_CENTS.value, "standard fee assumed (account cliff could not be read)", False
    return STANDARD_FEE_CENTS.value, "standard fee assumed (account cliff feature off)", False


def _context(client, norm: dict):
    recorder = _Recorder(client)
    ctx = fetch_context(recorder, norm)
    seller_txn = (norm.get("seller_transaction_ids") or [None])[0]
    return ctx, _eligibility(recorder.search_bodies, seller_txn)


def _economics(client, norm: dict, code: str | None) -> EconomicsLine | None:
    if not isinstance(norm.get("amount_cents"), int) or not norm.get("currency"):
        return None
    fee, label, high = _fee(client, norm)
    return economics_line(norm["amount_cents"], norm["currency"], fee, label, None, BUYER_CLOSES_FEE_KNOWN,
                          fee_applies_to_concede=norm.get("stage") != "INQUIRY", eligibility_code=code,
                          high_volume=high, fee_level_text=FEE_LEVEL_TEXT.get(label))


def dispute_economics(client, norm: dict, code: str | None) -> EconomicsLine | None:
    """Public dispute economics contract shared with the read-only triage view."""
    return _economics(client, norm, code)


def _any_pending(history: list) -> bool:
    return any(h["status"] == store.STATUS_PENDING for h in history)


def _shown_text(row: dict) -> str:
    """The text of a stored request for the history details: the message, or the offer amount and its note."""
    if row["action"] == "message":
        return _stored_message_text(row)
    try:
        body = json.loads(row.get("request_json") or "{}")
    except ValueError:
        return ""
    amount = body.get("offer_amount") if isinstance(body, dict) else None
    if not isinstance(amount, dict):
        return ""
    return f"{amount.get('value', '')} {amount.get('currency_code', '')}: {body.get('note', '')}".strip()


SENDING_STARTED = "sending_started"      # history only: a reservation without a stored row (the end of the send is unknown)


def history_rows(dispute_id: str) -> list:
    """The stored actions of a dispute, oldest first, plus one row for every reservation that has no stored row:
    a send that started and whose end the app never recorded (the process stopped, or an unexpected error). The trace
    survives; it counts as "Sending started - not confirmed yet" and still blocks a repeat."""
    rows = [{**r, "released": bool(store.readback_of(r).get("released")), "shown_text": _shown_text(r)}
            for r in store.list_actions(dispute_id)]
    # a released row no longer covers its key: a newer send of the same content that has a reservation but no stored row
    # (the end of the send was not recorded) must still show as "sending started"
    known = {(r["action"], r["request_sha256"]) for r in rows if r["status"] in store.SENT_STATUSES and not r["released"]}
    extra = []
    for res in write_guard.list_reservations(dispute_id):
        if res["state"] in write_guard.BLOCKING and (res["action"], res["request_sha256"]) not in known                 and res["action"] in store.ACTIONS:
            extra.append({"id": None, "dispute_id": dispute_id, "action": res["action"],
                          "created_at": datetime.fromtimestamp(res["created_at"]).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                          "request_json": "", "request_sha256": res["request_sha256"], "approval_sha256": res["request_sha256"],
                          "readback_json": "null", "settled": 0, "status": SENDING_STARTED, "limitation_text": None,
                          "released": False, "shown_text": ""})
    return sorted(rows + extra, key=lambda r: (r["created_at"], r["id"] or 0))


def page_state(client, dispute_id: str) -> PageState:
    _raw, norm, limits = _read(client, dispute_id)
    history = history_rows(dispute_id)
    if norm is None:
        return PageState(None, order.Actions(False, False, False), "", False, None, history, limits,
                         _any_pending(history))
    ctx, code = _context(client, norm)
    buyer_closed = buyer_closed_raw(_raw)
    offer = _raw.get("offer") if isinstance(_raw, dict) else None
    requested = offer.get("buyer_requested_amount") if isinstance(offer, dict) else None
    return PageState(norm, order.allowed_actions(norm), order.next_step_text(norm, history), order.is_settling(norm),
                     _economics(client, norm, code), history, tuple(limits) + tuple(ctx.limitations),
                     _any_pending(history), _template_text(norm, ctx), buyer_closed,
                     requested if isinstance(requested, dict) else None)


def _not_allowed(action: str, norm: dict | None, limits: tuple, text: str = "") -> Preview:
    return Preview(action, None, None, False, text or (order.next_step_text(norm) if norm else "PayPal could not be read"),
                   None, order.next_step_text(norm) if norm else "", limits)


def prepare_offer(client, dispute_id: str, cents: int, note: str) -> Preview:
    _raw, norm, limits = _read(client, dispute_id)
    if norm is None:
        return _not_allowed("offer", None, limits)
    ctx, code = _context(client, norm)
    economics = _economics(client, norm, code)
    if not order.allowed_actions(norm).offer:
        return Preview("offer", None, None, False, order.next_step_text(norm), economics, order.next_step_text(norm),
                       tuple(limits))
    try:
        body = act.offer_body(cents, norm.get("currency") or "", note, max_cents=norm.get("amount_cents"))
    except ValueError as exc:
        return Preview("offer", None, None, False, str(exc), economics, order.next_step_text(norm), tuple(limits))
    return Preview("offer", body, act.request_sha256(body), True, "", economics, order.next_step_text(norm),
                   tuple(limits) + tuple(ctx.limitations), "seller")


LINK_MARKERS = ("http", "www.", "@")


def link_marker(text) -> str | None:
    """The first link or email marker in the text (LINK_MARKERS, any case, by position), or None."""
    lowered = text.lower() if isinstance(text, str) else ""
    found = [(lowered.index(marker), marker) for marker in LINK_MARKERS if marker in lowered]
    return min(found)[1] if found else None


def message_text_problem(text) -> str | None:
    """The one rule set for the text of a seller message, used at preview time (prepare_message) AND at write time
    (_body_problem), so the two cannot drift: a plain-language problem, or None when the text may be sent. Refused: a non-string,
    any link or email marker (LINK_MARKERS, any case; the answer quotes the marker it found), and whatever act._clean_text
    refuses (empty, or over MAX_TEXT_LEN)."""
    if not isinstance(text, str):
        return "The message request is not in the expected shape."
    marker = link_marker(text)
    if marker:
        return (f"Your message contains “{marker}”. Messages sent through Proofie cannot include links or email "
                f"addresses. Remove “{marker}” and review again.")
    if not text.strip():
        return "Enter the message you want to send to the buyer"
    if len(text.strip()) > act.MAX_TEXT_LEN:
        return f"Message must be {act.MAX_TEXT_LEN} characters or fewer"
    try:
        act._clean_text(text, "message")
    except ValueError as exc:
        return str(exc)
    return None


def _latest_valid_draft(dispute_id: str) -> dict | None:
    """The latest draft the validator accepted for this dispute, read only. Looked up only when the draft
    feature is switched on, so the draft table is never touched otherwise."""
    if not config.feature_enabled("draft"):
        return None
    from copilot.features.draft import store as draft_store      # lazy: flag-guarded, read only
    draft = draft_store.latest_draft(dispute_id)
    return draft if draft and draft.get("validation_ok") else None


def message_origin(dispute_id: str, text: str, template_text: str) -> str:
    """Where this exact text came from, decided by the server and never by the browser: the latest valid AI draft
    unchanged, the fixed template unchanged, a valid AI draft exists and the text differs, or written by the seller."""
    chosen = (text or "").strip()
    draft = _latest_valid_draft(dispute_id)
    if draft and (draft.get("en") or "").strip() == chosen:
        return "ai_unedited"
    if chosen and chosen == (template_text or "").strip():
        return "template"
    return "ai_edited" if draft else "seller"


def _template_text(norm: dict, ctx) -> str:
    """The fixed message template from PayPal's own fields (the same text prepare_message sends when the seller adds none)."""
    fields = {"payment_date": ctx.get("payment_date"), "total_amount_cents": ctx.get("total_amount_cents"),
              "total_currency": ctx.get("total_currency"), "shipping_address": ctx.get("shipping_address")}
    tracking = {"tracking_number": ctx.get("tracking_number"), "carrier": ctx.get("carrier"),
                "tracking_status": ctx.get("tracking_status")}
    return act.message_text(norm, fields, tracking if tracking["tracking_number"] else None)


def prepare_message(client, dispute_id: str, text: str | None = None) -> Preview:
    """The message preview. `text` is the seller's reviewed text (e.g. from the draft feature): cleaned and bounded by
    act._clean_text, refused when it holds a link or an email, and used instead of the template; empty or None keeps the
    template. The body stays {"message": text}, so the hash, the one POST and the GET after it are unchanged."""
    _raw, norm, limits = _read(client, dispute_id)
    if norm is None:
        return _not_allowed("message", None, limits)
    ctx, code = _context(client, norm)
    economics = _economics(client, norm, code)
    if not order.allowed_actions(norm).message:
        return Preview("message", None, None, False, order.next_step_text(norm), economics,
                       order.next_step_text(norm), tuple(limits))
    chosen = _template_text(norm, ctx)
    if text is not None and text.strip():
        problem = message_text_problem(text)
        if problem:
            return Preview("message", None, None, False, problem, economics, order.next_step_text(norm), tuple(limits))
        chosen = act._clean_text(text, "message")
    body = act.message_body(chosen)
    template = _template_text(norm, ctx)
    return Preview("message", body, act.request_sha256(body), True, "", economics, order.next_step_text(norm),
                   tuple(limits) + tuple(ctx.limitations), message_origin(dispute_id, chosen, template), template)


def _link_path(norm: dict, dispute_id: str, rel: str, suffix: str) -> tuple[str | None, str]:
    """PayPal's own link for the write (from a fresh GET), checked against the sandbox host/path/method allowlist."""
    links = norm.get("action_links") or {}
    link = links.get(rel) or links.get(rel.replace("-", "_"))
    if not link or not link.get("href"):
        return None, order.next_step_text(norm)
    parsed = urlparse(link["href"])
    bad = (parsed.scheme != "https" or parsed.netloc != ALLOWED_HOST
           or parsed.path != f"/v1/customer/disputes/{dispute_id}{suffix}" or (link.get("method") or "").upper() != "POST")
    if bad:
        return None, "PayPal gave an unexpected link for this action; nothing was sent."
    return parsed.path, ""


def _body_problem(action: str, body: dict, norm: dict) -> str | None:
    if not isinstance(body, dict):
        return "The request is not in the expected shape."
    if action == "message":
        if set(body) != {"message"}:
            return "The message request is not in the expected shape."
        return message_text_problem(body.get("message"))
    amount = body.get("offer_amount")
    if set(body) != {"offer_type", "offer_amount", "note"} or body.get("offer_type") != act.OFFER_TYPE \
            or not isinstance(amount, dict) or not isinstance(body.get("note"), str) or not body["note"].strip():
        return "The offer request is not in the expected shape."
    try:
        cents, currency = money_to_minor(amount)
    except ValueError:
        return "The offer amount is not valid."
    if cents is None or cents <= 0 or cents > (norm.get("amount_cents") or 0) or currency != norm.get("currency"):
        return "The offer amount must be positive, in the dispute's currency and not above the disputed amount."
    return None


def _definite_refusal(status_code) -> bool:
    """A PayPal answer that says nothing was done: a 4xx other than 429. 5xx, 429 and no answer leave it unknown."""
    return isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429


ALREADY_LISTED_TEXT = "PayPal already lists a message from the seller with this exact text; nothing was sent."


def already_sent_text(action: str) -> str:
    return f"This exact {'offer' if action == 'offer' else 'message'} was already sent; nothing was sent again."


def _already_sent(dispute_id: str, action: str, sha: str, reserved_state: str | None = None) -> Outcome | None:
    """The same approved request is sent once. A stored row whose POST may have reached PayPal
    (pending, confirmed, unconfirmed: also rows saved before this guard existed) or a live/unknown reservation of the
    same key blocks a repeat; a definite refusal (refused_422, failed with a PayPal status) does not. No PayPal call."""
    row = store.find_sent(dispute_id, action, sha)
    if row is None and not (reserved_state or write_guard.is_blocking(write_guard.lookup(dispute_id, action, sha))):
        return None
    return Outcome("already_sent", None, None, None, (), sha, already_sent_text(action), existing=row)


def register(client, dispute_id: str, action: str, body: dict, approval_sha256: str, origin: str | None = None) -> Outcome:
    """Validates, POSTs once and returns at once: after a 2xx the row is saved as sent_pending_settle and the page
    settles it one GET per refresh (settle_pending). Nothing here waits. The same approved request (dispute, action,
    sha256 of the body) is POSTed at most once: it is reserved atomically in SQLite before the POST. `origin` is the
    closed code of where the text came from (computed by the server at preview); it is kept beside the row."""
    if action not in WRITE:
        raise ValueError(f"unknown action {action!r}")
    op, rel, suffix = WRITE[action]
    sha = act.request_sha256(body) if isinstance(body, dict) else ""
    if not sha or approval_sha256 != sha:
        return Outcome("rejected_hash", None, None, None, (), sha, "The approval does not match this request; nothing was sent.")
    repeat = _already_sent(dispute_id, action, sha)
    if repeat is not None:
        return repeat                                   # no PayPal call at all, not even a GET
    _raw, norm, limits = _read(client, dispute_id)
    if norm is None:
        return Outcome("read_failed", None, None, None, tuple(limits), sha, "PayPal could not be read; nothing was sent.")
    path, reason = _link_path(norm, dispute_id, rel, suffix)
    if path is None:
        return Outcome("rejected_link", None, None, None, (), sha, reason, settling=order.is_settling(norm))
    problem = _body_problem(action, body, norm)
    if problem:
        return Outcome("rejected_body", None, None, None, (), sha, problem)
    if action == "message" and message_evidence(_raw, body["message"])[0]:
        # PayPal's fresh read already lists a seller message with this exact text: nothing is reserved and nothing is POSTed.
        return Outcome("already_listed", None, None, None, (), sha, ALREADY_LISTED_TEXT)

    claim = write_guard.reserve(dispute_id, action, sha)       # BEGIN IMMEDIATE: two concurrent submits cannot both pass
    if not claim.reserved:
        return _already_sent(dispute_id, action, sha, claim.state)
    final = write_guard.UNKNOWN                                  # until we know better, the outcome is unknown
    stored_origin = {"origin": origin} if origin in ORIGIN_LABELS else None
    try:
        request_id = str(uuid.uuid5(_REQUEST_NS, f"{dispute_id}:{sha}"))
        try:
            client.call("POST", path, op, json_body=body, request_id=request_id,
                        log_extra=write_audit(sha, approval_sha256))
        except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
            status_code = getattr(exc, "status", None)
            if isinstance(exc, PayPalError) and status_code == 422:
                text, status = REFUSED_422_TEXT, "refused_422"
            else:
                text = failure_message(exc, f"send the {action}")
                if text is None:
                    raise
                # a transport failure, a 5xx or a 429 leaves the outcome unknown (PayPal may have processed it, the
                # same rule as the evidence send); a 4xx refusal is a plain failure
                status = "failed" if _definite_refusal(status_code) else "sent_unconfirmed"
            final = write_guard.REFUSED if _definite_refusal(status_code) else write_guard.UNKNOWN
            saved = store.save_action(dispute_id, action, body, sha, approval_sha256, stored_origin, False, status, text)
            return Outcome(status, status_code if isinstance(status_code, int) else None, None, None,
                           (Limitation(SRC_DISPUTE, text, paypal_words.technical_of(exc)),), sha, text, row_id=saved["id"],
                           technical=paypal_words.technical_of(exc))
        final = write_guard.SENT
        saved = store.save_action(dispute_id, action, body, sha, approval_sha256, stored_origin, False, store.STATUS_PENDING, None)
        return Outcome(store.STATUS_PENDING, None, None, None, (), sha, PENDING_TEXT, row_id=saved["id"])
    finally:
        write_guard.finish(dispute_id, action, sha, final)


def _age_seconds(created_at: str, now: datetime) -> int | None:
    try:
        return int((now - datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S%z")).total_seconds())
    except (ValueError, TypeError):
        return None


def _row_epoch(created_at):
    try:
        return datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (ValueError, TypeError):
        return None


def settle_pending(client, dispute_id: str, now: datetime | None = None) -> int:
    """One GET per sent_pending_settle row (never a loop). Settled: one more read-back, then sent_confirmed (an offer
    needs its link gone; a message needs PayPal to LIST it) or sent_unconfirmed, settled 1. Still settling: the row
    stays pending until it is older than SETTLE_WINDOW_SECONDS, then sent_unconfirmed with the Limitation text. A
    failed read leaves it pending (or times it out the same way). Returns the number of rows updated."""
    now = now or datetime.now(timezone.utc)
    updated = 0
    for row in [r for r in store.list_actions(dispute_id) if r["status"] == store.STATUS_PENDING]:
        audit = readback_audit(row["request_sha256"], row["approval_sha256"])
        polled = wait_until_settled(client, dispute_id, polls=1, log_extra=audit)
        if polled.settled:
            raw, final, read_limits = _read(client, dispute_id, READBACK_OP, log_extra=audit)
            confirmed, readback = _evidence(row, raw, final, now)
            text = " ".join(lim.message for lim in read_limits) or None
            store.update_action_status(row["id"], "sent_confirmed" if confirmed else "sent_unconfirmed", readback, True, text)
            updated += 1
            continue
        age = _age_seconds(row["created_at"], now)
        if age is not None and age >= SETTLE_WINDOW_SECONDS:
            store.update_action_status(row["id"], "sent_unconfirmed", None, False, SETTLE_TIMEOUT_TEXT)
            updated += 1
        else:                                  # still settling: keep only the time PayPal was last read ("Last checked ...")
            store.update_action_status(row["id"], store.STATUS_PENDING,
                                       {"checked_at": now.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")}, False, None)
    return updated


def check_rows(client, dispute_id: str, row_id: int | None = None, now: datetime | None = None) -> CheckResult:
    """The read-only check behind "Check PayPal again". One GET of the dispute per stored row that is still
    pending or unconfirmed (or only `row_id`), and updates only from what PayPal's response shows: a message is
    confirmed only when PayPal lists it, an offer only when PayPal no longer shows the make-offer link. A row stays as
    it was while PayPal is still updating the dispute or when the read fails. The check time is stored in the read-back.
    It never sends and never retries a send."""
    now = now or datetime.now(timezone.utc)
    rows = [r for r in store.list_actions(dispute_id)
            if r["status"] in (store.STATUS_PENDING, "sent_unconfirmed")
            and (r["id"] == row_id if row_id is not None else not store.readback_of(r).get("released"))]
    checked = confirmed = absent = unchanged = offered = 0
    absent_ids, released_ids, error = [], [], ""
    for row in rows:
        audit = readback_audit(row["request_sha256"], row["approval_sha256"])
        raw, norm, limits = _read(client, dispute_id, READBACK_OP, log_extra=audit)
        checked += 1
        if norm is None:
            error = " ".join(lim.message for lim in limits)
            unchanged += 1
            continue
        if order.is_settling(norm):
            unchanged += 1
            age = _age_seconds(row["created_at"], now)
            if row["status"] == store.STATUS_PENDING and age is not None and age >= SETTLE_WINDOW_SECONDS:
                store.update_action_status(row["id"], "sent_unconfirmed", None, False, SETTLE_TIMEOUT_TEXT)
            continue
        ok, readback = _evidence(row, raw, norm, now)
        if ok:
            store.update_action_status(row["id"], "sent_confirmed", readback, True, None)
            confirmed += 1
            continue
        if row["action"] == "message":
            # PayPal answered, is not updating the dispute and does not list the message. That is only a positive absence
            # when (a) the read succeeded, (b) the reservation of the send is at least RELEASE_MIN_AGE_SECONDS old and
            # (c) the read lists the dispute's messages normally (a "messages" list is present). Then, and only then, the
            # key is released so the seller may prepare it again (fresh check page, fresh approval). Otherwise the row
            # stays blocked. The age is the RESERVATION's (the POST), never the checked row's: a newer send of the same
            # content is never released by a check of an older row, and a row that was already released is not released again.
            previous = store.readback_of(row)
            normal_read = isinstance(raw.get("messages"), list) if isinstance(raw, dict) else False
            readback["message_absent"] = True
            absent += 1
            absent_ids.append(row["id"])
            if previous.get("released"):          # released earlier: keep its mark and times, only note this check
                store.update_action_status(row["id"], "sent_unconfirmed", {**previous, "checked_at": readback["checked_at"]},
                                           True, row.get("limitation_text"))
                continue
            reservation = write_guard.lookup(dispute_id, row["action"], row["request_sha256"])
            row_epoch = _row_epoch(row["created_at"])
            posted_at = reservation["created_at"] if reservation else row_epoch
            age = (now.timestamp() - posted_at) if posted_at is not None else None
            newer = bool(reservation and row_epoch is not None
                         and reservation["created_at"] > row_epoch + RESERVATION_SLACK_SECONDS)
            in_flight = bool(reservation and reservation["state"] == write_guard.RESERVED)
            released = False
            if age is not None and age >= RELEASE_MIN_AGE_SECONDS and normal_read and not newer and not in_flight:
                changed = write_guard.release_after_absence(
                    dispute_id, row["action"], row["request_sha256"],
                    created_at_most=None if row_epoch is None else row_epoch + RESERVATION_SLACK_SECONDS)
                released = changed or not write_guard.is_blocking(write_guard.lookup(dispute_id, row["action"], row["request_sha256"]))
            if released:
                readback["released"] = True
                readback["minutes_after_send"] = int(age // 60)
                store.update_action_status(row["id"], "sent_unconfirmed", readback, True, row.get("limitation_text"))
                # every check page of this content prepared before the release is spent: a repeat needs a new one
                store.supersede_checks_for_request(dispute_id, row["action"], row["request_sha256"], time.time())
                released_ids.append(row["id"])
            else:
                store.update_action_status(row["id"], "sent_unconfirmed", readback, True, row.get("limitation_text"))
        else:
            store.update_action_status(row["id"], "sent_unconfirmed", readback, True, row.get("limitation_text"))
            unchanged += 1
            offered += 1                          # PayPal answered normally and still offers the make-offer option
    return CheckResult(checked, confirmed, absent, unchanged, error, tuple(absent_ids), tuple(released_ids), offered)
