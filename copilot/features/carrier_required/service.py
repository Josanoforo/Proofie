"""Pure missing-carrier characterization; no body or persistence changes."""
from copilot.core.carriers import map_carrier

MESSAGE = "Pick a carrier: PayPal rejects evidence with no carrier"

def carrier_missing(value: str | None) -> bool:
    return map_carrier(value).code is None
