"""The page's data and the model step. Reads PayPal (GET only), never writes to it. The draft is text under review: it is never a
state and never a fact; the fact sheet (by code) and the fixed template stand behind it.

draft_view builds the fact sheet and the template from the same sources the resolution feature's message path uses. make_draft runs the model on the
sheet only, validates the answer by code and stores it (refused drafts too, with their problems; the template stands).

A failed PayPal read is its own channel (CHANNEL_UNKNOWN): the page then says it could not read the dispute and shows
no PayPal state, and make_draft never calls the model on an empty fact sheet. A stored draft is only offered when it is current: the
stored fact sheet equals today's sheet and the text still passes the validator against it (state "valid"); otherwise it is "stale"
(names what changed) or, when the checks refused it, "rejected" (one sentence per kind, recomputed from the row's own sheet)."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from copilot.core import paypal_words
from copilot.core.context import SRC_DISPUTE, Limitation, fetch_context
from copilot.core.disputes import normalize
from copilot.features.draft import check, model, store
from copilot.features.draft.facts import FactSheet, fact_sheet
from copilot.features.resolution import actions as act
from copilot.features.resolution import order

READ_OP = "draft_read"
CHANNEL_PAYPAL, CHANNEL_COPY, CHANNEL_UNKNOWN = "paypal_message", "copy_only", "unknown"
SENTENCE_COPY = "PayPal offers no send-message option for this dispute. You can copy this text and send it to the buyer yourself; PayPal's record will not show it."
SENTENCE_PAYPAL = "PayPal accepts a message on this dispute. Nothing is sent from this page."
STATE_NONE, STATE_VALID, STATE_STALE, STATE_REJECTED = "none", "valid", "stale", "rejected"
ERROR_KINDS = {model.UNAVAILABLE: "unavailable", model.MALFORMED: "unreadable"}

# Display words of the fact-sheet fields that can make a stored draft stale (the sheet's own labelled() names stay for the prompt).
FIELD_WORDS = (("order_date", "order date"), ("amount_text", "order amount"), ("currency", "order amount"),
               ("shipping_address_line", "shipping address"), ("tracking_number", "tracking number"), ("carrier", "carrier"),
               ("tracking_status", "last tracking status"), ("delivery_estimate", "delivery estimate"),
               ("dispute_id", "dispute"), ("dispute_stage", "dispute stage"))


@dataclass(frozen=True)
class DraftView:
    sheet: FactSheet
    template_text: str
    draft: dict | None
    channel: str
    sentence: str
    limitations: tuple = ()
    error: str | None = None
    error_kind: str | None = None          # "unavailable" | "unreadable" when the model step failed
    offer_possible: bool = False           # PayPal still lists make-offer for this dispute
    state: str = STATE_NONE                # none | valid | stale | rejected (see the module docstring)
    stale_labels: tuple = ()               # what changed since a stale draft was written
    issues: tuple | None = None            # rejected: check.Issue values recomputed at render; None = fail closed


def _failure_text(exc: Exception) -> str | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return "PayPal could not return the dispute right now. Try again in a few minutes."
    if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return "PayPal did not answer in time when reading the dispute."
    return None


def _read(client, dispute_id: str):
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", READ_OP)
    except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
        text = _failure_text(exc)
        if text is None:
            raise
        return None, (Limitation(SRC_DISPUTE, text, paypal_words.technical_of(exc)),)
    return normalize(raw, datetime.now(timezone.utc)), ()


def _facts(client, dispute_id: str):
    """(norm, sheet, template text, limitations): the same fields prepare_message in the resolution feature uses."""
    norm, limits = _read(client, dispute_id)
    if norm is None:
        return None, fact_sheet(None, {}, None), "", tuple(limits)
    ctx = fetch_context(client, norm)
    fields = {"payment_date": ctx.get("payment_date"), "total_amount_cents": ctx.get("total_amount_cents"),
              "total_currency": ctx.get("total_currency"), "shipping_address": ctx.get("shipping_address")}
    tracking = {"tracking_number": ctx.get("tracking_number"), "carrier": ctx.get("carrier"),
                "tracking_status": ctx.get("tracking_status")}
    tracking = tracking if tracking["tracking_number"] else None
    return norm, fact_sheet(norm, fields, tracking), act.message_text(norm, fields, tracking), \
        tuple(limits) + tuple(ctx.limitations)


def sheet_json(sheet: FactSheet) -> str:
    """The exact serialisation store.save_draft keeps: two sheets are the same sheet when these strings are equal (a loaded list never
    equals the dataclass's tuple, so the comparison is on the text)."""
    return json.dumps(dataclasses.asdict(sheet), sort_keys=True, ensure_ascii=False)


def _sheet_from_row(draft: dict) -> FactSheet:
    values = {k: (tuple(v) if isinstance(v, list) else v) for k, v in draft["sheet"].items()}
    return FactSheet(**values)


def _changed_labels(draft: dict, sheet: FactSheet) -> tuple:
    now = dataclasses.asdict(sheet)
    words = []
    for field, word in FIELD_WORDS:
        if draft["sheet"].get(field) != now.get(field) and word not in words:
            words.append(word)
    return tuple(words)


def assess(draft: dict | None, sheet: FactSheet):
    """-> (state, stale_labels, issues) for the latest stored draft against today's sheet. Pure; never calls the model."""
    if draft is None:
        return STATE_NONE, (), None
    if not draft["validation_ok"]:
        try:
            own = _sheet_from_row(draft)
            verdict = check.validate_draft(own, draft["en"], draft["es"])
            consistent = (not verdict.ok) and verdict.problems == tuple(draft["problems"])
            return STATE_REJECTED, (), tuple(check.issues(own, draft["en"], draft["es"])) if consistent else None
        except (TypeError, KeyError, ValueError):
            return STATE_REJECTED, (), None            # fail closed: no per-kind sentences
    if draft["sheet_json"] != sheet_json(sheet) or not check.validate_draft(sheet, draft["en"], draft["es"]).ok:
        return STATE_STALE, _changed_labels(draft, sheet), None
    return STATE_VALID, (), None


def _view(norm, sheet, template, draft, limits, error=None, error_kind=None) -> DraftView:
    if norm is None:
        return DraftView(sheet, template, None, CHANNEL_UNKNOWN, "", tuple(limits), error, error_kind)
    actions = order.allowed_actions(norm)
    channel = CHANNEL_PAYPAL if actions.message else CHANNEL_COPY
    state, stale_labels, issues = assess(draft, sheet)
    return DraftView(sheet, template, draft, channel, SENTENCE_PAYPAL if channel == CHANNEL_PAYPAL else SENTENCE_COPY,
                     tuple(limits), error, error_kind, bool(actions.offer), state, stale_labels, issues)


def draft_view(client, db_path, dispute_id: str) -> DraftView:
    norm, sheet, template, limits = _facts(client, dispute_id)
    return _view(norm, sheet, template, store.latest_draft(dispute_id), limits)


def make_draft(client, db_path, dispute_id: str, call=None) -> DraftView:
    """Fact sheet -> model -> validation -> stored. An unavailable or unreadable model stores nothing and sets `error` (the
    template stands); a draft the validator refuses is stored with its problems. When PayPal cannot be read the sheet is empty and
    the model is not called at all."""
    norm, sheet, template, limits = _facts(client, dispute_id)
    if norm is None:
        return _view(norm, sheet, template, None, limits)
    result = model.call_draft(sheet, call=call)
    if result.error:
        return _view(norm, sheet, template, store.latest_draft(dispute_id), limits, result.error, ERROR_KINDS.get(result.error))
    validation = check.validate_draft(sheet, result.en, result.es)
    seller_txn = (norm or {}).get("seller_transaction_ids") or [None]
    store.save_draft(dispute_id, seller_txn[0], sheet, result.en, result.es, result.model, result.seed, result.raw_sha256,
                     validation.ok, validation.problems, "model")
    return _view(norm, sheet, template, store.latest_draft(dispute_id), limits)


# ---- the rejected-draft sentences: one per kind, values and languages merged ---------------------------------------
_PLAIN = {
    "tracking_missing": "It leaves out the tracking number from the facts.",
    "echo": "It copies words from the buyer's address or another fact. Proofie does not reuse those words.",
    "refund": "It mentions a refund. Refunds are your decision on the resolution page.",
    "link": "It has a link or an email address.",
    "promise": "It makes a promise (guarantee). Proofie does not allow promises.",
    "spelled_date": "It writes a date in words. Proofie accepts only the date as shown in the facts.",
    "too_long": f"It is longer than {check.MAX_LEN} characters.",
    "delivery": "It says delivered or signed for. PayPal's tracking does not show a delivery, so Proofie did not use it.",
}
_VALUED = {
    "number": ("It has numbers that are not in the facts: {}.", "It has numbers that are not in the facts: {}."),
    "date": ("It has a date that is not in the facts: {}.", "It has dates that are not in the facts: {}."),
    "tracking_code": ("It has a tracking-like code that is not in the facts: {}.",
                      "It has tracking-like codes that are not in the facts: {}."),
}
BOTH_LANGUAGES = " (English and Spanish text)"


def _with_display_date(value: str) -> str:
    """the literal date the model wrote stays (it is the evidence); the seller's display form follows it in
    brackets, e.g. '2026-10-02 (2 Oct 2026)'. A value the display format cannot read is shown as written."""
    from copilot.web import timefmt
    shown = timefmt.display(value)
    return value if shown in ("", "Not available") else f"{value} ({shown})"


def rejected_sentences(issues) -> list:
    """Seller sentences for check.Issue values, in the order the kinds first appear; a kind found in both texts gets the suffix."""
    order_of_kinds, grouped = [], {}
    for issue in issues:
        key = (issue.kind, issue.language) if issue.kind in ("language", "empty") else (issue.kind, None)
        if key not in grouped:
            grouped[key] = {"languages": [], "values": []}
            order_of_kinds.append(key)
        entry = grouped[key]
        if issue.language not in entry["languages"]:
            entry["languages"].append(issue.language)
        if issue.value and issue.value not in entry["values"]:
            entry["values"].append(issue.value)
    sentences = []
    for key in order_of_kinds:
        kind, language = key
        entry = grouped[key]
        if kind == "language":
            text = f"The {language} text does not read as {language}."
        elif kind == "empty":
            text = f"The {language} text is empty."
        elif kind in _VALUED:
            single, plural = _VALUED[kind]
            values = [_with_display_date(v) for v in entry["values"]] if kind == "date" else entry["values"]
            text = (single if len(values) == 1 else plural).format(", ".join(values))
        else:
            text = _PLAIN[kind]
        if len(entry["languages"]) == 2:
            text += BOTH_LANGUAGES
        sentences.append(text)
    return sentences
