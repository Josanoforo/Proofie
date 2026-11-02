"""Signed, self-contained buyer status link (D-101 round 3, B4).

The link carries a small payload the seller approved, signed with an operator secret. Nothing is stored and nothing
is read from PayPal: the page is built from the token alone. The payload has no dispute id, no buyer name, address,
e-mail or amount, and no copy of a PayPal response body.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

KEY_ENV = "COPILOT_STATUS_LINK_KEY"
BASE_ENV = "COPILOT_STATUS_LINK_BASE"
MIN_KEY_CHARS = 32
TTL_DAYS = 14
STATUSES = ("Preparing", "Shipped", "In transit", "Delivered")
FIELDS = ("carrier", "tracking_number", "status", "registered", "description", "locale")
TOKEN_PATH = re.compile(r"^/s/[A-Za-z0-9_.-]{20,1500}$")
_CARRIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&'/-]{1,39}\Z")
_TRACKING = re.compile(r"[A-Za-z0-9-]{6,40}\Z")
_DOMAIN = b"proofie-status-link-v1."


class LinkError(ValueError):
    """The seller's input cannot become a link; the message is shown to the seller."""


def signing_key() -> bytes | None:
    value = os.environ.get(KEY_ENV, "")
    return value.encode("utf-8") if len(value) >= MIN_KEY_CHARS else None


def public_base() -> str:
    """Where the buyer can reach the page (the hosted copy). Empty means: use the address the seller is on."""
    base = os.environ.get(BASE_ENV, "").strip().rstrip("/")
    parsed = urlparse(base)
    try:
        port = parsed.port
    except ValueError:
        return ""
    clean_origin = parsed.scheme == "https" and bool(parsed.hostname) and not (parsed.path or parsed.params or parsed.query or parsed.fragment or parsed.username)
    return base if clean_origin and (port is None or 0 < port < 65536) else ""


def clean(raw: dict) -> dict:
    """Validate the seller's fields; return exactly FIELDS, nothing else."""
    carrier = str(raw.get("carrier", "")).strip()
    if not _CARRIER.fullmatch(carrier):
        raise LinkError("Carrier: 2 to 40 letters, digits, spaces or . & ' / -.")
    tracking = str(raw.get("tracking_number", "")).strip()
    if not _TRACKING.fullmatch(tracking):
        raise LinkError("Tracking number: 6 to 40 letters, digits or hyphens.")
    status = str(raw.get("status", ""))
    if status not in STATUSES:
        raise LinkError("Choose one of the recorded tracking statuses.")
    registered = str(raw.get("registered", "")).strip()
    try:
        date.fromisoformat(registered)
    except ValueError as exc:
        raise LinkError("Date registered with PayPal: a valid calendar date (YYYY-MM-DD).") from exc
    description = " ".join(str(raw.get("description", "")).split())
    if not 1 <= len(description) <= 120:
        raise LinkError("Item description: 1 to 120 characters.")
    if ("@" in description or re.search(r"https?:|www\.", description, re.I)
            or re.search(r"\d[\d ().+-]{7,}\d", description) or any(ord(c) < 32 for c in description)):
        raise LinkError("Item description: no e-mail, link or phone/card-like number; describe the item only.")
    locale = str(raw.get("locale", "en"))
    if locale not in ("en", "es"):
        raise LinkError("Choose English or Spanish.")
    return {"carrier": carrier, "tracking_number": tracking, "status": status, "registered": registered,
            "description": description, "locale": locale}


def payload_sha256(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, body: str) -> str:
    return _b64(hmac.new(key, _DOMAIN + body.encode("ascii"), hashlib.sha256).digest())


def expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(days=TTL_DAYS)


def make_token(fields: dict, *, now: datetime | None = None) -> str:
    key = signing_key()
    if key is None:
        raise LinkError(f"The operator has not set {KEY_ENV} (at least {MIN_KEY_CHARS} characters); links cannot be signed.")
    fields = clean(fields)
    body = _b64(json.dumps({"v": 1, "exp": int(expiry(now).timestamp()), **fields},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_mac(key, body)}"


def read_token(token: str, *, now: datetime | None = None) -> dict | None:
    """The page fields, or None for a missing key, a tampered, unknown or expired token (callers never say which)."""
    key = signing_key()
    if key is None or not isinstance(token, str) or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    try:
        if not hmac.compare_digest(mac.encode("ascii"), _mac(key, body).encode("ascii")):
            return None
        data = json.loads(_unb64(body).decode("utf-8"))
        if data.get("v") != 1 or set(data) != {"v", "exp", *FIELDS}:
            return None
        if int((now or datetime.now(timezone.utc)).timestamp()) >= int(data["exp"]):
            return None
        fields = clean({k: data[k] for k in FIELDS})
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
    return {**fields, "expires": datetime.fromtimestamp(int(data["exp"]), timezone.utc).date().isoformat()}


class PageLimiter:
    """Process-local sliding window for the public path: per address and overall."""
    def __init__(self, per_address=30, overall=600, window=600.0):
        self.per_address, self.overall, self.window = per_address, overall, window
        self.hits: deque = deque()

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        while self.hits and now - self.hits[0][0] >= self.window:
            self.hits.popleft()
        if len(self.hits) >= self.overall or sum(a == address for _, a in self.hits) >= self.per_address:
            return False
        self.hits.append((now, address))
        return True


LIMITER = PageLimiter()
