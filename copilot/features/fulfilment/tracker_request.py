"""The Add Tracking request body and its hash. Pure functions, no I/O.

Body shape (accepted by the sandbox with 201):
    {"trackers": [{"transaction_id", "tracking_number", "status", "carrier", "carrier_name_other"}]}
`carrier_name_other` is sent whenever a name is supplied (a sandbox run sent it for a catalogue carrier and PayPal
simply did not echo it back) and is mandatory when the carrier is OTHER.

request_sha256 canon: the send path's own `sha256_json` (copilot.core.send, from pilot/step5_send_evidence.py):
sha256 over json.dumps(obj, sort_keys=True, ensure_ascii=False) encoded as UTF-8, default separators (", " and ": ").
Reused by import so the two paths can never drift apart; the tests pin a fixture value.

The form posts a catalogue code (or OTHER plus a typed name) and a tracking number from the receipt.
Whitespace inside the tracking number is removed BEFORE it is checked, hashed and sent, so the preview, the check
page and the register step always carry the same cleaned value. Every problem the form can have is collected in one
pass (FieldError.errors, keyed by field) and routed to its own field."""
from __future__ import annotations

import re

from copilot.core import carriers
from copilot.core.carriers import KNOWN_CODES, map_carrier
from copilot.core.send import sha256_json

TRACKER_PATH = "/v1/shipping/trackers"
STATUSES = ("SHIPPED", "DELIVERED", "ON_HOLD", "CANCELLED", "LOCAL_PICKUP")
STATUS_LABELS = {"SHIPPED": "Shipped", "DELIVERED": "Delivered", "ON_HOLD": "On hold", "CANCELLED": "Canceled",
                 "LOCAL_PICKUP": "Local pickup", "PARTIALLY_DELIVERED": "Partially delivered",
                 "IN_TRANSIT": "In transit"}
CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9]{17}$")
MAX_TRACKING_LEN = carriers.TRACKING_MAX_LENGTH
MAX_CARRIER_NAME_LEN = carriers.CARRIER_NAME_MAX_LENGTH

MSG_ENTER_TRACKING = "Enter the tracking number from the carrier's receipt."
MSG_CHOOSE_STATUS = "Choose the shipment status"
MSG_ADDRESS_FIELD_TOO_LONG = "{label} must be {limit} characters or fewer."
MSG_COUNTRY = "Choose the country, or type its 2-letter code (for example MX or US)."
MSG_COUNTRY_OTHER = "Enter the country's 2-letter code (for example CA)."

# Declared-address limits live on the server (no maxlength in the markup).
ADDRESS_LIMITS = {"line1": ("Street", 120), "city": ("City", 64), "state": ("State", 64),
                  "postal_code": ("Postal code", 20)}
_COUNTRY_NAMES = {
    "mexico": "MX", "mx": "MX", "mex": "MX", "united states": "US", "united states of america": "US", "usa": "US",
    "us": "US", "u s": "US", "estados unidos": "US", "eeuu": "US", "ee uu": "US",
}


class FieldError(ValueError):
    """A form problem routed to its own field: `errors` is {field: message}, in the order the form shows them.
    FieldError("tracking_number", message) is the one-field case. A ValueError, so older callers still work."""

    def __init__(self, field: str | None = None, message: str | None = None, errors: dict | None = None):
        self.errors = dict(errors) if errors else {field: message}
        first = next(iter(self.errors))
        self.field, self.message = first, self.errors[first]
        super().__init__(self.message)


def validate_capture_id(capture_id: str) -> str | None:
    """None when `capture_id` is 17 alphanumerics (PayPal's transaction id shape); else a plain reason."""
    if not isinstance(capture_id, str) or not CAPTURE_ID_RE.match(capture_id):
        return "Enter the transaction ID: 17 letters and numbers, from the payment details in PayPal"
    return None


def build_tracker_body(capture_id: str, tracking_number: str, carrier_code: str, carrier_name_other: str | None,
                       status: str = "SHIPPED") -> dict:
    """The exact Add Tracking body for one tracker. Raises ValueError (a refusal the caller shows) for a bad id,
    an empty or odd tracking number, an unknown status or carrier code, or OTHER without a name."""
    problem = validate_capture_id(capture_id)
    if problem:
        raise ValueError(problem)
    number = (tracking_number or "").strip()
    if not number or len(number) > MAX_TRACKING_LEN or not number.isprintable() or " " in number:
        raise ValueError(f"The tracking number must be 1 to {MAX_TRACKING_LEN} characters without spaces.")
    if status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}.")
    if carrier_code != "OTHER" and carrier_code not in KNOWN_CODES:
        raise ValueError("Carrier must be a PayPal Tracking catalogue code or OTHER.")
    name = (carrier_name_other or "").strip() or None
    if name is not None and (len(name) > MAX_CARRIER_NAME_LEN or not name.isprintable()):
        raise ValueError(f"The carrier name must be at most {MAX_CARRIER_NAME_LEN} printable characters.")
    if carrier_code == "OTHER" and name is None:
        raise ValueError("A carrier outside the PayPal catalogue needs its name.")
    tracker = {"transaction_id": capture_id, "tracking_number": number, "status": status, "carrier": carrier_code}
    if name is not None:
        tracker["carrier_name_other"] = name
    return {"trackers": [tracker]}


def form_problems(tracking_number: str, carrier: str, status: str, carrier_other_name: str = "") -> dict:
    """{field: message} for everything wrong in the three required controls, in the order the form shows them
    (carrier, carrier name, tracking number, status); empty when the form is acceptable."""
    errors: dict = {}
    stored, field, message = carriers.carrier_from_form(carrier, carrier_other_name)
    if message:
        errors[field] = message
    elif not stored:
        errors["carrier"] = carriers.MSG_CHOOSE_CARRIER
    problem = carriers.tracking_number_problem(tracking_number)
    if problem:
        errors["tracking_number"] = problem
    elif not carriers.normalize_tracking(tracking_number):
        errors["tracking_number"] = MSG_ENTER_TRACKING
    if status not in STATUSES:
        errors["status"] = MSG_CHOOSE_STATUS
    return errors


def body_from_form(capture_id: str, tracking_number: str, carrier_text: str, status: str = "SHIPPED",
                   carrier_other_name: str = "") -> dict:
    """The form's choices -> the exact body. A catalogue code goes with its display name as carrier_name_other (the
    sandbox-verified shape); OTHER goes with the typed name; legacy free text is still mapped through
    copilot.core.carriers.map_carrier (OTHER + the typed name when unmapped). The tracking number is whitespace-
    stripped first. Raises FieldError with every field problem, or ValueError for a bad transaction id."""
    problem = validate_capture_id(capture_id)
    if problem:
        raise ValueError(problem)
    errors = form_problems(tracking_number, carrier_text, status, carrier_other_name)
    if errors:
        raise FieldError(errors=errors)
    stored, _field, _message = carriers.carrier_from_form(carrier_text, carrier_other_name)
    number = carriers.normalize_tracking(tracking_number)
    try:
        if stored in KNOWN_CODES:
            return build_tracker_body(capture_id, number, stored, carriers.display_name(stored), status)
        match = map_carrier(stored)
        return build_tracker_body(capture_id, number, match.code,
                                  match.carrier_name_other or str(stored).strip(), status)
    except ValueError as exc:
        raise FieldError("tracking_number", str(exc)) from exc


def country_code(selected: str, other: str = "") -> tuple[str, str | None, str | None]:
    """(code, error field, message) for the declared country: the select's MX or US, a typed name from a short list,
    or Other plus a 2-letter code. Empty is allowed (the whole address is optional)."""
    selected, other = (selected or "").strip(), (other or "").strip()
    if selected.upper() == "OTHER":
        if re.fullmatch(r"[A-Za-z]{2}", other):
            return other.upper(), None, None
        return "", "declared_country_other", MSG_COUNTRY_OTHER
    if not selected:
        if other:
            return "", "declared_country_code", MSG_COUNTRY
        return "", None, None
    name = " ".join(re.sub(r"[^a-z ]", " ", selected.casefold().replace("é", "e").replace("á", "a")).split())
    if name in _COUNTRY_NAMES:
        return _COUNTRY_NAMES[name], None, None
    return "", "declared_country_code", MSG_COUNTRY


def declared_address(values: dict) -> tuple[dict, dict]:
    """(declared address with only the filled keys, {field: message}) from the declared_* form values."""
    errors: dict = {}
    declared: dict = {}
    for key, (label, limit) in ADDRESS_LIMITS.items():
        value = " ".join(str(values.get(f"declared_{key}") or "").split())
        if len(value) > limit or (value and not value.isprintable()):
            errors[f"declared_{key}"] = MSG_ADDRESS_FIELD_TOO_LONG.format(label=label, limit=limit)
        elif value:
            declared[key] = value
    code, field, message = country_code(values.get("declared_country_code"), values.get("declared_country_other"))
    if message:
        errors[field] = message
    elif code:
        declared["country_code"] = code
    return declared, errors


def request_sha256(body: dict) -> str:
    """sha256 of the body in the send path's canon (see the module docstring)."""
    return sha256_json(body)
