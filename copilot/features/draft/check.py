"""Validation of a draft by code, against the fact sheet. Pure; no model. A draft that fails any rule is refused (stored with
its problems) and the fixed template stands.

Rules (each failure is one named problem): no empty text and none over 600 characters; every digit run of 3+ digits is in the
sheet's numbers; every ISO-looking date is the order date or inside a sheet literal; the tracking number (when the sheet has one)
appears verbatim in both texts and no other token that looks like a tracking number ([A-Z0-9]{10,}) appears; no link or email
(http, www., @); no promise word (guarantee, guaranteed, garantiz): the only promise allowed is that the buyer can reply here and the seller will look into it right away; and a cheap language sanity check (stated as such, not a language detector): the Spanish text holds one of pedido,
envío, paquete, entrega and the English one one of order, shipment, package, delivery. Refund wording is refused in both texts.
The bounded lexical echo rule refuses six consecutive words copied from the shipping address or another free-text literal
over 40 characters; it is not resistance to arbitrary injection. Numbers, dates, tracking and dispute IDs keep their own rules.
A draft never says the order was delivered or signed for. Signature words ("signed", "firmad[oa]") are always
refused; assertive delivery phrases ("was delivered", "has been delivered", "fue entregad[oa]", "ha sido entregad[oa]") are refused
unless the fact sheet's tracking status is DELIVERED (by code, from PayPal's tracker). Questions and conditional sentences pass.
`issues()` gives the same refusals as structured values (kind, language, value) for the page; `validate_draft` keeps its string tuple.
The fixed template writes the order date in words ("28 September 2026", resolution actions.date_words). Exactly
that string is the same fact as the sheet's ISO order date and is not counted as a spelled-out date; any other spelled date
(another day, format, case or language) is still refused. No rule reads carrier names, so the template's display names pass."""
from __future__ import annotations

import re
from dataclasses import dataclass

from copilot.features.draft.facts import DIGIT_RUN, FactSheet
from copilot.features.resolution.actions import date_words

MAX_LEN = 600
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Tracking-like (case-insensitive, tolerant of spaces and hyphens): alphanumeric pieces are chained when joined by a hyphen, or by ONE space
# when both neighbours contain a digit; a chain of 10+ alphanumerics that holds at least one digit is a tracking-like token. Words
# without digits never form one.
PIECE = re.compile(r"[A-Za-z0-9]+")
TRACKING_MIN_LEN = 10
# Spelled-out months, English and Spanish, full names and three-letter forms, any case. The ambiguous forms that are also ordinary words
# ("may", "mar", "ago") only count when a digit is next to them ("may 3", "3 mar").
MONTHS_FULL = ("january", "february", "march", "april", "june", "july", "august", "september", "october", "november", "december",
               "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "setiembre", "octubre",
               "noviembre", "diciembre")
MONTHS_SHORT = ("jan", "feb", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "ene", "abr", "dic")
MONTHS_AMBIGUOUS = ("may", "mar", "ago")
MONTH_WORD = re.compile(r"\b(?:" + "|".join(MONTHS_FULL + MONTHS_SHORT) + r")\b", re.IGNORECASE)
MONTH_WITH_DIGIT = re.compile(r"(?:\d\s*\.?\s*\b(?:" + "|".join(MONTHS_AMBIGUOUS) + r")\b|\b(?:" + "|".join(MONTHS_AMBIGUOUS)
                              + r")\b\.?\s*\d)", re.IGNORECASE)
SPELLED_DATE = "contains a spelled-out date (use the facts' ISO date)"
LINK_MARKERS = ("http", "www.", "@")
PROMISE_WORDS = ("guarantee", "guaranteed", "garantiz")
ES_WORDS = ("pedido", "envío", "paquete", "entrega")
EN_WORDS = ("order", "shipment", "package", "delivery")
REFUND_WORDS = ("refund", "reembols", "money back", "dinero")
ECHO_WORDS = re.compile(r"\w+", re.UNICODE)
# draft-specific. Not the evidence regex: that one would refuse ordinary questions that say "recibido".
SIGNATURE_CLAIM = re.compile(r"\bsigned\b|\bfirmad[oa]s?\b", re.IGNORECASE)
DELIVERY_CLAIM = re.compile(r"\b(?:was|has\s+been)\s+delivered\b|\b(?:fue|ha\s+sido)\s+entregad[oa]s?\b", re.IGNORECASE)
CONDITIONAL_WORD = re.compile(r"\b(?:if|unless|whether|si)\b", re.IGNORECASE)
SENTENCE_BREAKS = ".!?;\n"
DELIVERED_STATUS = "DELIVERED"


@dataclass(frozen=True)
class Validation:
    ok: bool
    problems: tuple = ()


@dataclass(frozen=True)
class Issue:
    """One refusal as data: its kind, the language of the text ("English" or "Spanish") and the offending value, when there is one."""
    kind: str
    language: str
    value: str | None = None


def _squash(value: str) -> str:
    """Upper-cased with spaces and hyphens removed: the form tracking-like tokens are compared in."""
    return re.sub(r"[ -]", "", value).upper()


def _tracking_chains(text: str) -> list:
    """(squashed token, the text as written, span) for each tracking-like chain: 10+ alphanumerics holding at least one digit."""
    chains, current, previous = [], [], None
    for match in PIECE.finditer(text):
        if previous is not None:
            gap = text[previous.end():match.start()]
            joined = gap == "-" or (gap == " " and any(c.isdigit() for c in previous.group(0)) and any(c.isdigit() for c in match.group(0)))
            if not joined:
                chains.append(current)
                current = []
        current.append(match)
        previous = match
    if current:
        chains.append(current)
    found = []
    for chain in chains:
        squashed = "".join(m.group(0) for m in chain).upper()
        if len(squashed) >= TRACKING_MIN_LEN and any(c.isdigit() for c in squashed):
            span = (chain[0].start(), chain[-1].end())
            found.append((squashed, text[span[0]:span[1]], span))
    return found


def tracking_like_tokens(text: str) -> list:
    """Squashed (upper-case, no spaces or hyphens) chains of 10+ alphanumerics holding at least one digit."""
    return [squashed for squashed, _written, _span in _tracking_chains(text)]


def _repeats_free_text(sheet: FactSheet, text: str) -> bool:
    """Six consecutive case-folded words; punctuation is a separator, digits are boundaries.

    Typed dates, amounts, currency, tracking and dispute IDs are excluded from echo sources;
    their existing validation rules still apply. Short shipping addresses are checked too.
    """
    typed = {v for v in (sheet.order_date, sheet.amount_text, sheet.currency,
                         sheet.tracking_number, sheet.delivery_estimate, sheet.dispute_id) if v}
    sources = [v for v in sheet.literals if v not in typed and len(v) > 40]
    if sheet.shipping_address_line:
        sources.append(sheet.shipping_address_line)
    tokens = ECHO_WORDS.findall(text.casefold())
    windows = {tuple(tokens[i:i + 6]) for i in range(len(tokens) - 5)
               if all(word.isalpha() for word in tokens[i:i + 6])}
    for value in sources:
        words = ECHO_WORDS.findall(value.casefold())
        if any(tuple(words[i:i + 6]) in windows for i in range(len(words) - 5)
               if all(word.isalpha() for word in words[i:i + 6])):
            return True
    return False


def _without_template_dates(sheet: FactSheet, text: str) -> str:
    """The text with the fixed template's own wording of the sheet's order date blanked out (digit-bounded, exact case), for the
    spelled-date rule only. Every other rule still reads the text as written."""
    words = date_words(sheet.order_date) if sheet.order_date else None
    if not words:
        return text
    return re.sub(r"(?<!\d)" + re.escape(words) + r"(?!\d)", " ", text)


def _sentence_bounds(text: str, position: int) -> tuple:
    """(start, terminator) of the sentence holding `position`; sentences end at . ! ? ; or a newline."""
    start = max(text.rfind(ch, 0, position) for ch in SENTENCE_BREAKS) + 1
    ends = [e for e in (text.find(ch, position) for ch in SENTENCE_BREAKS) if e != -1]
    end = min(ends) if ends else len(text)
    return start, (text[end] if end < len(text) else "")


def delivery_or_signature_claim(sheet: FactSheet, text: str) -> bool:
    """True when the text claims the order was delivered or signed for and PayPal's tracking in the sheet does not show a delivery.
    Signature claims are always refused. Delivery phrases are refused unless the sheet's tracking status is DELIVERED. A question
    ("Was it delivered?") or a conditional sentence ("If it was delivered, ...") is not a claim."""
    if SIGNATURE_CLAIM.search(text):
        return True
    if (sheet.tracking_status or "").strip().upper() == DELIVERED_STATUS:
        return False
    for match in DELIVERY_CLAIM.finditer(text):
        start, terminator = _sentence_bounds(text, match.start())
        if terminator == "?" or CONDITIONAL_WORD.search(text[start:match.start()]):
            continue
        return True
    return False


def _findings(sheet: FactSheet, text: str, label: str, words: tuple) -> list:
    """[(Issue, problem string, span or None)] in the order the problem strings have always had; the delivery claim is last."""
    found = []

    def add(kind, problem, value=None, span=None):
        found.append((Issue(kind, label, value), f"{label}: {problem}", span))

    if not text or not text.strip():
        return [(Issue("empty", label), f"{label}: the text is empty", None)]
    if len(text) > MAX_LEN:
        add("too_long", f"the text is over {MAX_LEN} characters")
    lowered = text.lower()
    if any(word in lowered for word in REFUND_WORDS):
        add("refund", "contains refund wording: the seller decides refunds from the page")
    if _repeats_free_text(sheet, text):
        add("echo", "repeats free text from a fact value")
    for match in DIGIT_RUN.finditer(text):
        if match.group(0) not in sheet.numbers:
            add("number", f"the number {match.group(0)} is not in the facts", match.group(0), match.span())
    for match in ISO_DATE.finditer(text):
        date = match.group(0)
        if date != sheet.order_date and not any(date in literal for literal in sheet.literals):
            add("date", f"the date {date} is not in the facts", date, match.span())
    if sheet.tracking_number:
        if sheet.tracking_number not in text:
            add("tracking_missing", "the tracking number is missing")
    allowed = {_squash(v) for v in (sheet.tracking_number, sheet.dispute_id) if v}
    for squashed, written, span in _tracking_chains(text):
        if squashed not in allowed:
            add("tracking_code", f"the tracking-like token {squashed} is not in the facts", written, span)
    spelled = _without_template_dates(sheet, text)
    if MONTH_WORD.search(spelled) or MONTH_WITH_DIGIT.search(spelled):
        add("spelled_date", SPELLED_DATE)
    for marker in LINK_MARKERS:
        if marker in lowered:
            add("link", f"contains {marker!r} (no links or emails)", marker)
    for word in PROMISE_WORDS:
        if word in lowered:
            add("promise", f"contains the promise word {word!r}", word)
    if not any(w in lowered for w in words):
        add("language", f"does not look like {'Spanish' if words is ES_WORDS else 'English'} (language check)")
    if delivery_or_signature_claim(sheet, text):
        add("delivery", "says delivered or signed for: PayPal's tracking in the facts does not show a delivery")
    return found


def _both(sheet: FactSheet, en: str, es: str) -> list:
    return _findings(sheet, en or "", "English", EN_WORDS) + _findings(sheet, es or "", "Spanish", ES_WORDS)


def validate_draft(sheet: FactSheet, en: str, es: str) -> Validation:
    problems = tuple(problem for _issue, problem, _span in _both(sheet, en, es))
    return Validation(not problems, problems)


def issues(sheet: FactSheet, en: str, es: str) -> list:
    """The refusals of `validate_draft` as Issue values (same verdict, same order). A digit-run issue is dropped when it lies inside
    a date or a tracking-like code reported for the same text, so one wrong date is one issue and not one per number in it."""
    result = []
    for label, text, words in (("English", en or "", EN_WORDS), ("Spanish", es or "", ES_WORDS)):
        found = _findings(sheet, text, label, words)
        date_spans = [span for issue, _problem, span in found if issue.kind in ("date", "tracking_code")]
        for issue, _problem, span in found:
            if issue.kind == "number" and any(a <= span[0] and span[1] <= b for a, b in date_spans):
                continue
            result.append(issue)
    return result
