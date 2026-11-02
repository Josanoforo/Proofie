"""Signature verification by PayPal itself.

The rule is PayPal's own, as stated in PayPal's public webhooks documentation: verify every
event with the endpoint POST /v1/notifications/verify-webhook-signature before trusting it. The body is the five PAYPAL-*
request headers (auth_algo, cert_url, transmission_id, transmission_sig, transmission_time), our webhook_id and the
event as parsed JSON (`webhook_event`). The verification changes nothing at PayPal. One POST through the injected client
(`client.call("POST", path, op, json_body=...)`, the pilot SandboxClient interface); no other HTTP code, no host string.
Never logs or stores a header, a signature or a body: callers keep hashes only."""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

VERIFY_PATH = "/v1/notifications/verify-webhook-signature"
VERIFY_OP = "webhook_verify"
HEADER_FIELDS = (("auth_algo", "paypal-auth-algo"), ("cert_url", "paypal-cert-url"),
                 ("transmission_id", "paypal-transmission-id"), ("transmission_sig", "paypal-transmission-sig"),
                 ("transmission_time", "paypal-transmission-time"))


@dataclass(frozen=True)
class Verification:
    status: str                       # verified | failed | rejected_headers | limitation
    limitation_text: str | None = None


def verification_body(headers, raw_body: bytes | str, webhook_id: str | None) -> dict | None:
    """The exact verification request body, or None when a header (case-insensitive), the webhook id is missing or the raw
    body is not a JSON object."""
    if not webhook_id:
        return None
    lowered = {str(k).lower(): v for k, v in dict(headers.items() if hasattr(headers, "items") else headers).items()}
    body = {}
    for field, header in HEADER_FIELDS:
        value = lowered.get(header)
        if not isinstance(value, str) or not value.strip():
            return None
        body[field] = value
    try:
        event = json.loads(raw_body)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    body["webhook_id"] = webhook_id
    body["webhook_event"] = event
    return body


def _failure_text(exc: Exception) -> str | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return "PayPal could not verify the signature right now. Try again in a few minutes."
    if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return "PayPal did not answer in time when verifying the signature."
    return None


def verify_signature(client, headers, raw_body: bytes | str, webhook_id: str | None) -> Verification:
    body = verification_body(headers, raw_body, webhook_id)
    if body is None:
        return Verification("rejected_headers")
    try:
        answer = client.call("POST", VERIFY_PATH, VERIFY_OP, json_body=body)
    except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
        text = _failure_text(exc)
        if text is None:
            raise
        return Verification("limitation", text)
    if isinstance(answer, dict) and answer.get("verification_status") == "SUCCESS":
        return Verification("verified")
    return Verification("failed")
