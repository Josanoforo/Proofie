"""Transaction context for a dispute: what PayPal itself can tell us beyond the dispute detail.

The dispute detail has neither the shipping address nor the tracking. Two more PayPal sources are tried,
GET only:
  * Transaction Search - GET /v1/reporting/transactions
      https://developer.paypal.com/docs/api/transaction-search/v1/#transactions_get
      params: transaction_id, start_date, end_date (window <= 31 days), fields=all
  * Tracking - GET /v1/shipping/trackers?transaction_id=...  (list; transaction_id is its required filter)
      https://developer.paypal.com/docs/api/tracking/v1/#trackers_get-list-trackers
      (read-one form: GET /v1/shipping/trackers/{id}, id = "<transaction_id>-<tracking_number>")

Every field carries its value and its source. A missing scope / 401 / 403 / 404 / empty answer / timeout
becomes a per-source limitation in plain English and never an exception; only programming errors raise.
The client is injected (the pilot SandboxClient interface: `.call(method, path, op)`), so tests use a fake.

Sources: "paypal_dispute" | "paypal_transaction_search" | "paypal_tracking" | "seller_statement" |
"document:<doc_id>".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlparse

import httpx

import copilot.core.disputes  # noqa: F401  (puts pilot/ on sys.path)
from copilot.core import paypal_words
from step3_dispute import money_to_minor, parse_utc  # noqa: E402

SRC_DISPUTE = "paypal_dispute"
SRC_SEARCH = "paypal_transaction_search"
SRC_TRACKING = "paypal_tracking"
SRC_ORDER = "paypal_order"            # the sale's own capture and order, read when the transaction report has no row yet
SRC_SELLER = "seller_statement"

ALLOWED_HOST = "api-m.sandbox.paypal.com"
ALLOWED_PATH_PREFIXES = ("/v1/reporting/transactions", "/v1/shipping/trackers", "/v2/payments/captures/",
                         "/v2/checkout/orders/")
_WINDOW_DAYS = 30           # Transaction Search allows <= 31 days per request
_MAX_WINDOWS = 3            # look back up to ~90 days before the dispute was opened


@dataclass(frozen=True)
class Field:
    value: object
    source: str


@dataclass(frozen=True)
class Limitation:
    source: str      # one of the SRC_* ids
    message: str     # plain English, shown to the seller
    technical: str = ""   # the HTTP status and issue code, for a Technical details disclosure only


@dataclass(frozen=True)
class TransactionContext:
    fields: dict = field(default_factory=dict)          # name -> Field
    limitations: tuple = ()                             # tuple[Limitation, ...]

    def get(self, name: str):
        f = self.fields.get(name)
        return f.value if f else None

    def source_of(self, name: str) -> str | None:
        f = self.fields.get(name)
        return f.source if f else None

    def with_seller_statement(self, name: str, value) -> "TransactionContext":
        """Record a value declared by the seller as `seller_declared_<name>` (source seller_statement).
        It never overwrites a PayPal-sourced field of the same name."""
        new = dict(self.fields)
        new[f"seller_declared_{name}"] = Field(value, SRC_SELLER)
        return replace(self, fields=new)

    def limitation_for(self, source: str) -> str | None:
        for lim in self.limitations:
            if lim.source == source:
                return lim.message
        return None


class DisallowedCall(RuntimeError):
    """Programming error: a call outside the allowlist was attempted."""


def _guard(method: str, path: str) -> None:
    if method.upper() != "GET":
        raise DisallowedCall(f"only GET is allowed here, got {method}")
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != ALLOWED_HOST:
            raise DisallowedCall(f"host not allowed: {parsed.netloc}")
    if not parsed.path.startswith(ALLOWED_PATH_PREFIXES):
        raise DisallowedCall(f"path not allowed: {parsed.path}")


def _get(client, path: str, op: str) -> dict:
    _guard("GET", path)
    return client.call("GET", path, op)


def _classify_failure(exc: Exception, what: str) -> str | None:
    """Plain-English limitation for an expected failure; None when it is a programming error."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        if status in (401, 403):
            return (f"PayPal did not allow this app to read {what} (the app may be missing that permission). "
                    f"You can enter the information yourself.")
        if status == 404:
            return f"PayPal has no {what} for this transaction."
        if status == 400:
            return f"PayPal could not look up {what} with the information available."
        return f"PayPal could not return {what} right now. Try again in a few minutes."
    if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return f"PayPal did not answer in time when looking up {what}."
    return None


def _parse_paypal_time(ts: str | None) -> datetime | None:
    """PayPal uses '...Z' or '...+0000'; return an aware datetime or None."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if len(s) > 5 and s[-5] in "+-" and s[-4:].isdigit() and ":" not in s[-5:]:
        s = s[:-2] + ":" + s[-2:]
    return parse_utc(s.replace("Z", "+00:00") if s.endswith("Z") else s)


def _money(m) -> tuple[int | None, str | None]:
    try:
        return money_to_minor(m)
    except ValueError:
        return None, None


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _put(fields: dict, name: str, value, source: str) -> None:
    if value not in (None, "", [], {}):
        fields[name] = Field(value, source)


def _from_dispute(norm: dict, fields: dict) -> None:
    for name, key in (("dispute_id", "dispute_id"), ("reason", "reason"), ("create_time", "create_time"),
                      ("dispute_amount_cents", "amount_cents"), ("dispute_currency", "currency"),
                      ("evidence_requested", "evidence_requested_from_seller"),
                      ("evidence_submitted", "evidence_submitted_by_seller")):
        _put(fields, name, norm.get(key), SRC_DISPUTE)
    ids = norm.get("seller_transaction_ids") or []
    if ids:
        fields["seller_transaction_id"] = Field(ids[0], SRC_DISPUTE)


def _search(client, txn_id: str, created: datetime, fields: dict, limits: list) -> None:
    what = "the transaction details"
    end = created + timedelta(days=1)
    detail = None
    for _ in range(_MAX_WINDOWS):
        start = end - timedelta(days=_WINDOW_DAYS)
        q = urlencode({"transaction_id": txn_id, "start_date": _fmt(start), "end_date": _fmt(end),
                       "fields": "all"})
        try:
            body = _get(client, f"/v1/reporting/transactions?{q}", "context_transaction_search")
        except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised below
            msg = _classify_failure(exc, what)
            if msg is None:
                raise
            limits.append(Limitation(SRC_SEARCH, msg, paypal_words.technical_of(exc)))
            return
        rows = (body or {}).get("transaction_details") or [] if isinstance(body, dict) else []
        match = [r for r in rows if (r.get("transaction_info") or {}).get("transaction_id") == txn_id]
        if match:
            detail = match[0]
            break
        end = start
    if detail is None:
        limits.append(Limitation(SRC_SEARCH, "PayPal returned no transaction details for this transaction "
                                             "(searched the 90 days before the dispute)."))
        return
    info = detail.get("transaction_info") or {}
    _put(fields, "payment_date", _fmt(d) if (d := _parse_paypal_time(info.get("transaction_initiation_date")))
         else None, SRC_SEARCH)
    cents, cur = _money(info.get("transaction_amount"))
    if cents is not None:
        fields["total_amount_cents"] = Field(cents, SRC_SEARCH)   # gross: already includes shipping and tax
        fields["total_currency"] = Field(cur, SRC_SEARCH)
    ship = (detail.get("shipping_info") or {})
    addr = ship.get("address") or {}
    shipping_address = {k: v for k, v in (
        ("line1", addr.get("line1")), ("line2", addr.get("line2")), ("city", addr.get("city")),
        ("state", addr.get("state")), ("postal_code", addr.get("postal_code")),
        ("country_code", addr.get("country_code"))) if v}
    if shipping_address:
        fields["shipping_address"] = Field(shipping_address, SRC_SEARCH)
        _put(fields, "recipient_name", ship.get("name"), SRC_SEARCH)
    else:
        limits.append(Limitation(SRC_SEARCH, "PayPal returned no shipping address for this transaction."))


def _tracking(client, txn_id: str, fields: dict, limits: list) -> None:
    what = "shipment tracking"
    try:
        body = _get(client, f"/v1/shipping/trackers?{urlencode({'transaction_id': txn_id})}",
                    "context_tracking")
    except Exception as exc:  # noqa: BLE001
        msg = _classify_failure(exc, what)
        if msg is None:
            raise
        limits.append(Limitation(SRC_TRACKING, msg, paypal_words.technical_of(exc)))
        return
    items = []
    if isinstance(body, dict):
        items = body.get("trackers") or body.get("items") or ([body] if body.get("tracking_number") else [])
    items = [t for t in items if isinstance(t, dict) and t.get("tracking_number")]
    if not items:
        limits.append(Limitation(SRC_TRACKING, "No tracking number is registered in PayPal for this "
                                               "transaction."))
        return
    t = items[0]
    fields["tracking_number"] = Field(t["tracking_number"], SRC_TRACKING)
    _put(fields, "carrier", t.get("carrier_name_other") if t.get("carrier") == "OTHER" and
         t.get("carrier_name_other") else t.get("carrier"), SRC_TRACKING)
    _put(fields, "tracking_status", t.get("status"), SRC_TRACKING)
    if len(items) > 1:
        fields["tracking_numbers_all"] = Field([x["tracking_number"] for x in items], SRC_TRACKING)


def _log_optional_read_failed(what: str, exc: Exception) -> None:
    """One line when the optional read of the sale fails, so a silent miss can be seen afterwards. Class and status only:
    never the message, which can carry an id or a body."""
    logging.getLogger("copilot.context").warning("optional read of the sale's %s failed: %s status=%s", what,
                                                 type(exc).__name__, getattr(exc, "status", None))


_REPORT_GAPS = ("PayPal returned no transaction details", "PayPal returned no shipping address")


def _order(client, txn_id: str, fields: dict, limits: list) -> None:
    """PayPal's transaction report lags new sales (hours): until it holds the sale, the amount, the payment date and the
    shipping address are read from the sale itself, the capture and its order (two read-only GETs), and labelled with
    their own source. Only fills what the report did not give. This read is an extra: whatever goes wrong in it (PayPal
    refuses, times out, answers something else) adds nothing and changes nothing, and the page shows what it showed before.
    The allowlist is checked outside the try, so a path that is not allowed is still a programming error."""
    capture_path = f"/v2/payments/captures/{quote(str(txn_id), safe='')}"
    _guard("GET", capture_path)
    try:
        capture = client.call("GET", capture_path, "context_capture")
    except Exception as exc:  # noqa: BLE001 - optional read, see the docstring
        _log_optional_read_failed("capture", exc)
        return
    if not isinstance(capture, dict) or capture.get("id") != txn_id:
        return
    cents, cur = _money(capture.get("amount"))
    if cents is not None and cur and "total_amount_cents" not in fields:
        fields["total_amount_cents"] = Field(cents, SRC_ORDER)
        fields["total_currency"] = Field(cur, SRC_ORDER)
    if "payment_date" not in fields:
        _put(fields, "payment_date", _fmt(d) if (d := _parse_paypal_time(capture.get("create_time"))) else None, SRC_ORDER)
    related = (capture.get("supplementary_data") or {}).get("related_ids") or {}
    order_id = related.get("order_id") if isinstance(related, dict) else None
    if "shipping_address" in fields or not isinstance(order_id, str) or not order_id:
        return
    order_path = f"/v2/checkout/orders/{quote(order_id, safe='')}"
    _guard("GET", order_path)
    try:
        order = client.call("GET", order_path, "context_order")
    except Exception as exc:  # noqa: BLE001 - optional read
        _log_optional_read_failed("order", exc)
        return
    units = [u for u in (order.get("purchase_units") or []) if isinstance(u, dict)] if isinstance(order, dict) else []
    mine = [u for u in units if any(isinstance(c, dict) and c.get("id") == txn_id
                                    for c in ((u.get("payments") or {}).get("captures") or []))]
    unit = mine[0] if mine else (units[0] if len(units) == 1 else None)
    ship = (unit or {}).get("shipping") or {}
    addr = ship.get("address") or {}
    shipping_address = {k: v for k, v in (
        ("line1", addr.get("address_line_1")), ("line2", addr.get("address_line_2")), ("city", addr.get("admin_area_2")),
        ("state", addr.get("admin_area_1")), ("postal_code", addr.get("postal_code")),
        ("country_code", addr.get("country_code"))) if v}
    if not shipping_address:
        return
    fields["shipping_address"] = Field(shipping_address, SRC_ORDER)
    _put(fields, "recipient_name", (ship.get("name") or {}).get("full_name"), SRC_ORDER)
    # the report's gap is filled from the sale itself: its two "not returned" notes no longer describe what the page shows
    limits[:] = [lim for lim in limits if not (lim.source == SRC_SEARCH and lim.message.startswith(_REPORT_GAPS))]


def fetch_context(client, dispute_norm: dict) -> TransactionContext:
    """Never raises for missing scope / 4xx / empty / timeout; those become limitations."""
    fields: dict = {}
    limits: list = []
    _from_dispute(dispute_norm, fields)
    txn_id = fields["seller_transaction_id"].value if "seller_transaction_id" in fields else None
    if not txn_id:
        msg = "The dispute does not name the seller transaction, so PayPal could not be asked for more."
        limits += [Limitation(SRC_SEARCH, msg), Limitation(SRC_TRACKING, msg)]
        return TransactionContext(fields, tuple(limits))
    created = _parse_paypal_time(dispute_norm.get("create_time"))
    if created is None:
        limits.append(Limitation(SRC_SEARCH, "The dispute has no creation time, so the transaction could not "
                                             "be searched."))
    else:
        _search(client, txn_id, created, fields, limits)
    if "shipping_address" not in fields or "total_amount_cents" not in fields:
        _order(client, txn_id, fields, limits)
    _tracking(client, txn_id, fields, limits)
    if "total_amount_cents" not in fields and "dispute_amount_cents" in fields:
        # Fallback, labelled with its real source; the checklist treats it as the disputed amount.
        fields["total_amount_cents"] = Field(fields["dispute_amount_cents"].value, SRC_DISPUTE)
        fields["total_currency"] = Field(fields["dispute_currency"].value, SRC_DISPUTE) \
            if "dispute_currency" in fields else Field(None, SRC_DISPUTE)
    return TransactionContext(fields, tuple(limits))


__all__ = ["Field", "Limitation", "TransactionContext", "fetch_context", "DisallowedCall",
           "ALLOWED_HOST", "ALLOWED_PATH_PREFIXES", "SRC_DISPUTE", "SRC_SEARCH", "SRC_TRACKING", "SRC_SELLER", "SRC_ORDER"]
