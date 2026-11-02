"""Pilot step 2: create + capture an order; timeout and retry without duplicate capture.

Usage:
  python step2_order.py create    -> creates order, prints buyer approval link (the buyer approves in the browser)
  python step2_order.py capture   -> forced client timeout, idempotent retry, new-id retry, GET verification
Amounts are integer cents converted to a decimal string by code (no floats).
"""
import json
import sys
import uuid

import httpx

from client import EVIDENCE, PayPalError, SandboxClient

STATE = EVIDENCE / "step2_state.json"
AMOUNT_CENTS = 2500
CURRENCY = "USD"


def cents_to_str(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def create() -> None:
    c = SandboxClient("step2_order")
    body = {"intent": "CAPTURE",
            "purchase_units": [{"reference_id": "pilot-order-1", "description": "DEMO DATA - pilot item",
                                "amount": {"currency_code": CURRENCY, "value": cents_to_str(AMOUNT_CENTS)}}],
            "payment_source": {"paypal": {"experience_context": {
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": "https://example.com/paypal/return",
                "cancel_url": "https://example.com/paypal/cancel",
                "brand_name": "Comprende Pilot (DEMO)",
                "landing_page": "LOGIN"}}}}
    order = c.call("POST", "/v2/checkout/orders", "create_order", json_body=body)
    approve = next(l["href"] for l in order["links"] if l["rel"] in ("approve", "payer-action"))
    STATE.write_text(json.dumps({"order_id": order["id"], "capture_request_id": str(uuid.uuid4())}), encoding="utf-8")
    print("order:", order["id"], "status:", order["status"])
    print("APPROVE AS SANDBOX BUYER:", approve)


def capture() -> None:
    c = SandboxClient("step2_order")
    st = json.loads(STATE.read_text(encoding="utf-8"))
    oid, rid = st["order_id"], st["capture_request_id"]
    path = f"/v2/checkout/orders/{oid}/capture"

    # 1) forced client-side timeout: we do not know if PayPal processed it
    try:
        c.call("POST", path, "capture_forced_timeout", json_body={}, request_id=rid, timeout_s=0.05)
        print("1) first attempt returned before timeout (no timeout forced)")
    except httpx.TimeoutException:
        print("1) client timeout forced; outcome unknown")

    # 2) idempotent retry with the SAME request id
    r2 = c.call("POST", path, "capture_retry_same_id", json_body={}, request_id=rid)
    caps2 = [cap for pu in r2["purchase_units"] for cap in pu["payments"]["captures"]]
    print("2) same-id retry: status", r2["status"], "captures", [(x["id"], x["status"], x["amount"]) for x in caps2])

    # 3) retry with a NEW request id must not capture again
    try:
        c.call("POST", path, "capture_retry_new_id", json_body={}, request_id=str(uuid.uuid4()))
        print("3) NEW-id retry was ACCEPTED -> check for duplicate!")
    except PayPalError as e:
        print("3) new-id retry rejected:", e.status, e.issue, "debug_id", e.debug_id)

    # 4) verify from PayPal
    order = c.call("GET", f"/v2/checkout/orders/{oid}", "get_order")
    caps = [cap for pu in order["purchase_units"] for cap in pu.get("payments", {}).get("captures", [])]
    ok = (order["status"] == "COMPLETED" and len(caps) == 1 and caps[0]["amount"] ==
          {"currency_code": CURRENCY, "value": cents_to_str(AMOUNT_CENTS)} and caps[0]["id"] == caps2[0]["id"])
    print("4) GET order:", order["status"], "captures:", len(caps), [(x["id"], x["status"], x["amount"]) for x in caps])
    print("RESULT:", "PASS" if ok else "FAIL")
    st["capture_id"] = caps[0]["id"] if caps else None
    STATE.write_text(json.dumps(st), encoding="utf-8")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    {"create": create, "capture": capture}[sys.argv[1]]()
