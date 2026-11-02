"""D-101 B8 step for tools/auto_run.py: a card-funded order and a chargeback open a dispute with no buyer hand, hands counted from the walk.

Runs tools/sandbox_card.py's chain against a stateful stand-in for PayPal (no network, no credentials, no real card number): the order is
created from a card, comes back COMPLETED, is read back, and the chargeback request returns a dispute id that is read by GET. The buyer
hands are counted from the stand-in's answers (an order that asks the payer for an action is a hand). The step prints the receipt line and
FAILS above 0. The real run, when it happens, is in logs-RC44/D101/b8."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_card as sc  # noqa: E402

STEP = "6.10"
STEP_CASE = "6.11"
TITLE_CASE = "Caso listo para responder con un solo comando, manos del comprador contadas"
EXPECTED_CASE = ("orden con tarjeta, contracargo y UNA información de apoyo (solo si PayPal ofrece el enlace) dejan la disputa en WAITING_FOR_SELLER_RESPONSE con provide_evidence; el simulador cuenta las manos del comprador y el paso falla si pasan de 0")
TITLE = "Venta con tarjeta de prueba y contracargo: disputa sin manos del comprador"
EXPECTED = ("la orden con tarjeta se crea COMPLETED sin pedir nada al comprador, se lee por GET, el contracargo devuelve una disputa que se lee por GET; "
            "las manos del comprador se cuentan de las respuestas y fallan si pasan de 0")
CARD = {"brand": "VISA", "number": "4" + "0" * 15, "expiry": "2030-12", "cvv": "987", "country": "MX"}     # a stand-in number, not a test card


class StandIn:
    def __init__(self, ask_payer: bool = False, offer_support_link: bool = True, wait_reads: int = 2, link_after: int = 0):
        self.ask_payer, self.calls, self.orders = ask_payer, [], {}
        self.offer_link, self.supported, self.wait_reads, self.reads = offer_support_link, False, wait_reads, 0
        self.link_after, self.dispute_reads = link_after, 0

    def call(self, method, path, op, *, json_body=None, request_id=None, log_extra=None, **kwargs):
        self.calls.append((method, path))
        if method == "POST" and path == sc.ORDER_PATH:
            assert "card" in json_body["payment_source"] and "billing_address" in json_body["payment_source"]["card"]
            status = "PAYER_ACTION_REQUIRED" if self.ask_payer else "COMPLETED"
            captures = [] if self.ask_payer else [{"id": "STANDINCARDCAP01", "status": "COMPLETED"}]
            unit = {"payee": {"merchant_id": "STANDINMERCHANT"}, "payments": {"captures": captures}}
            self.orders["STANDINCARDORDER1"] = {"id": "STANDINCARDORDER1", "status": status, "purchase_units": [unit],
                                                "links": [{"rel": "payer-action"}] if self.ask_payer else []}
            return self.orders["STANDINCARDORDER1"]
        if path.startswith(sc.ORDER_PATH + "/"):
            return self.orders[path.rsplit("/", 1)[-1]]
        if path.startswith("/v2/payments/captures/"):
            return {"id": path.rsplit("/", 1)[-1], "status": "COMPLETED", "amount": {"currency_code": "MXN", "value": "500.00"}}
        if method == "POST" and path == sc.CHARGEBACK_PATH:
            assert json_body["instrument"]["instrument_type"] == "CARD" and json_body["adjacency"] == "PAYPAL"
            return {"dispute_id": "PP-D-STANDIN-1"}
        if method == "POST" and path.endswith("/provide-supporting-info"):
            assert self.offer_link and not self.supported, "supporting info sent without a link or a second time"
            assert "input" in kwargs.get("multipart", {})
            self.supported, self.reads = True, 0
            return {"links": [{"rel": "detail"}]}
        if path.startswith(sc.DISPUTE_PATH):
            dispute_id = path.rsplit("/", 1)[-1]
            if self.supported:
                self.reads += 1
                ready = self.reads > self.wait_reads
                status = "WAITING_FOR_SELLER_RESPONSE" if ready else "UNDER_REVIEW"
                rels = ["self", "accept_claim", "provide_evidence"] if ready else ["self", "provide_supporting_info"]
            else:
                self.dispute_reads += 1
                shown = self.offer_link and self.dispute_reads > self.link_after
                status, rels = ("UNDER_REVIEW" if shown else "OPEN"), ["self"] + (["provide_supporting_info"] if shown else [])
            links = [{"rel": r, "method": "POST" if r != "self" else "GET",
                      "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute_id}" + ("/provide-supporting-info" if r == "provide_supporting_info" else "")}
                     for r in rels]
            return {"dispute_id": dispute_id, "status": status, "reason": "UNAUTHORISED", "dispute_life_cycle_stage": "CHARGEBACK", "links": links}
        raise AssertionError(f"unexpected call {method} {path}")


def walk(ask_payer: bool = False) -> str:
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        state = sc.State(Path(folder) / sc.STATE_FILE)
        client = StandIn(ask_payer)
        body = sc.variant_body(CARD, 2)
        result = sc.run_order(client, state, CARD, 2, sc.fingerprint(body), emit=lines.append)
        if result["hands"]:
            raise sc.Stop(f"manos del comprador: {result['hands']}, objetivo 0")
        done = sc.run_chargeback(client, state, sc.chargeback_fingerprint(state), emit=lines.append)
        approvals = sum("approved by the seller" in line for line in lines)
        writes = sum(1 for m, _ in client.calls if m == "POST")
        return (sc.receipt(result["hands"]) + f"; disputa {done['dispute_id']} leída por GET; aprobaciones del vendedor mostradas: {approvals} de 2; "
                f"escrituras al simulador: {writes}")


MAX_JUDGE_COMMANDS = 1


def walk_case(offer_support_link: bool = True, ask_payer: bool = False, polls: int = sc.POLLS) -> str:
    """The one-command case against the stand-in: hands counted from the order's answer, commands counted from the calls made."""
    lines: list[str] = []
    commands = 0
    with tempfile.TemporaryDirectory() as folder:
        state = sc.State(Path(folder) / sc.STATE_FILE)
        client = StandIn(ask_payer, offer_support_link)
        approvals = sc.case_fingerprints(CARD, 2, sc.DEFAULT_REASON_CODE)
        while True:                                  # each pass is one command the judge types; a "still preparing" answer means another one
            commands += 1
            result = sc.run_case(client, state, CARD, 2, sc.DEFAULT_REASON_CODE, approvals, emit=lines.append,
                                 sleep=lambda *_: None, polls=polls)
            if result["hands"]:
                raise sc.Stop(f"manos del comprador: {result['hands']}, objetivo 0")
            if result.get("preparing") and commands <= MAX_JUDGE_COMMANDS + 2:
                continue
            break
        if commands > MAX_JUDGE_COMMANDS:
            raise sc.Stop(f"comandos del juez: {commands}, objetivo {MAX_JUDGE_COMMANDS}")
        if not result["ready"]:
            raise sc.Stop(f"el caso no quedó listo: {result.get('stopped')}")
        approvals_shown = sum("approved by the seller" in line for line in lines)
        return sc.CASE_RECEIPT.format(hands=result["hands"], commands=commands) + f"; aprobaciones del vendedor mostradas: {approvals_shown} de 3"


def run(c) -> str:
    return walk()


def run_case_step(c) -> str:
    return walk_case()
