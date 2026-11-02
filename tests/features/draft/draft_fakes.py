"""Fixtures and fakes for the draft tests: a fake model `call` with programmable JSON, and a fake PayPal client (synthetic dicts,
no recorded body). The model call and the PayPal client record every call; neither reaches the network."""
import json

import httpx
import pytest

from copilot.core.disputes import PayPalError
from copilot.features.draft.facts import fact_sheet

DISPUTE_ID = "PP-R-TEST-0000001"
CAPTURE = "5S647034CC1979815"
HOST = "https://api-m.sandbox.paypal.com"
BASE = f"/v1/customer/disputes/{DISPUTE_ID}"

NORM = {"dispute_id": DISPUTE_ID, "stage": "INQUIRY", "create_time": "2026-10-01T15:00:00Z", "amount_cents": 4000,
        "currency": "USD"}
TXN = {"payment_date": "2026-09-28T10:00:00Z", "total_amount_cents": 4000, "total_currency": "USD",
       "shipping_address": {"line1": "Domicilio Demo 123", "city": "Ciudad de Mexico", "state": "CDMX",
                            "postal_code": "06600", "country_code": "MX"}}
TRACK = {"tracking_number": "DEMO-FDX-000001", "carrier": "FEDEX", "tracking_status": "SHIPPED",
         "delivery_estimate": "2026-10-10"}

EN_OK = ("Hello, about your order of 2026-09-28 for 40.00 USD: the shipment DEMO-FDX-000001 with FEDEX shows SHIPPED. "
         "If the package has not arrived by 2026-10-10, the buyer can reply here and the seller will look into it right away.")
ES_OK = ("Hola, sobre su pedido del 2026-09-28 por 40.00 USD: el envío DEMO-FDX-000001 con FEDEX figura como SHIPPED. "
         "Si el paquete no ha llegado el 2026-10-10, responda aquí y lo revisaré de inmediato.")
# The same pair without the delivery estimate: PayPal's data does not carry one, so a draft that names it is (rightly) refused.
EN_NOEST = EN_OK.replace("by 2026-10-10", "by the date on the tracking page")
ES_NOEST = ES_OK.replace("el 2026-10-10", "en la fecha de la página de seguimiento")


def sheet(norm=NORM, txn=TXN, track=TRACK):
    return fact_sheet(norm, txn, track)


def model_call(payload=None, raw=None, metrics=None, raises=None):
    """A fake `call(messages, schema, seed)`; records what it was given in `.calls`."""
    def call(messages, schema, seed):
        call.calls.append((messages, schema, seed))
        if raises is not None:
            raise raises
        content = raw if raw is not None else json.dumps(payload if payload is not None else {"en": EN_OK, "es": ES_OK})
        return content, (metrics if metrics is not None else {"wall_ms": 42})
    call.calls = []
    return call


def links(*rels):
    return [{"rel": "self", "href": HOST + BASE, "method": "GET"}] + \
           [{"rel": r, "href": f"{HOST}{BASE}/{r.replace('_', '-')}", "method": "POST"} for r in rels]


class FakePayPal:
    """GET only: the dispute (with the links given), the transaction and the tracker. Records (method, path, op)."""
    def __init__(self, rels=("send_message", "provide_evidence"), get_raises=None):
        self.rels, self.get_raises, self.calls = rels, get_raises, []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        assert method == "GET", "the draft feature must never write to PayPal"
        if path.startswith("/v1/reporting/transactions"):
            return {"transaction_details": [{
                "transaction_info": {"transaction_id": CAPTURE, "transaction_initiation_date": "2026-09-28T10:00:00+0000",
                                     "transaction_amount": {"currency_code": "USD", "value": "40.00"}},
                "shipping_info": {"name": "Ana Demo", "address": TXN["shipping_address"]}}]}
        if path.startswith("/v1/shipping/trackers"):
            return {"trackers": [{"transaction_id": CAPTURE, "tracking_number": "DEMO-FDX-000001", "carrier": "FEDEX",
                                  "status": "SHIPPED"}]}
        if self.get_raises:
            raise self.get_raises
        return {"dispute_id": DISPUTE_ID, "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "INQUIRY",
                "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "create_time": "2026-10-01T15:00:00Z",
                "dispute_amount": {"currency_code": "USD", "value": "40.00"},
                "disputed_transactions": [{"seller_transaction_id": CAPTURE}], "links": links(*self.rels)}


def paypal_error(status):
    return PayPalError(status, "ISSUE", "dbg-1", {})


@pytest.fixture(autouse=True)
def _no_httpx_calls(monkeypatch):
    """No test of the draft feature may reach Ollama or any network: httpx.post and httpx.get raise if called. Every draft test
    module imports this fixture (a conftest.py here would shadow the repository's own `conftest` module for other test folders)."""
    def refuse(*args, **kwargs):
        raise AssertionError("a draft test reached httpx: no Ollama and no PayPal call is allowed here")
    monkeypatch.setattr(httpx, "post", refuse)
    monkeypatch.setattr(httpx, "get", refuse)
