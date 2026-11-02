"""Pure dispute-evidence mapping; the Tracking API retains its own catalogue code."""
import json

from .extract import FIXTURES

_DOC = json.loads((FIXTURES / "disputes.json").read_text(encoding="utf-8"))
_CARRIER = _DOC["components"]["schemas"]["tracking_info"]["properties"]["carrier_name"]


def _enums(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "enum":
                yield from value
            else:
                yield from _enums(value)
    elif isinstance(node, list):
        for value in node:
            yield from _enums(value)


ALLOWED_CODES = frozenset(_enums(_CARRIER))


def evidence_carrier(code, name):
    if code in ALLOWED_CODES:
        return code, name if code == "OTHER" else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("A carrier outside the dispute contract needs its display name.")
    return "OTHER", name
