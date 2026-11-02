"""Sandbox orders for manual "item not received" disputes (sandbox only, via pilot/client.SandboxClient).
Promoted to the official judge-facing helper (docs/JUDGE-RUN.md) - judges need a
dispute whose underlying transaction actually carries a shipping address, so copilot/core/context.py's
lookups (Transaction Search, Trackers) have something real to find, not an empty/buyer-chosen one.

Usage (run with the .venv python):
  sandbox_orders.py create <amount_cents> <label>   POST /v2/checkout/orders with a DEMO shipping address
                                                     set directly on the order (SET_PROVIDED_ADDRESS, never
                                                     left for the buyer to pick) - prints order id + approve URL
  sandbox_orders.py batch <n> <amount_cents> <label>  n orders in a row (labels <label>-1 .. <label>-n), one approve URL each:
                                                     the buyer approves them all in ONE sitting, and later opens one case per sale
  sandbox_orders.py capture <order_id> [<order_id>...]  POST capture (PayPal-Request-Id), then GET and print status/capture id/amount,
                                                     and for each captured sale the single buyer click that opens its case
  sandbox_orders.py show <order_id>                 GET order
Evidence: evidence/sandbox_orders.jsonl. Amounts are integer cents converted to a string by code.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pilot"))

from client import SandboxClient  # noqa: E402

CURRENCY = "USD"
LOG_NAME = "sandbox_orders"

# DEMO DATA - fictitious, matches the address already used by copilot/fixtures/m1's own demo documents
# (read-only reference here) so a judge's real sandbox dispute and the bundled demo
# documents agree on the same address, the same way the real send already did.
SHIPPING_NAME = "Ana Demo Ficticia"
SHIPPING_ADDRESS = {"address_line_1": "Calle Falsa 123, Col. Demo", "admin_area_2": "Ciudad de Mexico",
                    "admin_area_1": "CDMX", "postal_code": "06600", "country_code": "MX"}


def cents_to_str(cents: int) -> str:
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
        raise ValueError("amount must be a positive integer number of cents")
    return f"{cents // 100}.{cents % 100:02d}"


def build_order_body(amount_cents: int, label: str) -> dict:
    return {"intent": "CAPTURE",
            "purchase_units": [{"reference_id": label, "description": f"DEMO DATA - {label}",
                                "amount": {"currency_code": CURRENCY, "value": cents_to_str(amount_cents)},
                                "shipping": {"name": {"full_name": SHIPPING_NAME},
                                            "address": SHIPPING_ADDRESS}}],
            "payment_source": {"paypal": {"experience_context": {
                "user_action": "PAY_NOW",
                # SET_PROVIDED_ADDRESS (not GET_FROM_FILE): this address is sent AS the shipping address,
                # never left for the buyer to pick one from their own sandbox profile - a judge's real
                # dispute then has a real address for copilot/core/context.py's lookups to find.
                "shipping_preference": "SET_PROVIDED_ADDRESS",
                "return_url": "https://example.com/paypal/return",
                "cancel_url": "https://example.com/paypal/cancel",
                "brand_name": "Proofie (DEMO)",
                "landing_page": "LOGIN"}}}}


def approve_url(order: dict) -> str:
    return next(l["href"] for l in order["links"] if l["rel"] in ("approve", "payer-action"))


def _captures(order: dict) -> list[dict]:
    return [c for pu in order.get("purchase_units", []) for c in pu.get("payments", {}).get("captures", [])]


def _print_order(order: dict) -> None:
    print("order:", order["id"], "status:", order["status"])
    for c in _captures(order):
        print("capture:", c["id"], c["status"], c["amount"]["value"], c["amount"]["currency_code"])


def buyer_click_lines(amount_value: str, currency: str, capture_id: str) -> list[str]:
    """The buyer's one click path for a captured sale (text only; the buyer types any password on PayPal's site)."""
    return [f"BUYER CLICK for the sale of {amount_value} {currency} (capture {capture_id}):",
            "  sandbox.paypal.com, signed in as the Personal (buyer) account > Resolution Center > Report a problem >",
            f"  the payment of {amount_value} {currency} > Item not received > continue to the final confirmation.",
            "  The case id starts with PP-R-; an unfinished draft is not a case."]


def create(amount_cents: int, label: str) -> None:
    c = SandboxClient(LOG_NAME)
    order = c.call("POST", "/v2/checkout/orders", "create_order", json_body=build_order_body(amount_cents, label),
                   log_extra={"label": label, "amount_cents": amount_cents})
    print("order:", order["id"], "status:", order["status"], "label:", label)
    print("APPROVE AS SANDBOX BUYER:", approve_url(order))


def batch(count: int, amount_cents: int, label: str) -> None:
    """n sales created one after the other; the buyer approves all of them in one sitting."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise ValueError("n must be a whole number from 1 to 10")
    for number in range(1, count + 1):
        create(amount_cents, f"{label}-{number}")
    print(f"{count} sale(s) created. Approve them all as the sandbox buyer in one sitting, then run: "
          f"sandbox_orders.py capture <order_id> ... with the {count} order id(s) printed above.")


def capture(order_id: str) -> None:
    c = SandboxClient(LOG_NAME)
    c.call("POST", f"/v2/checkout/orders/{order_id}/capture", "capture_order", json_body={},
           request_id=str(uuid.uuid4()))
    order = c.call("GET", f"/v2/checkout/orders/{order_id}", "get_order_after_capture")
    _print_order(order)
    for cap in _captures(order):
        if cap.get("status") == "COMPLETED":
            print(*buyer_click_lines(cap["amount"]["value"], cap["amount"]["currency_code"], cap["id"]), sep="\n")


def show(order_id: str) -> None:
    c = SandboxClient(LOG_NAME)
    _print_order(c.call("GET", f"/v2/checkout/orders/{order_id}", "get_order"))


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 4 and argv[1] == "create":
            create(int(argv[2]), argv[3])
        elif len(argv) == 5 and argv[1] == "batch":
            batch(int(argv[2]), int(argv[3]), argv[4])
        elif len(argv) >= 3 and argv[1] == "capture":
            failed = 0
            for order_id in argv[2:]:
                try:
                    capture(order_id)
                except Exception as e:  # one unapproved order must not stop the others
                    failed += 1
                    print("ERROR:", order_id, type(e).__name__, e)
            if failed:
                return 1
        elif len(argv) == 3 and argv[1] == "show":
            show(argv[2])
        else:
            print(__doc__)
            return 2
    except Exception as e:  # PayPalError etc.: print type + message (no secrets in them)
        print("ERROR:", type(e).__name__, e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
