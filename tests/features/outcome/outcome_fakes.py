"""A fake PayPal for the outcome tests (synthetic dicts shaped like the Disputes API's described responses; no recorded
body). Records every call as (method, path, op, kwargs)."""
from copilot.core.disputes import PayPalError

DISPUTE_ID = "PP-R-TST-0000002"
CAPTURE = "5S647034CC1979815"
HOST = "https://api-m.sandbox.paypal.com"
BASE = f"/v1/customer/disputes/{DISPUTE_ID}"

TRANSACTIONS = {"transaction_details": [{
    "transaction_info": {"transaction_id": CAPTURE, "transaction_initiation_date": "2026-09-28T10:00:00+0000",
                         "transaction_amount": {"currency_code": "USD", "value": "40.00"}},
    "shipping_info": {"name": "Ana Demo Ficticia", "address": {
        "line1": "Domicilio Demo 123", "city": "Ciudad de Mexico", "state": "CDMX", "country_code": "MX",
        "postal_code": "06600"}}}]}
TRACKERS = {"trackers": [{"transaction_id": CAPTURE, "tracking_number": "DEMO-FDX-000001", "carrier": "FEDEX",
                          "status": "SHIPPED"}]}


def links(*rels):
    return [{"rel": "self", "href": HOST + BASE, "method": "GET"}] + \
           [{"rel": r, "href": f"{HOST}{BASE}/{r.replace('_', '-')}", "method": "POST"} for r in rels]


def dispute_raw(status="WAITING_FOR_SELLER_RESPONSE", stage="CHARGEBACK", rels=("provide_evidence",), outcome=None):
    raw = {"dispute_id": DISPUTE_ID, "status": status, "dispute_life_cycle_stage": stage,
           "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "create_time": "2026-10-01T15:00:00Z",
           "seller_response_due_date": "2026-10-17T05:59:59.000Z",
           "dispute_amount": {"currency_code": "USD", "value": "40.00"},
           "disputed_transactions": [{"seller_transaction_id": CAPTURE}], "links": links(*rels)}
    if outcome is not None:
        raw["dispute_outcome"] = outcome
    return raw


DECIDED = dict(status="RESOLVED", stage="CHARGEBACK", rels=("appeal",),
               outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR", "amount_refunded": {"currency_code": "USD", "value": "10.50"}})


class FakePayPal:
    """GET of the dispute answers `raw` (or the `after` body once a POST happened); POST answers `post_answer` or raises."""
    def __init__(self, raw=None, after=None, post_raises=None, get_raises=None):
        self.raw = raw if raw is not None else dispute_raw(**DECIDED)
        self.after, self.post_raises, self.get_raises = after, post_raises, get_raises
        self.posted = False
        self.calls = []

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op, kw))
        if method == "POST":
            if self.post_raises:
                raise self.post_raises
            self.posted = True
            return {}
        if path.startswith("/v1/reporting/transactions"):
            return TRANSACTIONS
        if path.startswith("/v1/shipping/trackers"):
            return TRACKERS
        if self.get_raises:
            raise self.get_raises
        return self.after if (self.posted and self.after is not None) else self.raw


def paypal_error(status, issue="ISSUE"):
    return PayPalError(status, issue, "dbg-1", {})
