"""PayPal's refusals in a seller's words.

PayPal answers a refused request with an HTTP status and, usually, an `issue` code. A seller cannot act on
"error 400" or on a code in capitals, so the visible sentence says what went wrong when the issue is known and says that
PayPal did not say why when it is not. The status, the issue code and PayPal's own description stay available for a
"Technical details" disclosure (technical_of, parse_technical). Nothing here calls PayPal or decides a state."""
from __future__ import annotations

import re

import httpx

# Issue codes PayPal returns for the requests Proofie makes, in a seller's words. A code that is not listed here is
# never guessed at: the caller falls back to PayPal's own description or to NOT_SAID.
ISSUE_WORDS = {
    "INVALID_TRACKING_NUMBER": "PayPal says the tracking number format is not valid for this carrier. Check the number on the carrier's receipt.",
    "INVALID_REQUEST": "PayPal says something in this request is not valid.",
    "INVALID_PARAMETER_VALUE": "PayPal says one value in this request is not valid.",
    "MISSING_REQUIRED_PARAMETER": "PayPal says a required value is missing from this request.",
    "MALFORMED_REQUEST": "PayPal could not read this request.",
    "MALFORMED_REQUEST_JSON": "PayPal could not read this request.",
    "INVALID_STRING_LENGTH": "PayPal says a text in this request is too short or too long.",
    "INVALID_RESOURCE_ID": "PayPal says the dispute or sale reference is not valid.",
    "RESOURCE_NOT_FOUND": "PayPal has no record of this dispute or sale.",
    "DISPUTE_ALREADY_RESOLVED": "PayPal says this dispute is already resolved.",
    "PERMISSION_DENIED": "PayPal did not allow this app to do that (the app may be missing that permission).",
    "NOT_AUTHORIZED": "PayPal did not allow this app to do that (the app may be missing that permission).",
    "AUTHORIZATION_ERROR": "PayPal did not allow this app to do that (the app may be missing that permission).",
    "DUPLICATE_REQUEST_ID": "PayPal says it already received this request.",
}
NOT_SAID = "PayPal did not say why; the technical details below show its answer."
NOT_SAID_ABOVE = "PayPal did not say why; the technical details show its answer."


def issue_of(exc) -> str | None:
    """The `issue` code of a PayPalError-like exception: its own attribute, else the first entry of body.details."""
    issue = getattr(exc, "issue", None)
    if isinstance(issue, str) and issue.strip():
        return issue.strip()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        details = body.get("details")
        first = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
        found = first.get("issue") or body.get("name")
        if isinstance(found, str) and found.strip():
            return found.strip()
    return None


def cause_of(issue: str | None) -> str | None:
    """The seller's-words sentence for a known issue code; None for a missing or unknown one."""
    return ISSUE_WORDS.get(str(issue or "").strip().upper())


def could_not(exc, what: str) -> str | None:
    """One plain sentence for an expected PayPal failure while trying to `what`, with the cause when PayPal gave a known
    one. No status in the text. None for an exception that is a programming error."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        if status in (401, 403):
            return f"PayPal did not allow this app to {what} (the app may be missing that permission)."
        if status >= 500 or status == 429:
            return f"PayPal could not {what} right now. Try again in a few minutes."
        cause = cause_of(issue_of(exc))
        return f"PayPal could not {what}. {cause}" if cause else f"PayPal could not {what}."
    if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return f"PayPal did not answer in time when trying to {what}."
    return None


def technical_of(exc) -> str:
    """What belongs under Technical details: the HTTP status and the issue code PayPal gave (never a debug id's meaning)."""
    parts = []
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        parts.append(f"HTTP status {status}")
    issue = issue_of(exc)
    if issue:
        parts.append(f"PayPal issue {issue}")
    return "; ".join(parts)


_TECHNICAL = re.compile(r"status=(\d+)(?:\s+issue=(\S+))?")


def cause_in_technical(technical) -> str | None:
    """The seller's-words cause for the issue named in a technical line from technical_of; None when none is known."""
    match = re.search(r"PayPal issue (\S+)", technical or "")
    return cause_of(match.group(1)) if match else None


def parse_technical(text) -> tuple:
    """(status, issue) from a stored technical line such as 'status=400 issue=INVALID_REQUEST debug_id=x'; None when absent."""
    match = _TECHNICAL.search(text) if isinstance(text, str) else None
    if not match:
        return None, None
    issue = match.group(2)
    return int(match.group(1)), (None if not issue or issue == "None" else issue)


def reason_words(reason) -> str:
    """A stored reason such as 'INVALID_TRACKING_NUMBER: The tracking number format is not valid' as one sentence for the
    page. A known code gives its sentence; an unknown code gives PayPal's own description when there is one; otherwise NOT_SAID."""
    text = str(reason or "").strip()
    if not text:
        return NOT_SAID
    code, separator, description = text.partition(": ")
    known = cause_of(code)
    if known:
        return known
    if not separator:
        return NOT_SAID if re.fullmatch(r"[A-Z0-9_]+", code) else f"PayPal said: {code.rstrip('.')}."
    return f"PayPal said: {description.strip().rstrip('.')}."


__all__ = ["ISSUE_WORDS", "NOT_SAID", "NOT_SAID_ABOVE", "issue_of", "cause_of", "could_not", "technical_of",
           "cause_in_technical", "parse_technical", "reason_words"]
