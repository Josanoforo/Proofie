"""Register one tracker with PayPal and read it back. I/O only through the injected client
(`client.call(method, path, op, ...)`, the pilot SandboxClient interface); no host string, no other HTTP code.

Order: POST /v1/shipping/trackers (op fulfilment_tracker_post), then GET /v1/shipping/trackers?transaction_id=...
(op fulfilment_tracker_get). `confirmed` is true only when the GET lists the same tracking number. Every expected
PayPal failure becomes a Limitation; programming errors still raise. The approval guard is pure: a call whose
approval sha256 differs from the body's own hash raises ApprovalMismatch before the client is touched."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

from copilot.core import write_guard
from copilot.core import paypal_words
from copilot.core.context import SRC_TRACKING, Limitation
from copilot.core.disputes import readback_audit, write_audit
from copilot.features.fulfilment import store
from copilot.features.fulfilment.tracker_request import TRACKER_PATH, request_sha256

POST_OP = "fulfilment_tracker_post"
GET_OP = "fulfilment_tracker_get"
_REQUEST_NS = uuid.UUID("5c2a0f0e-3b86-4b0e-9c11-6a1d6f1e7a42")

STATUS_REGISTERED = "registered"
STATUS_UNCONFIRMED = "registered_unconfirmed"
STATUS_UNKNOWN = "unknown"       # transport failure, 5xx or 429: PayPal may or may not have saved it
STATUS_FAILED = "failed"         # only a definite 4xx with PayPal's reason: nothing was registered


class ApprovalMismatch(ValueError):
    """The approval does not cover this exact body: nothing was sent."""


SANDBOX_HOST = "api-m.sandbox.paypal.com"


class HostRefused(RuntimeError):
    """The client talks to a host other than the PayPal sandbox: nothing was sent."""


def assert_sandbox_host(client) -> None:
    """Refuse a write through a client whose base URL is not the PayPal sandbox over https. SandboxClient hard-wires that
    URL and publishes it as `base_url`; this check is the second lock if a client is ever built differently. A client
    with no base_url (a test double that makes no network call) is not a network client and passes."""
    base = getattr(client, "base_url", None)
    if base is None:
        return
    parsed = urlsplit(str(base))
    if parsed.scheme != "https" or parsed.hostname != SANDBOX_HOST or parsed.port not in (None, 443):
        raise HostRefused("Registering a tracker is allowed only against the PayPal sandbox; nothing was sent.")


@dataclass(frozen=True)
class RegisterResult:
    status: str                                  # registered | registered_unconfirmed | unknown | failed
    post_status: int | None                      # the HTTP status of a refused POST; None when it was accepted (2xx)
    tracker: dict | None                         # as read back
    confirmed: bool
    limitations: tuple = ()
    request_sha256: str = ""
    post_accepted: bool = False                  # the POST returned 2xx
    reason: str | None = None                    # PayPal's own reason for a definite refusal (status failed)


def _failure_message(exc: Exception, what: str) -> str | None:
    status = getattr(exc, "status", None)
    if not isinstance(status, int) and isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return f"PayPal did not answer in time when trying to {what}; what happened is unknown."
    return paypal_words.could_not(exc, what)


def _definite_status(status) -> bool:
    """A 4xx other than 429: PayPal read the request and refused it. Everything else leaves the outcome open."""
    return isinstance(status, int) and 400 <= status < 500 and status != 429


def _refusal_reason(exc: Exception) -> str | None:
    """PayPal's reason for a refusal as text (its issue code and description), never its debug id."""
    body = getattr(exc, "body", None)
    parts = []
    if isinstance(body, dict):
        details = body.get("details")
        first = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
        parts = [first.get("issue"), first.get("description") or body.get("message")]
    if not any(parts):
        parts = [getattr(exc, "issue", None)]
    text = ": ".join(str(p).strip() for p in parts if p and str(p).strip())
    text = " ".join(text.split())[:200].rstrip(" .")
    return text or None


def _unknown_message(exc: Exception) -> str | None:
    """The honest sentence for a POST whose outcome is not known; None when the exception is not one of those."""
    status = getattr(exc, "status", None)
    if isinstance(status, int) and not _definite_status(status):
        return ("PayPal answered with an error when Proofie asked it to register the tracking; "
                "what happened is unknown.")
    if not isinstance(status, int) and isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return "PayPal did not answer in time when trying to register the tracking; what happened is unknown."
    return None


def _links(tracker: dict) -> list:
    return [str(link.get("rel")) for link in tracker.get("links") or [] if isinstance(link, dict) and link.get("rel")]


def _readback(tracker: dict) -> dict:
    return {"tracking_number": tracker.get("tracking_number"), "status": tracker.get("status"),
            "carrier": tracker.get("carrier"), "shipment_date": tracker.get("shipment_date"),
            "last_updated_time": tracker.get("last_updated_time"), "link_rels": _links(tracker)}


def read_trackers_listing(client, capture_id: str, *, log_extra: dict | None = None) -> tuple[list, tuple, bool]:
    """GET the trackers of a capture: (list of tracker dicts, limitations, complete). `complete` is True only when PayPal
    answered with a normal listing (a `trackers` list): only such a read can show that a tracker is absent (the release
    rule of copilot/core/write_guard.py)."""
    path = f"{TRACKER_PATH}?{urlencode({'transaction_id': capture_id})}"
    try:
        body = client.call("GET", path, GET_OP, **({"log_extra": log_extra} if log_extra is not None else {}))
    except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
        msg = _failure_message(exc, "read the tracker back")
        if msg is None:
            raise
        return [], (Limitation(SRC_TRACKING, msg, paypal_words.technical_of(exc)),), False
    listing = body.get("trackers") if isinstance(body, dict) else None
    return [t for t in (listing or []) if isinstance(t, dict)], (), isinstance(listing, list)


def read_trackers(client, capture_id: str, *, log_extra: dict | None = None) -> tuple[list, tuple]:
    """GET the trackers of a capture: (list of tracker dicts, limitations)."""
    items, limits, _complete = read_trackers_listing(client, capture_id, log_extra=log_extra)
    return items, limits


def register_tracker(client, body: dict, approval_sha256: str) -> RegisterResult:
    assert_sandbox_host(client)
    sha = request_sha256(body)
    if approval_sha256 != sha:
        raise ApprovalMismatch("The approval does not match this request; nothing was sent.")
    tracker = body["trackers"][0]
    capture_id, number = tracker["transaction_id"], tracker["tracking_number"]
    request_id = str(uuid.uuid5(_REQUEST_NS, sha))      # the same body always carries the same id (not relied on to prevent duplicates)
    try:
        posted = client.call("POST", TRACKER_PATH, POST_OP, json_body=body, request_id=request_id,
                             log_extra=write_audit(sha, approval_sha256))
    except Exception as exc:  # noqa: BLE001
        unknown = _unknown_message(exc)
        if unknown is not None:
            # a transport failure, a 5xx or a 429 is NOT a refusal. PayPal may have saved the tracker.
            return RegisterResult(STATUS_UNKNOWN, getattr(exc, "status", None), None, False,
                                  (Limitation(SRC_TRACKING, unknown),), sha)
        msg = _failure_message(exc, "register the tracking")
        if msg is None:
            raise
        return RegisterResult(STATUS_FAILED, getattr(exc, "status", None), None, False,
                              (Limitation(SRC_TRACKING, msg, paypal_words.technical_of(exc)),), sha, False, _refusal_reason(exc))
    post_status = None   # SandboxClient.call returns only the body for any 2xx and raises PayPalError for >= 400
    limits: list = []
    if isinstance(posted, dict) and posted.get("errors"):
        limits.append(Limitation(SRC_TRACKING, "PayPal reported a problem with the tracker it was sent."))
    items, read_limits = read_trackers(client, capture_id,
                                     log_extra=readback_audit(sha, approval_sha256, tracking_number=number))
    limits.extend(read_limits)
    match = next((t for t in items if str(t.get("tracking_number", "")).strip() == number), None)
    if match is None:
        if not read_limits:
            limits.append(Limitation(SRC_TRACKING, "PayPal answered OK; not listed yet."))
        return RegisterResult(STATUS_UNCONFIRMED, post_status, None, False, tuple(limits), sha, True)
    return RegisterResult(STATUS_REGISTERED, post_status, _readback(match), True, tuple(limits), sha, True)


@dataclass(frozen=True)
class AlreadyRegistered:
    """This exact tracker body was already sent to PayPal (or is being sent, or its end is unknown):
    nothing was POSTed. `state` is the write_guard state (reserved | sent | unknown)."""
    state: str
    request_sha256: str


ACTION = "tracker"


def _definite_refusal(post_status) -> bool:
    return _definite_status(post_status)


def register_once(client, body: dict, approval_sha256: str) -> RegisterResult | AlreadyRegistered:
    """register_tracker, at most once per approved body. The app does not rely on PayPal deduplicating by the request
    id (in the sandbox PayPal did not deduplicate send-message by it, observed 2026-10-16), so it reserves (capture id, action, body sha256) atomically in SQLite before the POST (copilot/core/
    write_guard.py). A repeat finds the key taken and makes no PayPal call. A definite PayPal refusal (a 4xx other
    than 429) frees the key for a new approval; anything else leaves it taken: the seller checks PayPal."""
    assert_sandbox_host(client)
    sha = request_sha256(body)
    if approval_sha256 != sha:
        raise ApprovalMismatch("The approval does not match this request; nothing was sent.")
    capture_id = body["trackers"][0]["transaction_id"]
    # the stored file is evidence too. A file of this exact body whose POST could have reached
    # PayPal blocks a new send even when no reservation row exists (a reservation lost with the database, or a file
    # restored from a backup): nothing is posted.
    earlier = store.latest_sent_for_request(capture_id, sha)
    if earlier is not None:
        return AlreadyRegistered("sent" if store.effective_status(earlier) != "unknown" else write_guard.UNKNOWN, sha)
    claim = write_guard.reserve(capture_id, ACTION, sha)
    if not claim.reserved:
        return AlreadyRegistered(claim.state or write_guard.UNKNOWN, sha)
    final = write_guard.UNKNOWN
    try:
        result = register_tracker(client, body, approval_sha256)
        if result.status == STATUS_FAILED:
            final = write_guard.REFUSED if _definite_refusal(result.post_status) else write_guard.UNKNOWN
        elif result.status == STATUS_UNKNOWN:
            final = write_guard.UNKNOWN
        else:
            final = write_guard.SENT
        return result
    finally:
        write_guard.finish(capture_id, ACTION, sha, final)
