"""Reads copilot/core/checklist.py's field vocabulary (tracking_number, carrier, delivery_status,
delivery_date, ship_date, postal_code, city, state, street, receiver_name, signature, delivery_type,
capture_date) out of a document's extracted plain text (copilot/core/document_ingest.py). Labelled-line
documents only (ES/EN carrier captures and delivery letters, as produced by copilot/fixtures/m1/*.txt) -
a field whose label is not found is simply absent, never guessed. No LLM, no network.

The checklist (per its own docstring) reads `fields[name] = {"value": ..., "confidence": "read" |
"illegible" | "absent"}` and treats "absent" as "not listed at all" - this module never emits an
"absent" entry, it just omits the key.
"""
from __future__ import annotations

import re

# A label IS present but its value is a placeholder/garbled
# token, never a real one - "Guia: NULL", "Fecha de entrega: --", an empty tail after the colon. This is
# illegible, not absent: the document says something is there and we cannot read what. Kept distinct from
# simply not finding the label at all (that stays "absent" per this module's docstring).
_NULL = re.compile(r"^\s*(null|n/?a|-{1,3}|\?{1,3}|x{2,3})?\s*$", re.IGNORECASE)

# Display names likely to appear verbatim in a seller's document. Deliberately NOT imported from
# copilot/core/carriers.py - that module's alias table is a private implementation detail
# of map_carrier(); this is only a search hint, and map_carrier() does the real classification afterwards,
# so a name missing here just falls through to "carrier absent", never a wrong classification.
_CARRIER_NAMES = ("correos de mexico", "correos de méxico", "sepomex", "estafeta", "paquetexpress",
                  "paquete express", "redpack", "red pack", "domino", "houndexpress", "hound express",
                  "senda", "dhl", "fedex", "federal express", "ups", "united parcel service",
                  "skydropx", "envia.com", "enviacom", "mienvio", "usps")
_DATE = r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}"


def _line_value(raw_text: str, *labels: str) -> tuple[str | None, bool]:
    """(captured value or None, label_found). First non-empty capture after any of these ES/EN labels,
    cut at 2+ spaces or end of line (several fixtures put two labelled values on one line, e.g. "Ship
    date: X          Delivery date: Y"). label_found is True even when nothing usable follows the label -
    that is the "present but illegible" case, distinct from the label never appearing at all. The
    space between the colon and the capture is deliberately `[ \\t]*`, not `\\s*` - `\\s*` would also
    match a newline, letting a label with nothing after it on its own line "bleed" into capturing the
    NEXT line's unrelated text (a truly blank tail must stay empty, never
    borrow a sibling line's content)."""
    pattern = r"(?:" + "|".join(labels) + r")\s*:?[ \t]*(.*)"
    m = re.search(pattern, raw_text, re.IGNORECASE)
    if not m:
        return None, False
    value = re.split(r"\s{2,}", m.group(1).strip(), maxsplit=1)[0].strip()
    return (value or None), True


def _date_value(raw_text: str, *labels: str) -> tuple[str | None, bool]:
    """(parsed date string or None, label_found). A label found with no valid date after it is illegible,
    not absent - see _set()."""
    raw, found = _line_value(raw_text, *labels)
    if not found:
        return None, False
    m = raw and re.match(_DATE, raw)
    return (m.group(0) if m else None), True


def _set(fields: dict, name: str, value: str | None, label_found: bool = False, confidence: str = "read") -> None:
    """label_found defaults to False: call sites that never look up a specific label (carrier/address/
    delivery_type detection, which search the whole text rather than one line) leave a None value simply
    absent, matching this module's pre-existing behaviour for those fields. _line_value()/_date_value()-
    based call sites pass their own label_found through explicitly."""
    if value is not None and not _NULL.match(value):
        fields[name] = {"value": value, "confidence": confidence}
    elif label_found:
        fields[name] = {"value": None, "confidence": "illegible"}


_AMOUNT = re.compile(r"(?P<cur>[A-Z]{3})?\s*\$?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?P<cur2>[A-Z]{3})?", re.IGNORECASE)


def _money_cents(raw: str) -> tuple[int | None, str | None]:
    """"4,500.00 MXN" / "MXN 4500" / "$4,500.50" -> (cents as int, currency or None). Pure string work, no floats,
    no currency conversion. (None, None) when no amount can be read."""
    m = _AMOUNT.search(raw or "")
    if not m:
        return None, None
    whole, _, frac = m.group("num").replace(",", "").partition(".")
    cur = (m.group("cur") or m.group("cur2") or "").upper() or None
    return int(whole) * 100 + int((frac + "00")[:2]), cur


def _extract_carrier(raw_text: str) -> str | None:
    for alias in sorted(_CARRIER_NAMES, key=len, reverse=True):
        m = re.search(re.escape(alias).replace(r"\ ", r"\s+"), raw_text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def _classify_signature(raw: str) -> str:
    if re.search(r"online", raw, re.IGNORECASE):
        return "online"
    if re.search(r"aggregator|portal", raw, re.IGNORECASE):
        return "aggregator_portal"
    if re.search(r"paper", raw, re.IGNORECASE):
        return "paper_only"
    if re.search(r"\[?firma\]?\s*\(?\s*(link,?\s*)?empty\)?$|^\s*$|none|no\b", raw, re.IGNORECASE):
        return "none"
    return raw


def _extract_address(raw_text: str, fields: dict) -> None:
    m = re.search(r"(?:delivered to|entregado a)\s*:\s*(.+)", raw_text, re.IGNORECASE)
    delivery_type = "home"
    if not m:
        m = re.search(r"sucursal destino\s*:\s*(.+)", raw_text, re.IGNORECASE)
        delivery_type = "branch_pickup"
    if not m:
        return
    parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
    postal = None
    if parts:
        digits = re.sub(r"\D", "", parts[-1])
        if digits and len(digits) >= 4:
            postal = digits
            parts = parts[:-1]
    if postal:
        _set(fields, "postal_code", postal)
    if len(parts) >= 3:
        _set(fields, "street", parts[0])
        _set(fields, "city", parts[-2])
        _set(fields, "state", parts[-1])
    elif len(parts) == 2:
        _set(fields, "city", parts[0])
        _set(fields, "state", parts[1])
    elif len(parts) == 1:
        _set(fields, "city", parts[0])
    _set(fields, "delivery_type", delivery_type)


def extract_fields(raw_text: str | None) -> dict:
    """Returns the subset of checklist.py's field vocabulary this text's labels support. {} for None/empty
    text or when no recognised label is found (that is "absent", not an error)."""
    if not raw_text or not raw_text.strip():
        return {}
    fields: dict = {}
    value, found = _line_value(raw_text, r"guia", r"gu[ií]a", r"tracking no\.?", r"tracking number",
                               r"n[uú]mero de gu[ií]a")
    _set(fields, "tracking_number", value, found)
    value, found = _line_value(raw_text, r"estatus", r"status")
    _set(fields, "delivery_status", value, found)
    value, found = _date_value(raw_text, r"fecha de env[ií]o", r"ship date")
    _set(fields, "ship_date", value, found)
    value, found = _date_value(raw_text, r"fecha de entrega", r"delivery date")
    _set(fields, "delivery_date", value, found)
    value, found = _date_value(raw_text, r"fecha de captura", r"(?<!id de )captura", r"captured")
    _set(fields, "capture_date", value, found)
    value, found = _line_value(raw_text, r"nombre de quien recibe", r"received by", r"recibi[oó]")
    _set(fields, "receiver_name", value, found)
    sig_raw, sig_found = _line_value(raw_text, r"firma de recibido", r"signature")
    if sig_raw is not None:
        _set(fields, "signature", _classify_signature(sig_raw))
    elif sig_found:
        _set(fields, "signature", None, label_found=True)   # labelled but blank: _classify_signature
                                                             # would call "none"; illegible is more honest
    value, found = _line_value(raw_text, r"invoice total", r"total de la factura", r"total factura",
                               r"importe total de la factura")
    cents, cur = _money_cents(value) if value else (None, None)
    if cents is not None:
        fields["invoice_total_cents"] = {"value": cents, "confidence": "read"}
        if cur:
            fields["invoice_currency"] = {"value": cur, "confidence": "read"}
    elif found:
        fields["invoice_total_cents"] = {"value": None, "confidence": "illegible"}
    carrier = _extract_carrier(raw_text)
    if carrier:
        _set(fields, "carrier", carrier)
    if re.search(r"ocurre|branch\s*pickup|recoger en sucursal", raw_text, re.IGNORECASE):
        fields.setdefault("delivery_type", {"value": "branch_pickup", "confidence": "read"})
    postal_raw, postal_found = _line_value(raw_text, r"c[oó]digo postal(?: destino)?", r"postal code", r"cp")
    postal_match = postal_raw and re.search(r"\d{4,6}", postal_raw)
    _set(fields, "postal_code", postal_match.group(0) if postal_match else None, postal_found)
    _extract_address(raw_text, fields)
    return fields


__all__ = ["extract_fields"]
