"""Minimal PayPal sandbox REST client for the phase-B pilot.

- Sandbox only: the base URL is hard-wired; live credentials are never used here.
- OAuth token kept in memory, never logged.
- Every POST carries a PayPal-Request-Id header; Proofie does not rely on it to prevent duplicates; callers pass it
  explicitly to test retries.
- Every call is appended to a sanitized JSONL evidence log (no tokens, secrets, emails or names).
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

SANDBOX = "https://api-m.sandbox.paypal.com"
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR_ENV = "PROOFIE_EVIDENCE_DIR"
DATA_DIR_ENV = "COPILOT_DATA_DIR"   # the same name as copilot/config.py's DATA_DIR_ENV (pilot/ does not import copilot)


def evidence_dir() -> Path:
    """The ONE runtime evidence folder: PROOFIE_EVIDENCE_DIR when it is set, otherwise <repo>/evidence
    (the default is unchanged). A relative value is resolved against the current directory, once, here. Every runtime
    writer (this client, the vision log, the step logs, the MCP smoke script) takes its folder from this function.
    When PROOFIE_EVIDENCE_DIR is unset and COPILOT_DATA_DIR is set, the folder is <COPILOT_DATA_DIR>/evidence, so one
    setting moves every local write of the app (database, uploads, this log) out of the code folder."""
    value = os.environ.get(EVIDENCE_DIR_ENV, "").strip()
    if value:
        return Path(value).expanduser().resolve()
    data = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(data).expanduser().resolve() / "evidence" if data else ROOT / "evidence"


EVIDENCE = evidence_dir()

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Fix at the root (2026-10-05; an exception to "pilot is historical", because copilot/
# uses this live): merchant_id/account_id were never redacted, confirmed present unredacted in 10 real
# evidence/*.jsonl files. Address sub-fields added as defense in depth for
# BOTH real PayPal address shapes this project actually calls, confirmed against real sandbox responses/
# recorded fixtures and PayPal's own Tracking API schema (2026-10-05) rather than assumed:
#   - Orders (/v2/checkout/orders) and the standard address_portable schema: address_line_1/2,
#     admin_area_1/2, postal_code - nested under a key literally called "address" (already redacted
#     wholesale by that entry below), but listed individually too in case a future response ever puts
#     them under a differently-named wrapper.
#   - Transaction Search (/v1/reporting/transactions): its OWN flat names - line1, line2, city, state,
#     postal_code (copilot/core/context.py's _transaction_search reads exactly these) - also nested under
#     "address" today, same defense-in-depth reasoning.
#   - Confirmed Tracking API's real schema (/v1/shipping/trackers) has account_id but no address/email/
#     phone/name fields at all - nothing to add there beyond account_id.
# (2026-10-05) account numbers. A real sandbox response (evidence/mcp_smoke.jsonl line 8, Transaction Search)
# carried the seller's `account_number` in clear because only `account_id` was listed. Added: account_number and
# the other obvious account-identifying names (bank/IBAN/routing numbers, payer/payee/merchant account ids).
# `shipping_address` is redacted wholesale like `address`.
# The list was checked on 2026-10-06 against the five pinned PayPal documents (their specs of that date).
# Matching rule unchanged: exact lower-case key, the whole value is replaced.
# Sorted within each group; letters name the document that carries the key: D customer-disputes, T tracking,
# S transaction-search, W webhooks, O orders. Free text is NOT covered by any key (stated limit).
_SENSITIVE_KEYS = {
    # credentials and app secrets (older entries; not schema properties)
    "access_token", "app_id", "authorization", "client_secret", "nonce",
    # names: given_name O S; surname O S; middle_name O S; full_name O S; alternate_full_name S; name D O T S W;
    # prefix O S; suffix O S; business_name O; payer_name S; user_name O (Venmo handle)
    "alternate_full_name", "business_name", "full_name", "given_name", "middle_name", "name", "payer_name",
    "prefix", "suffix", "surname", "user_name",
    # e-mail: email_address O S. `email` (D O) is deliberately not a key: its value is an e-mail, so the e-mail regex
    # below turns it into "[email]" (an existing test asserts that form); a key entry would replace it with "[redacted]".
    "email_address",
    # phone: phone O S; phone_number O S; national_number O S; extension_number O S
    "extension_number", "national_number", "phone", "phone_number",
    # address, whole objects: address D O S; address_details D O; shipping_address O; billing_address O;
    # secondary_shipping_address S; return_shipping_address D; return_shipping_address_info D
    "address", "address_details", "billing_address", "return_shipping_address", "return_shipping_address_info",
    "secondary_shipping_address", "shipping_address",
    # address parts: address_line_1/2/3 D O; admin_area_1..4 D O; postal_code D O S; ships_from_postal_code O;
    # line1 line2 city S; building_name street_name street_number street_type sub_building D O
    "address_line_1", "address_line_2", "address_line_3", "admin_area_1", "admin_area_2", "admin_area_3",
    "admin_area_4", "building_name", "city", "line1", "line2", "postal_code", "ships_from_postal_code",
    "street_name", "street_number", "street_type", "sub_building",
    # identifiers: payer_id O; account_id O T S; merchant_id D O; account_number S; paypal_account_id S;
    # auction_buyer_id S; merchant_customer_id O; consumer_reference O; alias_key alias_label O (Blik);
    # tax_id O; birth_date O; plus older names not present as schema properties
    "account_id", "account_no", "account_num", "account_number", "alias_key", "alias_label", "auction_buyer_id",
    "bank_account_number", "birth_date", "consumer_reference", "iban", "merchant_account_id", "merchant_customer_id",
    "merchant_id", "paypal_account_id", "payee_account_id", "payer_account_id", "payer_id", "routing_number", "tax_id",
    # network and device: ip_address consumer_ip buyer_user_agent device_manufacturer_id O; consumer_user_agent O
    "buyer_user_agent", "consumer_ip", "consumer_user_agent", "device_manufacturer_id", "ip_address",
    # payment instrument and credential values: number expiry security_code last_digits O; card_last_digits
    # iban_last_chars O; payment_instrument_suffix D; pin decrypted_token single_use_token cryptogram vault_id token O;
    # bin O (bin_details/properties/bin, the card's Bank Identification Number)
    "bin", "card_last_digits", "cryptogram", "decrypted_token", "expiry", "iban_last_chars", "last_digits", "number",
    "payment_instrument_suffix", "pin", "security_code", "single_use_token", "token", "vault_id",
    # customer, whole object like an address wrapper: O (card_attributes, paypal_wallet_attributes, apple_pay_attributes,
    # venmo_wallet_attributes, vault_response and the vault responses carry a `customer` object whose generic `id` is a
    # person identifier that no exact-key rule can name). Nothing in copilot/ or pilot/ reads a `customer` key.
    "customer"}

# `state` is personal data only as part of an address (Transaction Search's shipping_info.address.state, or
# any object that carries address keys). A checklist item's `state`, a dispute/attempt state and similar statuses
# are not personal data and must stay readable in the evidence. So `state` is redacted only when its own object
# looks like an address: it holds one of these keys, or it sits under a key named address/shipping_address.
_ADDRESS_MARKER_KEYS = {"address_line_1", "address_line_2", "admin_area_1", "admin_area_2", "postal_code",
                        "country_code", "line1", "line2", "city",
                        # other shapes an address can take
                        "zip", "zip_code", "postcode", "post_code", "province", "region"}
_ADDRESS_WRAPPER_KEYS = {"address", "shipping_address"}


def sanitize(value: Any, _parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        address_like = (_parent_key in _ADDRESS_WRAPPER_KEYS
                        or bool(_ADDRESS_MARKER_KEYS & {k.lower() for k in value}))
        out = {}
        for k, v in value.items():
            lowered = k.lower()
            if lowered in _SENSITIVE_KEYS or (lowered == "state" and address_like):
                out[k] = "[redacted]"
            else:
                out[k] = sanitize(v, lowered)
        return out
    if isinstance(value, list):
        return [sanitize(v, _parent_key) for v in value]
    if isinstance(value, str):
        return _EMAIL.sub("[email]", value)
    return value


# The correlation schema contains only existing hashes and remote status words.
_AUDIT_STATUSES = frozenset({"OPEN", "WAITING_FOR_SELLER_RESPONSE", "WAITING_FOR_BUYER_RESPONSE",
                            "UNDER_REVIEW", "RESOLVED", "SHIPPED", "DELIVERED", "ON_HOLD", "CANCELLED",
                            "LOCAL_PICKUP", "NEW", "SHIPPED_RETURNED"})

# exact read-back families only; other audit/send/error lines retain their body.
_KEYS_ONLY_READBACK_OPS = frozenset({"resolution_readback", "resolution_poll", "fulfilment_tracker_get"})
_READBACK_AUDIT_ORIGIN = object()


def write_audit(request_sha256: str, approval_sha256: str | None = None) -> dict:
    def valid_sha(value):
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None
    result = {"request_sha256": valid_sha(request_sha256)}
    if approval_sha256 is not None:
        result["approval_sha256"] = valid_sha(approval_sha256)
    return result


def readback_audit(request_sha256: str, approval_sha256: str | None = None,
                   *, tracking_number: str | None = None) -> dict:
    """Resolve status only after a successful GET. The tracker selector is never logged."""
    def status_from(body):
        if not isinstance(body, dict):
            return None
        if tracking_number is None:
            return body.get("status")
        trackers = body.get("trackers")
        if not isinstance(trackers, list):
            return None
        matches = [row for row in trackers if isinstance(row, dict)
                   and row.get("tracking_number") == tracking_number]
        return matches[0].get("status") if len(matches) == 1 else None
    # Private selector metadata never enters the serialized extra dictionary.
    status_from._audit_origin = _READBACK_AUDIT_ORIGIN
    status_from._tracking_number = tracking_number
    return {**write_audit(request_sha256, approval_sha256), "settled_status": status_from}


def _readback_body_fields(extra: dict, method: str, op: str, status: int, body) -> dict:
    selector = extra.get("settled_status")
    if not (method.upper() == "GET" and 200 <= status < 300 and op in _KEYS_ONLY_READBACK_OPS
            and getattr(selector, "_audit_origin", None) is _READBACK_AUDIT_ORIGIN):
        return {"body": body}
    safe = sanitize(body)
    keys = {}
    if isinstance(safe, dict):
        keys = {k: safe[k] for k in ("dispute_id", "status", "dispute_life_cycle_stage", "seller_response_due_date")
                if k in safe}
        outcome = safe.get("dispute_outcome")
        if isinstance(outcome, dict) and "outcome_code" in outcome:
            keys["dispute_outcome"] = {"outcome_code": outcome["outcome_code"]}
        links = safe.get("links")
        if isinstance(links, list):
            keys["links"] = [{"rel": rel} for rel in sorted(row["rel"] for row in links
                             if isinstance(row, dict) and isinstance(row.get("rel"), str))]
        if op == "fulfilment_tracker_get" and isinstance(body, dict):
            trackers = body.get("trackers")
            if isinstance(trackers, list):
                keys["trackers"] = [sanitize({k: row[k] for k in ("tracking_number", "status", "carrier") if k in row})
                                    for row in trackers if isinstance(row, dict)
                                    and row.get("tracking_number") == getattr(selector, "_tracking_number", None)]
    digest = hashlib.sha256(json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                            .encode("utf-8")).hexdigest()
    return {"body_keys": keys, "body_sha256": digest}


def _resolved_log_extra(extra: dict, method: str, status: int | None, body=None) -> dict:
    result = dict(extra)
    value = result.get("settled_status")
    if callable(value):
        value = value(body) if method.upper() == "GET" and status is not None and 200 <= status < 300 else None
        result["settled_status"] = value if isinstance(value, str) and value in _AUDIT_STATUSES else None
    return result


class PayPalError(RuntimeError):
    def __init__(self, status: int, issue: str | None, debug_id: str | None, body: Any):
        super().__init__(f"PayPal {status} issue={issue} debug_id={debug_id}")
        self.status, self.issue, self.debug_id, self.body = status, issue, debug_id, body


class SandboxClient:
    def __init__(self, log_name: str, timeout_s: float = 30.0):
        load_dotenv(ROOT / ".env", override=False)
        self._id = os.environ.get("PAYPAL_CLIENT_ID", "")
        self._secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
        if not self._id or not self._secret:
            raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET missing in .env")
        self._http = httpx.Client(base_url=SANDBOX, timeout=timeout_s)
        self._token: str | None = None
        self._token_expiry = 0.0
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.log_path = EVIDENCE / f"{log_name}.jsonl"

    @property
    def base_url(self) -> str:
        """The one host this client talks to (the PayPal sandbox); published so a write path can assert it."""
        return str(self._http.base_url)

    def _log(self, record: dict) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sanitize(record), ensure_ascii=False) + "\n")

    def token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        t0 = time.perf_counter()
        try:
            r = self._http.post("/v1/oauth2/token", auth=(self._id, self._secret),
                                data={"grant_type": "client_credentials"})
        except httpx.TransportError as exc:
            # the same sanitised line call() writes for a transport failure. Only the exception
            # CLASS is recorded - never the credentials, the request body or the exception text.
            ms = round((time.perf_counter() - t0) * 1000)
            self._log({"op": "oauth_token", "method": "POST", "path": "/v1/oauth2/token", "status": None, "ms": ms,
                       "transport_error": type(exc).__name__, "outcome": "unknown"})
            raise
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            body = _json(r)
            self._log({"op": "oauth_token", "status": r.status_code, "ms": ms, "error": body.get("error"),
                       "error_description": body.get("error_description")})
            raise PayPalError(r.status_code, body.get("error"), r.headers.get("paypal-debug-id"), body)
        data = r.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 0))
        self._log({"op": "oauth_token", "status": 200, "ms": ms, "expires_in": data.get("expires_in"),
                   "scopes_count": len(data.get("scope", "").split()),
                   "has_disputes_scope": "disputes" in data.get("scope", "")})
        return self._token

    RELEVANT_SCOPES = ("disputes/create", "disputes/read-seller", "disputes/update-seller",
                       "disputes/read-buyer", "disputes/update-buyer")

    def scopes(self) -> list[str]:
        """Return the scope list of the current app token. Logs only a count and whether each scope in
        RELEVANT_SCOPES is present (never the token, never the full scope list: scope URLs can be
        identifying)."""
        t0 = time.perf_counter()
        r = self._http.post("/v1/oauth2/token", auth=(self._id, self._secret),
                            data={"grant_type": "client_credentials"})
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            body = _json(r)
            self._log({"op": "scopes", "status": r.status_code, "ms": ms, "error": body.get("error")})
            raise PayPalError(r.status_code, body.get("error"), r.headers.get("paypal-debug-id"), body)
        scope_list = r.json().get("scope", "").split()
        present = {s: any(s in full for full in scope_list) for s in self.RELEVANT_SCOPES}
        self._log({"op": "scopes", "status": 200, "ms": ms, "scopes_count": len(scope_list),
                   "relevant_scopes_present": present})
        return scope_list

    def call(self, method: str, path: str, op: str, *, json_body: Any = None, request_id: str | None = None,
             extra_headers: dict | None = None, timeout_s: float | None = None,
             multipart: dict | None = None, log_extra: dict | None = None) -> dict:
        """multipart: httpx `files` mapping, e.g. {"input": (None, json_text, "application/json")}; the JSON
        Content-Type is then left to httpx so the boundary is set. log_extra is merged into the evidence line;
        readback_audit's status selector is resolved after a successful GET, never serialized or sent."""
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
                   "Prefer": "return=representation"}
        if multipart is not None:
            headers.pop("Content-Type")
        if method.upper() == "POST":
            headers["PayPal-Request-Id"] = request_id or str(uuid.uuid4())
        if extra_headers:
            headers.update(extra_headers)
        extra = log_extra or {}
        t0 = time.perf_counter()
        try:
            r = self._http.request(method, path, json=None if multipart is not None else json_body,
                                   files=multipart, headers=headers,
                                   timeout=timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TransportError as exc:
            ms = round((time.perf_counter() - t0) * 1000)
            self._log({"op": op, "method": method, "path": path, "status": None, "ms": ms,
                       "request_id": headers.get("PayPal-Request-Id"),
                       "transport_error": type(exc).__name__, "outcome": "unknown",
                       **_resolved_log_extra(extra, method, None)})
            raise
        ms = round((time.perf_counter() - t0) * 1000)
        body = _json(r)
        debug_id = r.headers.get("paypal-debug-id") or (body.get("debug_id") if isinstance(body, dict) else None)
        issue = None
        if r.status_code >= 400 and isinstance(body, dict):
            details = body.get("details") or []
            issue = details[0].get("issue") if details else body.get("name")
        self._log({"op": op, "method": method, "path": path, "status": r.status_code, "ms": ms,
                   "request_id": headers.get("PayPal-Request-Id"), "debug_id": debug_id, "issue": issue,
                   **_resolved_log_extra(extra, method, r.status_code, body),
                   **_readback_body_fields(extra, method, op, r.status_code, body)})
        if r.status_code >= 400:
            raise PayPalError(r.status_code, issue, debug_id, body)
        return body


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"non_json_length": len(r.content)}
