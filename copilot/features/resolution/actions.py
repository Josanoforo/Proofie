"""The two request bodies and the buyer message, all pure. No model, no free text from the seller in this version.

Offer body (as the PayPal sandbox accepts it): {"offer_type": "REFUND",
"offer_amount": {"currency_code", "value"}, "note"}; REFUND is the only type the sandbox lists. Message body:
{"message": text}.
request_sha256 is the send path's `sha256_json` (copilot.core.send): sha256 over json.dumps(obj, sort_keys=True,
ensure_ascii=False) as UTF-8, default separators."""
from __future__ import annotations

import copilot.core.disputes  # noqa: F401  (puts pilot/ on sys.path)
from step3_dispute import minor_to_str  # noqa: E402

from copilot.core import carriers
from copilot.core.send import sha256_json

MAX_TEXT_LEN = 2000
OFFER_TYPE = "REFUND"


def cents_to_str(cents: int, currency: str = "USD") -> str:
    """1000 -> "10.00" by integer arithmetic (zero-decimal currencies keep the integer, as the pilot does)."""
    return minor_to_str(cents, currency)


def _clean_text(text: str, what: str) -> str:
    value = (text or "").strip()
    if not value or len(value) > MAX_TEXT_LEN:
        raise ValueError(f"The {what} must be 1 to {MAX_TEXT_LEN} characters.")
    return value


def offer_body(cents: int, currency: str, note: str, max_cents: int | None = None) -> dict:
    """ValueError on a non-integer amount, cents <= 0, or more than `max_cents` (the dispute amount) when given."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise ValueError("The offer amount must be a whole number of cents.")
    if cents <= 0:
        raise ValueError("The offer amount must be more than zero.")
    if max_cents is not None and cents > max_cents:
        raise ValueError("The offer cannot be more than the disputed amount.")
    if not currency or not currency.isalpha():
        raise ValueError("The offer needs a currency code.")
    return {"offer_type": OFFER_TYPE, "offer_amount": {"currency_code": currency, "value": cents_to_str(cents, currency)},
            "note": _clean_text(note, "note")}


def message_body(text: str) -> dict:
    return {"message": _clean_text(text, "message")}


def request_sha256(body: dict) -> str:
    return sha256_json(body)


def _address_line(address: dict | None) -> str | None:
    parts = [str(address[k]).strip() for k in ("line1", "line2", "city", "state", "postal_code", "country_code")
             if address and address.get(k)]
    return ", ".join(parts) or None


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December")


def date_words(iso_text: str | None) -> str | None:
    """"2026-09-28..." -> "28 September 2026" from a fixed English table (never the machine's locale); None when the text
    does not start with a valid date."""
    text = str(iso_text or "")[:10]
    try:
        year, month, day = int(text[0:4]), int(text[5:7]), int(text[8:10])
        if text[4] != "-" or text[7] != "-" or not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
    except (ValueError, IndexError):
        return None
    return f"{day} {MONTHS[month - 1]} {year}"


def _money_text(cents, currency) -> str | None:
    return f"{cents_to_str(cents, currency)} {currency}" if isinstance(cents, int) and currency else None


def message_text(norm: dict, txn_fields: dict, tracking: dict | None) -> str:
    """The message the seller approves or does not send, from a template, English. Every literal comes from PayPal's
    own data (transaction fields, tracker); a literal that is not known is left out, never replaced by a
    placeholder. `txn_fields`: payment_date (ISO text), total_amount_cents, total_currency, shipping_address.
    `tracking`: tracking_number, carrier, tracking_status, delivery_estimate (all optional)."""
    txn_fields, tracking = txn_fields or {}, tracking or {}
    date = date_words(txn_fields.get("payment_date") or norm.get("create_time"))
    amount = _money_text(txn_fields.get("total_amount_cents"), txn_fields.get("total_currency")) or \
        _money_text(norm.get("amount_cents"), norm.get("currency"))
    first = "Hello, this is about your order"
    if date:
        first += f" of {date}"
    if amount:
        first += f" for {amount}"
    sentences = [first + "."]
    address = _address_line(txn_fields.get("shipping_address"))
    if address:
        sentences.append(f"It was shipped to: {address}.")
    number = tracking.get("tracking_number")
    if number:
        line = f"Tracking number: {number}"
        if tracking.get("carrier"):
            line += f" with {carriers.display_name(tracking['carrier'])}"
        sentences.append(line + ".")
    estimate = tracking.get("delivery_estimate")
    sentences.append(f"If the item has not arrived by {estimate}, reply here and I will look into it right away."
                     if estimate else "If it has not arrived, reply here and I will look into it right away.")
    return " ".join(sentences)
