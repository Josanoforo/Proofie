"""Map a carrier name (as printed on a seller's document) to a PayPal Tracking API carrier code.

Catalogue source: PayPal Shipment Tracking API v1 documentation (carrier codes list)
(https://developer.paypal.com/docs/api/tracking/v1/). The dispute contract's `carrier_name` enum is narrower;
see evidence_carrier. Mexico codes: CORREOS_DE_MEXICO, MEX_ESTAFETA,
PAQUETEXPRESS, MEX_REDPACK, DOMINO, GRUPO, HOUNDEXPRESS, MEX_SENDA. DHL / FEDEX / UPS use their global codes.

Anything not in the catalogue (including 99minutos) maps to OTHER + carrier_name_other and is flagged as a
risk - never as invalid. Shipping aggregators (Skydropx, Envia.com, Mienvio) are not carriers: OTHER, with a
note telling the seller to use the underlying carrier's proof.
Pure functions, no network.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MX_CATALOG = ("CORREOS_DE_MEXICO", "MEX_ESTAFETA", "PAQUETEXPRESS", "MEX_REDPACK", "DOMINO", "GRUPO",
              "HOUNDEXPRESS", "MEX_SENDA")
GLOBAL_CATALOG = ("DHL", "FEDEX", "UPS")
KNOWN_CODES = frozenset(MX_CATALOG + GLOBAL_CATALOG)

# Known catalogue codes accepted by the dispute tracking_info.carrier_name enum.
# Schema v1.12, full-document SHA256 fee5e04ab606f8961b95744e65c4189fc7c4613e79ba2f944d2deab79e834330;
# derived 2026-10-08. DOMINO belongs to Tracking, not the dispute enum.
DISPUTE_EVIDENCE_CODES = frozenset({
    "CORREOS_DE_MEXICO", "DHL", "FEDEX", "GRUPO", "HOUNDEXPRESS",
    "MEX_ESTAFETA", "MEX_REDPACK", "MEX_SENDA", "PAQUETEXPRESS", "UPS",
})
EVIDENCE_DISPLAY_NAMES = {"DOMINO": "Domino"}


def evidence_carrier(code: str | None, name: str | None) -> tuple[str, str | None]:
    """Dispute evidence code/name; keep the Tracking catalogue mapping separate.

    Contract-listed codes pass through; OTHER preserves a nonblank supplied name.
    A catalogue code outside the dispute contract uses its display name with OTHER.
    No code retains the existing OTHER-without-name behaviour.
    """
    if code in DISPUTE_EVIDENCE_CODES:
        return code, None
    if code == "OTHER":
        return "OTHER", (name if isinstance(name, str) and name.strip() else None)
    if not code:
        return "OTHER", None
    display = EVIDENCE_DISPLAY_NAMES.get(code) or (name.strip() if isinstance(name, str) and name.strip() else None)
    if display is None:
        raise ValueError("A carrier outside the dispute contract needs its display name.")
    return "OTHER", display


_ALIASES = {
    "correos de mexico": "CORREOS_DE_MEXICO", "correos mexico": "CORREOS_DE_MEXICO",
    "sepomex": "CORREOS_DE_MEXICO", "servicio postal mexicano": "CORREOS_DE_MEXICO",
    "estafeta": "MEX_ESTAFETA", "estafeta mexicana": "MEX_ESTAFETA",
    "paquetexpress": "PAQUETEXPRESS", "paquete express": "PAQUETEXPRESS",
    "redpack": "MEX_REDPACK", "red pack": "MEX_REDPACK",
    "domino": "DOMINO",
    "grupo": "GRUPO",
    "hound express": "HOUNDEXPRESS", "houndexpress": "HOUNDEXPRESS",
    "senda": "MEX_SENDA", "autobuses senda": "MEX_SENDA",
    "dhl": "DHL", "dhl express": "DHL",
    "fedex": "FEDEX", "federal express": "FEDEX", "fedex express": "FEDEX",
    "ups": "UPS", "united parcel service": "UPS",
}
_FIRST_TOKEN = {"dhl": "DHL", "fedex": "FEDEX", "ups": "UPS", "estafeta": "MEX_ESTAFETA",
                "redpack": "MEX_REDPACK", "paquetexpress": "PAQUETEXPRESS"}
_AGGREGATORS = ("skydropx", "envia", "enviacom", "mienvio")

AGGREGATOR_NOTE = "aggregator: use the underlying carrier's proof"


@dataclass(frozen=True)
class CarrierMatch:
    code: str | None            # Tracking API code, "OTHER", or None when no name was given
    carrier_name_other: str | None   # required by the Tracking API when code == "OTHER"
    known: bool                 # True only for catalogue carriers
    aggregator: bool
    risk: bool                  # True for OTHER (unknown carrier or aggregator)
    note: str | None


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def carriers_equal(left: str | None, right: str | None) -> bool:
    """Compare a catalogue code with its explicit label without changing either record.

    Unknown names retain their identity; a common first word is not evidence
    that two carrier values are the same carrier.
    """
    def key(value):
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        code = raw.upper() if raw.upper() in KNOWN_CODES else _ALIASES.get(_norm(raw))
        return ("catalogue", code) if code else ("literal", _norm(raw))

    first, second = key(left), key(right)
    return first is not None and second is not None and first == second


def map_carrier(name: str | None) -> CarrierMatch:
    if name is None or not str(name).strip():
        return CarrierMatch(None, None, False, False, False, "no carrier name given")
    raw = str(name).strip()
    n = _norm(raw)
    compact = n.replace(" ", "")
    if raw.upper() in KNOWN_CODES:
        return CarrierMatch(raw.upper(), None, True, False, False, None)
    if any(compact.startswith(a) for a in _AGGREGATORS):
        return CarrierMatch("OTHER", raw, False, True, True, AGGREGATOR_NOTE)
    code = _ALIASES.get(n)
    if code is None and n:
        code = _FIRST_TOKEN.get(n.split(" ")[0])
    if code is not None:
        return CarrierMatch(code, None, True, False, False, None)
    return CarrierMatch("OTHER", raw, False, False, True,
                        "carrier not in the PayPal Tracking catalogue; registered as OTHER")


# ----------------------------------------------------------------------------- The seller's form
# What the seller sees and types. Presentation and input checks only: nothing here decides what is sent (that is
# map_carrier / evidence_carrier above plus the send guards).
DISPLAY_NAMES = {
    "FEDEX": "FedEx", "DHL": "DHL", "UPS": "UPS", "MEX_ESTAFETA": "Estafeta",
    "CORREOS_DE_MEXICO": "Correos de México", "PAQUETEXPRESS": "Paquetexpress", "MEX_REDPACK": "Redpack",
    "MEX_SENDA": "Senda", "HOUNDEXPRESS": "Hound Express", "GRUPO": "Grupo", "DOMINO": "Domino",
}
# (code, label) in the order of the shipping-company list; the last entry is the free-text escape.
SELECT_OPTIONS = tuple(DISPLAY_NAMES.items()) + (("OTHER", "Other carrier"),)

TRACKING_MAX_LENGTH = 64
CARRIER_NAME_MAX_LENGTH = 64

MSG_CARRIER_NAME_AS_TRACKING = ("“{value}” is a carrier name, not a tracking number. Choose it in “Carrier (shipping "
                                "company)”, then enter the code from the carrier's receipt in “Tracking number (from "
                                "the carrier's receipt)”.")
MSG_TRACKING_NEEDS_DIGIT = "Enter the tracking number from the carrier's receipt. It includes numbers."
MSG_TRACKING_IS_URL = "Enter only the tracking number, not a web address."
MSG_TRACKING_IS_EMAIL = "Enter the tracking number, not an email address."
MSG_TRACKING_TOO_LONG = "Tracking number must be 64 characters or fewer. Check the receipt."
MSG_CHOOSE_CARRIER = "Choose the shipping company"
MSG_ENTER_CARRIER_NAME = "Enter the carrier name"
MSG_CARRIER_NAME_LOOKS_LIKE_TRACKING = ("This looks like a tracking number. Enter it in “Tracking number (from the "
                                        "carrier's receipt)”, and type the carrier's name here.")
MSG_CARRIER_NAME_TOO_LONG = "Carrier name must be 64 characters or fewer."


def display_name(code_or_text) -> str:
    """The seller's word for a carrier: a catalogue code gives its name; anything else (a typed name, a legacy
    free-text value) is shown exactly as stored."""
    if not isinstance(code_or_text, str) or not code_or_text.strip():
        return ""
    raw = code_or_text.strip()
    return DISPLAY_NAMES.get(raw.upper() if raw.upper() in DISPLAY_NAMES else raw, raw)


def normalize_tracking(value) -> str:
    """Tracking numbers are copied from receipts with spaces in them: all whitespace is removed, nothing else."""
    return re.sub(r"\s+", "", value) if isinstance(value, str) else ""


def _compact(text: str) -> str:
    return _norm(text).replace(" ", "")


_CARRIER_WORDS = frozenset(_compact(w) for w in (
    *KNOWN_CODES, *_ALIASES, *_AGGREGATORS, "99minutos"))


def tracking_number_problem(value):
    """None, or the exact message for a value that cannot be a tracking number. Blocks only what is certainly
    wrong; a plausible code is accepted (the carrier is the authority). No prefix or first-token match, no phone rule."""
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        return None
    normalized = normalize_tracking(raw)
    if _compact(normalized) in _CARRIER_WORDS:
        return MSG_CARRIER_NAME_AS_TRACKING.format(value=raw)
    lowered = normalized.lower()
    if "://" in lowered or lowered.startswith("www."):
        return MSG_TRACKING_IS_URL
    if "@" in normalized:
        return MSG_TRACKING_IS_EMAIL
    if not any(c.isdigit() for c in normalized):
        return MSG_TRACKING_NEEDS_DIGIT
    if len(normalized) > TRACKING_MAX_LENGTH:
        return MSG_TRACKING_TOO_LONG
    return None


def _carrier_name_problem(name: str):
    if re.search(r"[0-9]{8,}", name):
        return MSG_CARRIER_NAME_LOOKS_LIKE_TRACKING
    if not any(c.isalpha() for c in name):
        return MSG_ENTER_CARRIER_NAME
    if len(name) > CARRIER_NAME_MAX_LENGTH or not name.isprintable():
        return MSG_CARRIER_NAME_TOO_LONG
    return None


def carrier_from_form(code, other_name, legacy_text=None):
    """(stored value, error field, message) for the carrier controls: a catalogue code, or the typed 'Other carrier'
    name - never the word OTHER. A value that is not a code (the old free-text field, or the 'Your entry' option) is
    accepted as legacy text under the same name rules. An empty choice stores nothing and is not an error."""
    code = code.strip() if isinstance(code, str) else ""
    other = " ".join(other_name.split()) if isinstance(other_name, str) else ""
    legacy = " ".join(legacy_text.split()) if isinstance(legacy_text, str) else ""
    if code == "OTHER":
        problem = MSG_ENTER_CARRIER_NAME if not other else _carrier_name_problem(other)
        return ("", "carrier_other_name", problem) if problem else (other, None, None)
    if code in KNOWN_CODES:
        return code, None, None
    if code:                                    # legacy free text posted as the carrier itself
        text = " ".join(code.split())
        problem = _carrier_name_problem(text)
        return ("", "carrier", problem) if problem else (text, None, None)
    if other:                                   # a name typed without choosing "Other carrier"
        return "", "carrier", MSG_CHOOSE_CARRIER
    if legacy:
        problem = _carrier_name_problem(legacy)
        return ("", "carrier", problem) if problem else (legacy, None, None)
    return "", None, None
