"""One webhook delivery, handled in a fixed order. The dispute state is never taken from the event body: after a VERIFIED dispute
event we read the dispute ourselves (one GET) and store that status.

Verification sub-flag off: validated events are stored unverified (202 skipped), no client call.
Order with verification on, by code: webhook id unset -> 503 (nothing stored, no call); body over MAX_BODY_BYTES -> 413 (nothing stored); not a
JSON object, or no usable `id` / `event_type` -> 400 (nothing stored); an event id already stored AND verified -> 200
`duplicate` (PayPal retries; no call); then verify with PayPal (verify.py): rejected_headers -> 400 stored, failed -> 400
stored (no GET), limitation -> 503 stored (no GET; PayPal retries and the retry replaces this row); verified -> for the
three dispute event types with a `resource.dispute_id` that passes the shared validator, one GET and the stored
`readback_status` is that GET's `status`; other verified types are stored with no GET; then 200. At most two PayPal calls,
each bounded by the client's own timeout; no thread, no queue: the local-first simplification of "answer 200 fast".
Never logs or stores a header, a signature or a body (hashes only)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from copilot import config
from copilot.core.send import sha256_json
from copilot.features.webhooks import store
from copilot.features.webhooks.verify import verify_signature
from copilot.web.security import validate_dispute_id

MAX_BODY_BYTES = 65536
MAX_FIELD_LEN = 128
DISPUTE_EVENTS = ("CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED")
READBACK_OP = "webhook_readback"
NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class Handled:
    http_status: int
    verification: str
    event_type: str | None = None
    dispute_id: str | None = None
    readback_status: str | None = None


def _text(value, limit: int = MAX_FIELD_LEN) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= limit else None


def _failure_text(exc: Exception) -> str | None:
    import httpx
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return "PayPal could not return the dispute right now. Try again in a few minutes."
    if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return "PayPal did not answer in time when reading the dispute."
    return None


def handle_event(client, db_path, headers, raw_body: bytes, webhook_id: str | None) -> Handled:
    verify_enabled = config.feature_enabled("webhooks_verify")
    if verify_enabled and webhook_id is None:
        return Handled(503, NOT_CHECKED)
    if len(raw_body) > MAX_BODY_BYTES:
        return Handled(413, NOT_CHECKED)
    try:
        event = json.loads(raw_body)
    except (ValueError, TypeError):
        return Handled(400, NOT_CHECKED)
    event_id = _text(event.get("id")) if isinstance(event, dict) else None
    event_type = _text(event.get("event_type")) if isinstance(event, dict) else None
    if event_id is None or event_type is None:
        return Handled(400, NOT_CHECKED)
    existing = store.get_event(event_id)
    if verify_enabled and existing is not None and existing["verification"] == "verified":
        return Handled(200, "duplicate", existing["event_type"], existing["dispute_id"], existing["readback_status"])

    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    candidate = resource.get("dispute_id")
    dispute_id = candidate if isinstance(candidate, str) and validate_dispute_id(candidate) is None else None
    resource_type = _text(event.get("resource_type"), 64)
    transmission_id = None
    try:
        transmission_id = _text(next((v for k, v in headers.items() if str(k).lower() == "paypal-transmission-id"), None))
    except AttributeError:
        pass
    body_sha = hashlib.sha256(raw_body).hexdigest()

    def save(verification: str, **kw) -> None:
        store.save_event(event_id, event_type, resource_type, dispute_id, transmission_id, body_sha, verification, **kw)

    if not verify_enabled:
        # A verified historical row stays trusted only as historical data; this delivery
        # remains skipped and does not inherit a readback or overwrite that row.
        if existing is None or existing["verification"] != "verified":
            save("unverified")
        return Handled(202, "skipped", event_type, dispute_id)

    result = verify_signature(client, headers, raw_body, webhook_id)
    if result.status == "rejected_headers":
        save("rejected_headers")
        return Handled(400, "rejected_headers", event_type, dispute_id)
    if result.status == "failed":
        save("failed")
        return Handled(400, "failed", event_type, dispute_id)
    if result.status == "limitation":
        save("limitation", limitation_text=result.limitation_text)
        return Handled(503, "limitation", event_type, dispute_id)

    readback_status = readback_sha = limitation = None
    if event_type in DISPUTE_EVENTS and dispute_id is not None:
        try:
            body = client.call("GET", f"/v1/customer/disputes/{dispute_id}", READBACK_OP)
        except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
            limitation = _failure_text(exc)
            if limitation is None:
                raise
        else:
            status = body.get("status") if isinstance(body, dict) else None
            readback_status = status if isinstance(status, str) else None
            readback_sha = sha256_json(body)
    save("verified", readback_status=readback_status, readback_sha256=readback_sha, limitation_text=limitation)
    return Handled(200, "verified", event_type, dispute_id, readback_status)
