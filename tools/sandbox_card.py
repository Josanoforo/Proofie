"""Sandbox only: a card-funded order, then PayPal's sandbox process-chargeback, so a dispute exists with no buyer hand (D-101 B8).

Usage (run with the .venv python; credentials come from --creds-from, never printed):
  sandbox_card.py init-card --brand VISA --number <test number> --expiry YYYY-MM --cvv NNN [--country MX]
                                   keep a PayPal sandbox TEST card in the private state (data/sandbox-seed/card-test.json, ignored by git)
  sandbox_card.py plan  --variant 2|3 [--currency MXN --amount-cents 50000]    print the exact order request (card masked) and its fingerprint
  sandbox_card.py order --variant 2|3 --approve <fingerprint>                  ONE order (the seller approved this exact request), then GET;
                                                                              capture when the order is not already completed, then GET
  sandbox_card.py chargeback --approve <fingerprint>                           ONE process-chargeback on the captured sale, then GET the dispute
  sandbox_card.py plan-case [--reason-code 1330]                               the three fingerprints of the one-command case
  sandbox_card.py case --approve-order A --approve-chargeback B --approve-support C [--reason-code 1330]
                                   order, GETs, chargeback, GET, ONE provide_supporting_info (only if PayPal lists the link), then bounded
                                   GETs until WAITING_FOR_SELLER_RESPONSE with provide_evidence: a case ready to answer, 0 buyer hands
Each run of `order`/`case` creates a sandbox sale and a chargeback, so the sandbox dispute count grows by one per run.
Exit codes of `case`: 0 ready to answer; 3 PayPal is still preparing the case (run the same command again, it resumes from the state and
writes nothing); 1 refused or stopped.
The default reason code is "1310" (item not received); "1330" (not as described) stays selectable. Both are an observed mapping, not documented by PayPal.
The supporting-information body is the shape PayPal answered 200 for: multipart/form-data with ONE part named `input` (application/json) holding
{"notes": "<plain string>"}; no filename, no attachment (support_multipart()). It is sent only while the dispute is UNDER_REVIEW and PayPal lists
the provide_supporting_info link, once per case.
The card number, the capture id and the dispute id of a run stay in the private state files; screens show them masked. A test card number is
a published PayPal test value and never goes into a tracked file. A buyer hand is an order that needs the buyer (PAYER_ACTION_REQUIRED, or a
3D Secure approval link); the receipt fails above 0. There is no refund path."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_orders as so  # noqa: E402
import sandbox_seed as seed  # noqa: E402

CARD_FILE = "card-test.json"
STATE_FILE = "b8-state.json"
ORDER_PATH = "/v2/checkout/orders"
CHARGEBACK_PATH = "/v2/customer-support/process-chargeback"
DISPUTE_PATH = "/v1/customer/disputes/"
RECEIPT = "disputa abierta sobre una venta pagada con tarjeta de prueba; manos del comprador: {hands}"
BILLING = {"address_line_1": "Calle Falsa 123, Col. Demo", "admin_area_2": "Ciudad de Mexico", "admin_area_1": "CDMX",
           "postal_code": "06600", "country_code": "MX"}
# processor_info.reason_code values. "1330" is PayPal's documented example and gave MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED (observed).
# "1310" (Visa 13.1 written the way 13.3 became 1330) gave MERCHANDISE_OR_SERVICE_NOT_RECEIVED (observed on PP-R-OKS-10195856,
# logs-RC44/D101/b8/evidence-2b/sandbox_card.jsonl:6). Both mappings are OBSERVED, not documented by PayPal. Nothing else is accepted.
REASON_CODES = ("1330", "1310")
DEFAULT_REASON_CODE = "1310"          # item not received; "1330" stays selectable
# PayPal's move to WAITING_FOR_SELLER_RESPONSE after the supporting information took 21 s (PTF), about 80 s (OKS) and between 2 and 3 minutes
# (KAV: logs-RC44/D101/b8/evidence-3; RYC took 16 reads); 36 reads 10 s apart (6 minutes) cover those, and the reads are bounded.
POLLS = 36
EXIT_READY, EXIT_REFUSED, EXIT_PREPARING = 0, 1, 3
PREPARING_LINE = "PayPal is still preparing the case; run the same command again to keep waiting (no new writes)."
VARIANTS = {2: {"currency": "MXN", "amount_cents": 50000},      # the merchant's country currency
            3: {"currency": "USD", "amount_cents": 2500}}        # the earlier currency, with a billing address this time


class Stop(Exception):
    pass


SANDBOX_HOST = "api-m.sandbox.paypal.com"


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


def assert_sandbox_host(client) -> None:
    base = str(getattr(getattr(client, "_http", None), "base_url", ""))
    if urlparse(base).hostname != SANDBOX_HOST:
        raise Stop("Not sent: this client is not pointed at " + SANDBOX_HOST)


def load_card(path: Path) -> dict:
    if not path.exists():
        raise Stop("No test card in the private state: run init-card")
    card = json.loads(path.read_text(encoding="utf-8"))
    for key in ("brand", "number", "expiry", "cvv"):
        if not card.get(key):
            raise Stop("The test card record lacks " + key)
    return card


def order_body(card: dict, amount_cents: int, currency: str, label: str, billing: dict | None = None) -> dict:
    return {"intent": "CAPTURE",
            "purchase_units": [{"reference_id": label, "description": f"DEMO DATA - {label}",
                                "amount": {"currency_code": currency, "value": so.cents_to_str(amount_cents)},
                                "shipping": {"name": {"full_name": so.SHIPPING_NAME}, "address": so.SHIPPING_ADDRESS}}],
            "payment_source": {"card": {
                "name": "Ana Demo Ficticia", "number": card["number"], "expiry": card["expiry"], "security_code": card["cvv"],
                "billing_address": billing or dict(BILLING, country_code=card.get("country", "MX")),
                "attributes": {"verification": {"method": "SCA_WHEN_REQUIRED"}},
                "experience_context": {"return_url": "https://example.com/card/return", "cancel_url": "https://example.com/card/cancel"}}}}


def fingerprint(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def masked_view(body: dict) -> dict:
    view = json.loads(json.dumps(body))
    card = view["payment_source"]["card"]
    card["number"] = mask(card["number"])
    card["security_code"] = "***"
    return view


def variant_body(card: dict, variant: int) -> dict:
    if variant not in VARIANTS:
        raise Stop("variant must be 2 or 3")
    spec = VARIANTS[variant]
    return order_body(card, spec["amount_cents"], spec["currency"], f"B8-CARD-{variant}")


def needs_buyer(order: dict) -> bool:
    """A buyer hand: PayPal asks the payer for something (status PAYER_ACTION_REQUIRED, or a payer-action / 3DS link)."""
    if order.get("status") == "PAYER_ACTION_REQUIRED":
        return True
    return any(link.get("rel") in {"payer-action", "approve"} for link in order.get("links", []) if isinstance(link, dict))


def first_capture(order: dict) -> dict:
    for unit in order.get("purchase_units", []):
        for cap in unit.get("payments", {}).get("captures", []):
            if isinstance(cap, dict):
                return cap
    return {}


def chargeback_body(capture: dict, merchant_id: str, brand: str, now: dt.datetime | None = None, reason_code: str = DEFAULT_REASON_CODE) -> dict:
    if reason_code not in REASON_CODES:
        raise Stop("reason_code must be one of " + ", ".join(REASON_CODES))
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")      # noqa: E731
    amount = capture["amount"]
    return {"adjacency": "PAYPAL", "file_layout": "ATCK_CB", "merchant_id": merchant_id, "financial_institution": {"processor": "FDMS"},
            "transaction": {"id_enc": capture["id"], "amount": {"currency_code": amount["currency_code"], "value": amount["value"]}},
            "instrument": {"instrument_type": "CARD", "card_brand": brand.upper(), "external": False,
                           "credit_card_transaction_id": "".join(random.choice("0123456789") for _ in range(16))},
            "dispute": {"id": None, "stage": "CHARGEBACK", "status": "2", "response_date": stamp(now + dt.timedelta(days=10)),
                        "amount": {"currency_code": amount["currency_code"], "value": amount["value"]},
                        "receive_date": stamp(now), "money_movement_date": stamp(now), "event_code": "CHARGEBACK_INITIATED",
                        "event_type": "DEBIT", "chargeback_type": "REPORTED_TO_PROCESSOR",
                        "processor_info": {"reference_number": "".join(random.choice("0123456789") for _ in range(23)),
                                           "reason_code": reason_code, "notes": "TEST_MEMO"}}}


def run_order(client, state: State, card: dict, variant: int, approve: str, emit=print) -> dict:
    body = variant_body(card, variant)
    if approve != fingerprint(body):
        raise Stop("Not sent: the approval does not match this exact request. Run plan and approve its fingerprint.")
    key = f"order{variant}"
    done = state.data.get(key, {})
    request_id = done.get("request_id") or str(uuid.uuid4())
    state.save(**{key: {**done, "request_id": request_id}})
    emit(f"Order request approved by the seller before it is sent (fingerprint {approve}, variant {variant}).")
    order = client.call("POST", ORDER_PATH, "card_order_create", json_body=body, request_id=request_id,
                        log_extra={"label": f"B8-CARD-{variant}"})
    order_id = order["id"]
    state.save(**{key: {**state.data[key], "order_id": order_id, "created_status": order.get("status")}})
    emit(f"Order {mask(order_id)} created, status {order.get('status')}")
    hands = 1 if needs_buyer(order) else 0
    if hands:
        emit("PayPal asks the payer for an action: a buyer hand; stopped, nothing captured.")
        return {"hands": hands, "captured": False}
    read = client.call("GET", f"{ORDER_PATH}/{order_id}", "card_order_read")
    if read.get("status") in {"CREATED", "APPROVED"} and not first_capture(read):
        client.call("POST", f"{ORDER_PATH}/{order_id}/capture", "card_order_capture", json_body={},
                    request_id=state.data[key].get("capture_request_id") or str(uuid.uuid4()))
        read = client.call("GET", f"{ORDER_PATH}/{order_id}", "card_order_read_after_capture")
    capture = first_capture(read)
    if read.get("status") != "COMPLETED" or capture.get("status") != "COMPLETED":
        raise Stop(f"order {read.get('status')}, capture {capture.get('status')}: not a completed charge")
    got = client.call("GET", f"/v2/payments/captures/{capture['id']}", "card_capture_read")
    merchant = (read.get("purchase_units") or [{}])[0].get("payee") or {}
    state.save(**{key: {**state.data[key], "capture_id": capture["id"], "capture_status": got.get("status"),
                        "amount": got.get("amount"), "merchant_id": merchant.get("merchant_id")}, "capture": {
        "id": capture["id"], "amount": got.get("amount"), "merchant_id": merchant.get("merchant_id"), "brand": card["brand"]}})
    emit(f"Capture {mask(capture['id'])} {got.get('status')} {got.get('amount', {}).get('value')} {got.get('amount', {}).get('currency_code')}"
         " (read back by GET)")
    return {"hands": 0, "captured": True}


def run_chargeback(client, state: State, approve: str, emit=print, reason_code: str = DEFAULT_REASON_CODE) -> dict:
    capture = state.data.get("capture")
    if not capture or not capture.get("merchant_id"):
        raise Stop("No completed card capture (with its merchant id) in the state: run order first")
    if approve != chargeback_fingerprint(state, reason_code):
        raise Stop("Not sent: the approval does not match this chargeback request. Run plan-chargeback and approve its fingerprint.")
    return send_chargeback(client, state, approve, emit, reason_code)


def send_chargeback(client, state: State, approve: str, emit, reason_code: str) -> dict:
    """The one process-chargeback POST and the GET of the dispute it opens (the approval was checked by the caller)."""
    capture = state.data["capture"]
    body = chargeback_body(capture, capture["merchant_id"], capture.get("brand", "VISA"), reason_code=reason_code)
    emit(f"Chargeback request approved by the seller before it is sent (fingerprint {approve}).")
    created = client.call("POST", CHARGEBACK_PATH, "card_process_chargeback", json_body=body)
    dispute_id = created.get("dispute_id")
    if not dispute_id:
        raise Stop("process-chargeback answered without a dispute id")
    state.save(dispute_id=dispute_id)
    read = client.call("GET", DISPUTE_PATH + dispute_id, "card_dispute_read")
    emit(f"Dispute {dispute_id}: status {read.get('status')}, reason {read.get('reason')}, stage {read.get('dispute_life_cycle_stage')}")
    return {"dispute_id": dispute_id, "status": read.get("status")}


def chargeback_fingerprint(state: State, reason_code: str = DEFAULT_REASON_CODE) -> str:
    capture = state.data.get("capture") or {}
    return hashlib.sha256(json.dumps({"id": capture.get("id"), "merchant": capture.get("merchant_id"), "amount": capture.get("amount"),
                                      "reason_code": reason_code},
                                     sort_keys=True).encode("utf-8")).hexdigest()[:16]


def receipt(hands: int) -> str:
    if hands > 0:
        raise Stop(f"manos del comprador: {hands}, objetivo 0")
    return RECEIPT.format(hands=hands)


SUPPORT_NOTE = "DEMO DATA - supporting information added from the seller side so this sandbox case can be answered."
SUPPORT_HREF_PATH = "/v1/customer/disputes/{id}/provide-supporting-info"


def case_fingerprints(card: dict, variant: int, reason_code: str) -> tuple[str, str, str]:
    """One approval per write, all derivable before the first write: the order request, the chargeback that follows THIS approved order,
    and the supporting-information note that follows that chargeback."""
    order_fp = fingerprint(variant_body(card, variant))
    spec = VARIANTS[variant]
    chargeback_fp = hashlib.sha256(json.dumps({"kind": "process-chargeback", "after": order_fp, "reason_code": reason_code,
                                               "amount_cents": spec["amount_cents"], "currency": spec["currency"]},
                                              sort_keys=True).encode("utf-8")).hexdigest()[:16]
    support_fp = hashlib.sha256(json.dumps({"kind": "provide-supporting-info", "after": chargeback_fp, "note": SUPPORT_NOTE},
                                           sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return order_fp, chargeback_fp, support_fp


def support_multipart() -> dict:
    """The shape PayPal answered 200 for on PTF: multipart/form-data, ONE part named `input` (application/json) with a plain-string `notes`;
    no filename, no attachment. The client drops its JSON content type so httpx sets the boundary."""
    return {"input": (None, json.dumps({"notes": SUPPORT_NOTE}), "application/json")}


def link_href(dispute: dict, rel: str, method: str = "POST") -> str | None:
    for link in dispute.get("links", []) or []:
        if isinstance(link, dict) and link.get("rel") == rel and str(link.get("method", "")).upper() == method:
            return link.get("href")
    return None


def support_path(href: str, dispute_id: str) -> str:
    """The path of the link PayPal listed, accepted only if it is this dispute's provide-supporting-info on the sandbox host."""
    parsed = urlparse(href or "")
    if parsed.hostname not in {SANDBOX_HOST, None, ""} or parsed.path != SUPPORT_HREF_PATH.format(id=dispute_id):
        raise Stop("The provide_supporting_info link PayPal listed is not this dispute's sandbox path; nothing sent")
    return parsed.path


def run_case(client, state: State, card: dict, variant: int, reason_code: str, approvals: tuple[str, str, str], emit=print,
             sleep=time.sleep, polls: int = POLLS, interval: float = 10.0, link_polls: int = 8) -> dict:
    """Order, chargeback, ONE provide_supporting_info, bounded reads. Every approval is checked before the first write; each write
    carries its own. A refusal or a missing link stops the chain with what was done recorded in the state."""
    expected = case_fingerprints(card, variant, reason_code)
    if tuple(approvals) != expected:
        raise Stop("Not sent: the approvals do not match these exact requests. Run plan-case and approve its three fingerprints.")
    if state.data.get("capture"):
        emit("The order of this case is already captured; not created again.")
    else:
        result = run_order(client, state, card, variant, expected[0], emit=emit)
        if result["hands"]:
            return {"ready": False, "hands": result["hands"], "stopped": "the order asked the payer for an action"}
    if state.data.get("dispute_id"):
        emit("The chargeback of this case was already sent; not sent again.")
        opened = {"dispute_id": state.data["dispute_id"]}
    else:
        opened = send_chargeback(client, state, expected[1], emit, reason_code)
    dispute_id = opened["dispute_id"]
    dispute = client.call("GET", DISPUTE_PATH + dispute_id, "card_case_dispute_read")
    href = link_href(dispute, "provide_supporting_info") if dispute.get("status") == "UNDER_REVIEW" else None
    waited = 0
    while not state.data.get("support_posted") and not href and waited < link_polls:
        sleep(interval)                      # PayPal lists the link some seconds after the chargeback: wait for it, bounded
        waited += 1
        dispute = client.call("GET", DISPUTE_PATH + dispute_id, "card_case_dispute_wait_link")
        href = link_href(dispute, "provide_supporting_info") if dispute.get("status") == "UNDER_REVIEW" else None
    if state.data.get("support_posted"):
        emit("Supporting information was already sent for this case; not sent again.")
    elif not href:
        emit("PayPal lists no provide_supporting_info link for this dispute; nothing sent.")
        return {"ready": False, "hands": 0, "dispute_id": dispute_id, "stopped": "no provide_supporting_info link",
                "status": dispute.get("status")}
    else:
        path = support_path(href, dispute_id)
        emit(f"Supporting-information note approved by the seller before it is sent (fingerprint {expected[2]}).")
        state.save(support_posted=True)
        client.call("POST", path, "card_provide_supporting_info", multipart=support_multipart())
    last = dispute
    for number in range(polls):
        last = client.call("GET", DISPUTE_PATH + dispute_id, "card_case_dispute_poll")
        links = {l.get("rel") for l in last.get("links", []) if isinstance(l, dict)}
        if last.get("status") == "WAITING_FOR_SELLER_RESPONSE" and "provide_evidence" in links:
            emit(f"Dispute {dispute_id}: WAITING_FOR_SELLER_RESPONSE with provide_evidence after {number + 1} read(s).")
            return {"ready": True, "hands": 0, "dispute_id": dispute_id, "status": last.get("status")}
        if number + 1 < polls:
            sleep(interval)
    emit(f"Dispute {dispute_id}: last read {last.get('status')}; the case is not ready to answer yet.")
    return {"ready": False, "preparing": True, "hands": 0, "dispute_id": dispute_id, "stopped": "not ready after the bounded reads",
            "status": last.get("status")}


CASE_RECEIPT = "caso listo para responder; manos del comprador: {hands}; comandos del juez: {commands}"


def _paths(root: Path, state_arg: Path | None):
    folder = seed.default_private_dir(root)
    card_path = folder / CARD_FILE
    state_path = Path(state_arg) if state_arg else folder / STATE_FILE
    for path in (card_path, state_path):
        refusal = seed.refuse_trackable(root, path, folder)
        if refusal:
            raise Stop(refusal)
    return card_path, state_path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["init-card", "plan", "order", "plan-chargeback", "chargeback", "plan-case", "case"])
    ap.add_argument("--creds-from", type=Path)
    ap.add_argument("--evidence-dir", type=Path)
    ap.add_argument("--state", type=Path)
    ap.add_argument("--variant", type=int, default=2)
    ap.add_argument("--approve", default="")
    ap.add_argument("--approve-order", default="")
    ap.add_argument("--approve-chargeback", default="")
    ap.add_argument("--approve-support", default="")
    ap.add_argument("--brand")
    ap.add_argument("--number")
    ap.add_argument("--expiry")
    ap.add_argument("--cvv")
    ap.add_argument("--country", default="MX")
    ap.add_argument("--reason-code", default=DEFAULT_REASON_CODE, choices=REASON_CODES)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = seed.checkout_root()
    try:
        card_path, state_path = _paths(root, args.state)
        state = State(state_path)
        if args.command == "init-card":
            if not (args.brand and args.number and args.expiry and args.cvv):
                raise Stop("init-card needs --brand --number --expiry --cvv")
            card_path.parent.mkdir(parents=True, exist_ok=True)
            card_path.write_text(json.dumps({"brand": args.brand.upper(), "number": args.number, "expiry": args.expiry,
                                             "cvv": args.cvv, "country": args.country}), encoding="utf-8")
            print(f"Test card {args.brand.upper()} {mask(args.number)} kept in the private state.")
            return 0
        if args.command == "plan":
            body = variant_body(load_card(card_path), args.variant)
            print(json.dumps(masked_view(body), indent=2))
            print("Fingerprint to approve:", fingerprint(body))
            return 0
        if args.command == "plan-case":
            order_fp, chargeback_fp, support_fp = case_fingerprints(load_card(card_path), args.variant, args.reason_code)
            print(json.dumps(masked_view(variant_body(load_card(card_path), args.variant)), indent=2))
            print("Fingerprints to approve: order", order_fp, "| chargeback", chargeback_fp, "| supporting info", support_fp)
            return 0
        if args.command == "plan-chargeback":
            print("Fingerprint to approve:", chargeback_fingerprint(state, args.reason_code))
            return 0
        if args.creds_from:
            from dotenv import load_dotenv
            load_dotenv(args.creds_from / ".env", override=False)
        if args.evidence_dir:
            os.environ["PROOFIE_EVIDENCE_DIR"] = str(args.evidence_dir.resolve())
        sys.path.insert(0, str(root))
        from pilot.client import PayPalError, SandboxClient
        client = SandboxClient("sandbox_card")
        assert_sandbox_host(client)
        try:
            if args.command == "case":
                result = run_case(client, state, load_card(card_path), args.variant, args.reason_code,
                                  (args.approve_order, args.approve_chargeback, args.approve_support))
                if result.get("preparing"):
                    print(PREPARING_LINE)
                    return EXIT_PREPARING
                if not result["ready"]:
                    print(f"Case not ready: {result.get('stopped')} (dispute {result.get('dispute_id', '-')}, status {result.get('status', '-')})")
                    return EXIT_REFUSED
                print(CASE_RECEIPT.format(hands=result["hands"], commands=1))
            elif args.command == "order":
                result = run_order(client, state, load_card(card_path), args.variant, args.approve)
                print(f"venta con tarjeta de prueba capturada; manos del comprador: {result['hands']}" if result["captured"] else "No capture: see above.")
            else:
                result = run_chargeback(client, state, args.approve, reason_code=args.reason_code)
                print(receipt(0) + f"; disputa {result['dispute_id']} estado {result['status']}")
        except PayPalError as exc:
            print(f"PayPal refused: status {exc.status}, issue {exc.issue}, debug_id {exc.debug_id}. Stopped; no retry.")
            return 1
    except Stop as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
