"""Sandbox host: charge a buyer's SAVED PayPal method more than once after ONE buyer approval (PayPal Vault v3), D-101 R2-B.

Usage (run with the .venv python; credentials come from the checkout named by --creds-from, never printed):
  sandbox_vault.py adopt --evidence <jsonl>      take the approved setup token's id out of the evidence file of the setup probe
  sandbox_vault.py token                         GET the setup token (must be APPROVED), POST /v3/vault/payment-tokens ONCE, GET it
  sandbox_vault.py plan   --count N --amount-cents C      print the exact order requests (ids masked) and their fingerprint
  sandbox_vault.py orders --count N --amount-cents C --approve <fingerprint>
                                                 N orders with payment_source.paypal.vault_id; each request is approved by the
                                                 seller before it is sent: the command refuses to send unless --approve is the fingerprint
                                                 `plan` printed for these exact requests. Capture (when the order is not already
                                                 captured), then a GET of each order and of each capture.

The setup-token id, the payment-token (vault) id, order and capture ids are sensitive: they are kept in the private state file
(data/sandbox-seed/vault-state.json, ignored by git; a path git would track is refused) and shown masked. A buyer hand is an order
that comes back PAYER_ACTION_REQUIRED (it would need the buyer to sign in again); the receipt line fails above 0."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_orders as so  # noqa: E402
import sandbox_seed as seed  # noqa: E402

STATE_NAME = "vault-state.json"
SETUP_PATH = "/v3/vault/setup-tokens/"
TOKEN_PATH = "/v3/vault/payment-tokens"
ORDER_PATH = "/v2/checkout/orders"
RECEIPT = "{n} cobros sobre un método guardado tras una aprobación del comprador; manos del comprador por venta: {hands}"


class Stop(Exception):
    pass


def mask(identifier: str) -> str:
    """A sensitive identifier is shown as its leading four and trailing three characters."""
    text = str(identifier or "")
    return text if len(text) <= 8 else f"{text[:4]}...{text[-3:]}"


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"schema": 1}

    def save(self, **updates) -> None:
        self.data.update(updates)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".pending")
        temporary.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)


def order_body(vault_id: str, amount_cents: int, label: str) -> dict:
    """An order paid from the saved method: no experience_context, no approve link, the buyer is not asked again."""
    return {"intent": "CAPTURE",
            "purchase_units": [{"reference_id": label, "description": f"DEMO DATA - {label}",
                                "amount": {"currency_code": so.CURRENCY, "value": so.cents_to_str(amount_cents)},
                                "shipping": {"name": {"full_name": so.SHIPPING_NAME}, "address": so.SHIPPING_ADDRESS}}],
            "payment_source": {"paypal": {"vault_id": vault_id}}}


def plan_bodies(vault_id: str, count: int, amount_cents: int, label: str = "VAULT-SALE") -> list[dict]:
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise Stop("N must be a whole number from 1 to 10")
    return [order_body(vault_id, amount_cents, f"{label}-{i}") for i in range(1, count + 1)]


def fingerprint(bodies: list[dict]) -> str:
    """Binds the approval to the exact requests (including the vault id): any change gives another value."""
    return hashlib.sha256(json.dumps(bodies, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def masked_view(bodies: list[dict]) -> list[dict]:
    view = json.loads(json.dumps(bodies))
    for body in view:
        paypal = body["payment_source"]["paypal"]
        paypal["vault_id"] = mask(paypal["vault_id"])
    return view


def first_capture(order: dict) -> dict:
    for unit in order.get("purchase_units", []):
        for cap in unit.get("payments", {}).get("captures", []):
            if isinstance(cap, dict):
                return cap
    return {}


def adopt_setup_id(evidence: Path) -> str:
    """The setup token's id as the probe's read-back GET line recorded it in its path."""
    found = None
    for line in evidence.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        match = re.fullmatch(re.escape(SETUP_PATH) + r"([A-Za-z0-9_-]{6,64})", str(record.get("path", "")))
        if match and record.get("method") == "GET" and record.get("status") == 200:
            found = match.group(1)
    if not found:
        raise Stop("No successful GET of a setup token in that evidence file")
    return found


def take_payment_token(client, state: State, emit=print) -> str:
    """GET the setup token (it must be APPROVED), then ONE POST of the payment token and a GET of it. Idempotent on the state."""
    if state.data.get("payment_token_id"):
        emit(f"Payment token already created: {mask(state.data['payment_token_id'])} (not created again).")
        return state.data["payment_token_id"]
    setup_id = state.data.get("setup_token_id")
    if not setup_id:
        raise Stop("No setup token in the state: run adopt before this command")
    setup = client.call("GET", SETUP_PATH + setup_id, "vault_setup_token_read")
    emit(f"Setup token {mask(setup_id)}: status {setup.get('status')}")
    if setup.get("status") != "APPROVED":
        raise Stop(f"The setup token is {setup.get('status')}, not APPROVED; the buyer's approval is not complete")
    state.save(setup_status=setup.get("status"), payment_token_requested=True)
    created = client.call("POST", TOKEN_PATH, "vault_payment_token_create",
                          json_body={"payment_source": {"token": {"id": setup_id, "type": "SETUP_TOKEN"}}},
                          request_id=state.data.setdefault("payment_token_request_id", str(uuid.uuid4())))
    token_id = created.get("id")
    if not token_id:
        raise Stop("PayPal created no payment token id")
    state.save(payment_token_id=token_id)
    read = client.call("GET", TOKEN_PATH + "/" + token_id, "vault_payment_token_read")
    emit(f"Payment token {mask(token_id)}: created, read back (customer {mask((read.get('customer') or {}).get('id', ''))})")
    return token_id


def run_orders(client, state: State, count: int, amount_cents: int, approve: str, emit=print) -> dict:
    """N orders on the saved method. Returns {'sales': n captured, 'buyer_hands': orders that needed the buyer again}."""
    vault_id = state.data.get("payment_token_id")
    if not vault_id:
        raise Stop("No payment token in the state: run token before this command")
    bodies = plan_bodies(vault_id, count, amount_cents)
    expected = fingerprint(bodies)
    if approve != expected:
        raise Stop("Not sent: the approval does not match these exact requests. Run plan and approve its fingerprint.")
    sales, hands = 0, 0
    for index, body in enumerate(bodies, start=1):
        key = f"sale{index}"
        done = state.data.get(key, {})
        if done.get("capture_status") == "COMPLETED":
            sales += 1
            emit(f"Sale {index}: already captured ({mask(done.get('capture_id', ''))}).")
            continue
        request_id = done.get("order_request_id") or str(uuid.uuid4())
        emit(f"Sale {index}: request approved by the seller before sending (fingerprint {approve})")
        state.save(**{key: {**done, "order_request_id": request_id}})
        order = client.call("POST", ORDER_PATH, "vault_order_create", json_body=body, request_id=request_id,
                            log_extra={"label": body["purchase_units"][0]["reference_id"]})
        order_id = order.get("id")
        state.save(**{key: {**state.data[key], "order_id": order_id, "created_status": order.get("status")}})
        emit(f"Sale {index}: order {mask(order_id)} created, status {order.get('status')}")
        if order.get("status") == "PAYER_ACTION_REQUIRED":
            hands += 1                                   # the buyer would have to sign in again: a hand, counted from PayPal's answer
            emit(f"Sale {index}: PayPal asks the buyer again; stopped here, nothing captured.")
            break
        read = client.call("GET", f"{ORDER_PATH}/{order_id}", "vault_order_read")
        if read.get("status") == "APPROVED" and not first_capture(read):
            client.call("POST", f"{ORDER_PATH}/{order_id}/capture", "vault_order_capture", json_body={},
                        request_id=state.data[key].get("capture_request_id") or str(uuid.uuid4()))
            read = client.call("GET", f"{ORDER_PATH}/{order_id}", "vault_order_read_after_capture")
        capture = first_capture(read)
        if read.get("status") != "COMPLETED" or capture.get("status") != "COMPLETED":
            raise Stop(f"Sale {index}: order {read.get('status')}, capture {capture.get('status')}; not a completed charge")
        got = client.call("GET", f"/v2/payments/captures/{capture['id']}", "vault_capture_read")
        if got.get("status") != "COMPLETED":
            raise Stop(f"Sale {index}: the capture read back as {got.get('status')}")
        state.save(**{key: {**state.data[key], "capture_id": capture["id"], "capture_status": got["status"],
                            "amount": got.get("amount")}})
        sales += 1
        emit(f"Sale {index}: capture {mask(capture['id'])} {got['status']} {got.get('amount', {}).get('value')} "
             f"{got.get('amount', {}).get('currency_code')} (read back by GET)")
    return {"sales": sales, "buyer_hands": hands}


def receipt(result: dict, count: int) -> str:
    if result["sales"] != count:
        raise Stop(f"{result['sales']} of {count} sales completed; no receipt")
    if result["buyer_hands"] > 0:
        raise Stop(f"manos del comprador por venta: {result['buyer_hands']}, objetivo 0")
    return RECEIPT.format(n=result["sales"], hands=result["buyer_hands"])


SANDBOX_HOST = "api-m.sandbox.paypal.com"


def assert_sandbox_host(client) -> None:
    """Refuse any client that is not pointed at the PayPal sandbox API (the project's SandboxClient already is; this checks it)."""
    base = str(getattr(getattr(client, "_http", None), "base_url", ""))
    if urlparse(base).hostname != SANDBOX_HOST:
        raise Stop("Not sent: this client is not pointed at " + SANDBOX_HOST)


def _state_path(root: Path, given: Path | None) -> Path:
    path = Path(given) if given else seed.default_private_dir(root) / STATE_NAME
    refusal = seed.refuse_trackable(root, path, seed.default_private_dir(root))
    if refusal:
        raise Stop(refusal)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["adopt", "token", "plan", "orders"])
    ap.add_argument("--creds-from", type=Path, help="checkout whose .env holds the sandbox app credentials")
    ap.add_argument("--evidence-dir", type=Path)
    ap.add_argument("--evidence", type=Path)
    ap.add_argument("--state", type=Path)
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--amount-cents", type=int, default=2500)
    ap.add_argument("--approve", default="")
    args = ap.parse_args(argv)
    root = seed.checkout_root()
    try:
        state = State(_state_path(root, args.state))
        if args.command == "adopt":
            if not args.evidence:
                raise Stop("adopt needs --evidence")
            state.save(setup_token_id=adopt_setup_id(args.evidence))
            print(f"Setup token {mask(state.data['setup_token_id'])} kept in the private state.")
            return 0
        if args.command == "plan":
            bodies = plan_bodies(state.data.get("payment_token_id", "NOT-YET-CREATED"), args.count, args.amount_cents)
            print(json.dumps(masked_view(bodies), indent=2))
            print("Fingerprint to approve:", fingerprint(bodies))
            return 0
        if args.evidence_dir:
            os.environ["PROOFIE_EVIDENCE_DIR"] = str(args.evidence_dir.resolve())
        if args.creds_from:
            from dotenv import load_dotenv
            load_dotenv(args.creds_from / ".env", override=False)
        sys.path.insert(0, str(root))
        from pilot.client import PayPalError, SandboxClient
        client = SandboxClient("sandbox_vault")
        assert_sandbox_host(client)
        try:
            if args.command == "token":
                take_payment_token(client, state)
            else:
                result = run_orders(client, state, args.count, args.amount_cents, args.approve)
                print(receipt(result, args.count))
        except PayPalError as exc:
            print(f"PayPal refused: status {exc.status}, issue {exc.issue}, debug_id {exc.debug_id}. Stopped; no retry.")
            return 1
    except Stop as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
