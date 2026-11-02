"""PayPal's own timeline: the dated items PayPal's dispute GET gives, in order, in PayPal's words. Pure; no model; no clock.

PayPal's dispute record has NO log of status changes (pinned schema copilot/features/conformance/fixtures/disputes.json), so
this never says when a status changed and never infers it. It lists what carries a time of its own: the opening, evidence
(requested or submitted), messages, offers, adjudications, supporting information, money movements, the contact times and
PayPal's last update. Each event keeps PayPal's own time text; the page shows it in Mexico City time through
copilot.web.timefmt. Ordering uses the instant, never the text; a time with no usable instant is listed last and reads
"Not available" (timefmt.display). An item with no time at all is not a dated item and is left out.

What never reaches an event: a message body, a note, a link, and any value PayPal sent that the closed tables below do not
know (it reads as a generic sentence, never echoed). Document names are shown trimmed, at most five, a link as "a file".
Money is read with integer cents (step3_dispute.money_to_minor) and written back by integer arithmetic.

The seam for later sources (W2-2, webhook events the app has verified): `build(raw, read_at, verified=[...])` merges events
made by `verified_event(at, text, detail)`; they are marked origin "verified_event" and kind "verified" so the page can say
where each line comes from. Nothing here verifies anything: the caller hands over only events it has verified itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from copilot.core.disputes import EVIDENCE_TYPE_WORDS, evidence_type_words, humanize_status
from copilot.web import timefmt
from step3_dispute import minor_to_str, money_to_minor  # noqa: E402  (pilot/ is on sys.path via copilot.core.disputes)

ORIGIN_PAYPAL = "paypal_dispute"
ORIGIN_VERIFIED = "verified_event"
MAX_FILE_NAMES = 5
MAX_NAME_LEN = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_FAR_PAST = datetime(1, 1, 1, tzinfo=timezone.utc)

OPENED_TEXT = "Dispute opened"
UPDATED_TEXT = "PayPal last updated the dispute"
EVIDENCE_TEXT = {"SUBMITTED_BY_BUYER": "Buyer submitted evidence", "SUBMITTED_BY_SELLER": "Seller submitted evidence",
                 "REQUESTED_FROM_SELLER": "PayPal asked the seller for evidence",
                 "REQUESTED_FROM_BUYER": "PayPal asked the buyer for evidence"}
EVIDENCE_OTHER = "PayPal listed evidence"
MESSAGE_TEXT = {"BUYER": "The buyer posted a message", "SELLER": "The seller posted a message"}
MESSAGE_OTHER = "A message was posted"
SUPPORTING_TEXT = {"SUBMITTED_BY_BUYER": "The buyer provided supporting information",
                   "SUBMITTED_BY_SELLER": "The seller provided supporting information"}
SUPPORTING_OTHER = "Supporting information was provided"
OFFER_ACTOR = {"BUYER": "The buyer", "SELLER": "The seller"}
OFFER_ACTOR_OTHER = "A party"
OFFER_EVENT = {"PROPOSED": "made an offer", "ACCEPTED": "accepted the offer", "DENIED": "declined the offer"}
OFFER_EVENT_OTHER = "recorded an offer event"
ADJUDICATION_TEXT = {"DENY_BUYER": "PayPal denied the buyer's claim",
                     "PAYOUT_TO_BUYER": "PayPal decided a payout to the buyer",
                     "PAYOUT_TO_SELLER": "PayPal decided a payout to the seller",
                     "RECOVER_FROM_SELLER": "PayPal decided to recover the funds from the seller"}
ADJUDICATION_OTHER = "PayPal recorded an adjudication"
MONEY_KIND = {"DEBIT": "A debit", "CREDIT": "A credit"}
MONEY_KIND_OTHER = "A money movement"
MONEY_PARTY = {"SELLER": "the seller", "BUYER": "the buyer", "PAYMENT_PROCESSOR": "PayPal"}
MONEY_REASON = {"DISPUTE_SETTLEMENT_FEE": "dispute settlement fee", "DISPUTE_SETTLEMENT": "dispute settlement",
                "DISPUTE_FEE": "dispute fee", "CHARGEBACK_FEE": "chargeback fee"}
BUYER_CONTACT_TEXT = "Buyer contact time recorded by PayPal"
SELLER_CONTACT_TEXT = "Seller contact time recorded by PayPal"


@dataclass(frozen=True)
class Event:
    at: str                              # PayPal's own time text, as given; shown through timefmt, never reformatted here
    kind: str                            # opened | evidence | message | offer | adjudication | supporting_info | money | contact | updated | verified
    text: str
    detail: tuple = ()                   # extra lines, each already safe to show
    instant: datetime | None = None      # aware UTC; None when `at` has no usable instant
    after_read: bool = False             # dated later than the read of PayPal's record
    origin: str = ORIGIN_PAYPAL


def _clean(value, limit: int = MAX_NAME_LEN) -> str:
    """One line, no control characters, at most `limit` characters ('…' marks a cut). '' for a non-string."""
    if not isinstance(value, str):
        return ""
    text = " ".join(_CONTROL.sub(" ", value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _dicts(value) -> list:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _make(at, kind: str, text: str, detail=(), origin: str = ORIGIN_PAYPAL):
    """None when `at` is not a text with something in it: no time at all means not a dated item."""
    if not isinstance(at, str) or not at.strip():
        return None
    return Event(at=at, kind=kind, text=text, detail=tuple(line for line in detail if line),
                 instant=timefmt.instant(at), origin=origin)


def _files(documents) -> tuple:
    """('Files: a.pdf, b.pdf',) by file name only: a link reads 'a file', at most five names, then a count."""
    names = []
    for document in _dicts(documents):
        name = document.get("name")
        if not isinstance(name, str):
            continue
        collapsed = " ".join(_CONTROL.sub(" ", name).split())
        if not collapsed:
            continue
        lowered = collapsed.lower()
        names.append("a file" if "://" in lowered or lowered.startswith("www.") else _clean(collapsed))
    if not names:
        return ()
    more = len(names) - MAX_FILE_NAMES
    return ("Files: " + ", ".join(names[:MAX_FILE_NAMES]) + (f" and {more} more" if more > 0 else ""),)


def _money(money) -> str | None:
    """{'currency_code': 'USD', 'value': '10.00'} -> '10.00 USD'; None unless PayPal's text reads exactly."""
    if not isinstance(money, dict) or not isinstance(money.get("value"), str):
        return None
    currency = money.get("currency_code")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        return None
    try:
        cents, code = money_to_minor(money)
    except (ValueError, TypeError, ArithmeticError):
        return None
    return f"{minor_to_str(cents, code)} {code}" if cents is not None else None


def _table(table: dict, key, other: str) -> str:
    return table.get(key, other) if isinstance(key, str) else other


def _evidence(raw: dict) -> list:
    out = []
    for item in _dicts(raw.get("evidences")):
        text = _table(EVIDENCE_TEXT, item.get("source"), EVIDENCE_OTHER)
        kind = item.get("evidence_type")
        if isinstance(kind, str) and kind in EVIDENCE_TYPE_WORDS:
            text = f"{text}: {evidence_type_words(kind)}"
        info = item.get("evidence_info")
        trackers = len(_dicts(info.get("tracking_info"))) if isinstance(info, dict) else 0
        detail = _files(item.get("documents")) + (
            (f"Tracking information for {trackers} shipment{'' if trackers == 1 else 's'}",) if trackers else ())
        out.append(_make(item.get("date"), "evidence", text, detail))
    return out


def _messages(raw: dict) -> list:
    return [_make(item.get("time_posted"), "message", _table(MESSAGE_TEXT, item.get("posted_by"), MESSAGE_OTHER),
                  _files(item.get("documents"))) for item in _dicts(raw.get("messages"))]


def _offers(raw: dict) -> list:
    offer = raw.get("offer")
    out = []
    for item in _dicts(offer.get("history")) if isinstance(offer, dict) else []:
        text = (f"{_table(OFFER_ACTOR, item.get('actor'), OFFER_ACTOR_OTHER)} "
                f"{_table(OFFER_EVENT, item.get('event_type'), OFFER_EVENT_OTHER)}")
        amount = _money(item.get("offer_amount"))
        out.append(_make(item.get("offer_time"), "offer", text, (f"Amount: {amount}",) if amount else ()))
    return out


def _adjudications(raw: dict) -> list:
    return [_make(item.get("adjudication_time"), "adjudication",
                  _table(ADJUDICATION_TEXT, item.get("type"), ADJUDICATION_OTHER)) for item in _dicts(raw.get("adjudications"))]


def _supporting(raw: dict) -> list:
    return [_make(item.get("provided_time"), "supporting_info", _table(SUPPORTING_TEXT, item.get("source"), SUPPORTING_OTHER),
                  _files(item.get("documents"))) for item in _dicts(raw.get("supporting_info"))]


def _money_movements(raw: dict) -> list:
    out = []
    for item in _dicts(raw.get("money_movements")):
        amount = _money(item.get("amount"))
        kind = _table(MONEY_KIND, item.get("type"), MONEY_KIND_OTHER)
        party = MONEY_PARTY.get(item.get("affected_party")) if isinstance(item.get("affected_party"), str) else None
        reason = MONEY_REASON.get(item.get("reason")) if isinstance(item.get("reason"), str) else None
        out.append(_make(item.get("initiated_time"), "money", f"{kind} of {amount} was initiated" if amount
                         else f"{kind} was initiated",
                         ((f"Affects: {party}",) if party else ()) + ((f"Reason: {reason}",) if reason else ())))
    return out


def _contacts(raw: dict) -> list:
    extensions = raw.get("extensions")
    if not isinstance(extensions, dict):
        return []
    return [_make(extensions.get("merchant_contacted_time"), "contact", SELLER_CONTACT_TEXT),
            _make(extensions.get("buyer_contacted_time"), "contact", BUYER_CONTACT_TEXT)]


def verified_event(at, text, detail=()) -> Event:
    """An event from a source the caller has verified itself (the seam for W2-2). Text and detail are cleaned here."""
    lines = tuple(line for line in (_clean(entry, 200) for entry in (detail if isinstance(detail, (list, tuple)) else ())) if line)
    event = _make(at if isinstance(at, str) and at.strip() else None, "verified", _clean(text, 200), lines, ORIGIN_VERIFIED)
    return event if event is not None else Event(at="", kind="verified", text=_clean(text, 200), detail=lines,
                                                 origin=ORIGIN_VERIFIED)


WEBHOOK_TEXT = "PayPal notified Proofie"
WEBHOOK_STATUS_LINE = "Proofie's own read of the dispute afterwards: {status}"
WEBHOOK_NO_STATUS_LINE = "Proofie has no read of the dispute stored for this notice"
_STATUS_CODE = re.compile(r"[A-Z][A-Z_]{0,39}")


def webhook_events(rows) -> list:
    """Verified webhook notices (rows of copilot/features/webhooks/store.py, which the caller reads: this package imports no
    other feature) as events for `build(verified=...)`. Only a row whose verification is 'verified' counts; the time is when
    Proofie received it; the status is the one Proofie read from PayPal afterwards (`readback_status`), never anything the
    notice itself claimed, and only a plain status code is worded (an odd value reads as no stored read)."""
    events = []
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, dict) or row.get("verification") != "verified":
            continue
        status = row.get("readback_status")
        line = (WEBHOOK_STATUS_LINE.format(status=humanize_status(status))
                if isinstance(status, str) and _STATUS_CODE.fullmatch(status) else WEBHOOK_NO_STATUS_LINE)
        events.append(verified_event(row.get("received_at"), WEBHOOK_TEXT, (line,)))
    return events


def _utc(moment):
    if not isinstance(moment, datetime):
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def build(raw, read_at, verified=()) -> list:
    """PayPal's dated items, oldest first (by instant; PayPal's own order breaks ties), then those with no usable instant.
    `read_at` is when the dispute was read; an event dated later than it is flagged `after_read`. Never raises on a
    malformed dispute: what cannot be read is left out."""
    raw = raw if isinstance(raw, dict) else {}
    found = [*_contacts(raw), _make(raw.get("create_time"), "opened", OPENED_TEXT), *_evidence(raw), *_supporting(raw),
             *_messages(raw), *_offers(raw), *_adjudications(raw), *_money_movements(raw),
             _make(raw.get("update_time"), "updated", UPDATED_TEXT)]
    found += [replace(event, kind="verified", origin=ORIGIN_VERIFIED, instant=timefmt.instant(event.at))
              for event in (verified if isinstance(verified, (list, tuple)) else ()) if isinstance(event, Event)]
    events = [event for event in found if event is not None]
    events.sort(key=lambda event: (event.instant is None, event.instant or _FAR_PAST))
    read = _utc(read_at)
    if read is None:
        return events
    return [replace(event, after_read=True) if event.instant is not None and event.instant > read else event
            for event in events]
