"""A fake PayPal for the resolution tests: a small state machine shaped like the PayPal sandbox's responses
(synthetic dicts, no recorded body). Records every call as (method, path, op)."""
from datetime import datetime, timezone

from copilot.core.disputes import PayPalError

DISPUTE_ID = "PP-R-TST-0000001"
CAPTURE = "5S647034CC1979815"
HOST = "https://api-m.sandbox.paypal.com"
BASE = f"/v1/customer/disputes/{DISPUTE_ID}"

TRANSACTIONS = {"transaction_details": [{
    "transaction_info": {"transaction_id": CAPTURE, "transaction_initiation_date": "2026-09-28T10:00:00+0000",
                         "transaction_amount": {"currency_code": "USD", "value": "40.00"},
                         "protection_eligibility": "01"},
    "shipping_info": {"name": "Ana Demo Ficticia", "address": {
        "line1": "Domicilio Demo 123", "city": "Ciudad de Mexico", "state": "CDMX", "country_code": "MX",
        "postal_code": "06600"}}}]}
TRACKERS = {"trackers": [{"transaction_id": CAPTURE, "tracking_number": "DEMO-FDX-000001", "carrier": "FEDEX",
                          "status": "SHIPPED"}]}

LINK_SETS = {
    "fresh": ("accept_claim", "escalate", "make_offer", "provide_evidence", "send_message"),
    "after_offer": ("provide_evidence", "send_message"),
    "after_message": ("accept_claim", "provide_evidence"),
}


def _links(rels):
    return [{"rel": "self", "href": HOST + BASE, "method": "GET"}] + \
           [{"rel": r, "href": HOST + BASE + "/" + r.replace("_", "-"), "method": "POST"} for r in rels]


class FakePayPal:
    """phase: fresh -> (offer POST) settling for `settling_reads` GETs -> after_offer; a message POST works the same
    way towards after_message. Reads while settling show UNDER_REVIEW with only `self`."""

    def __init__(self, settling_reads=2, post_raises=None, phase="fresh", status="WAITING_FOR_SELLER_RESPONSE",
                 stage="INQUIRY", get_raises=None, amount="40.00", lists_messages=True):
        self.phase, self.settling_reads, self.post_raises = phase, settling_reads, post_raises
        self.status, self.stage, self.get_raises, self.amount = status, stage, get_raises, amount
        self.lists_messages = lists_messages   # PayPal's read-back lists the messages it accepted
        self.messages = []             # raw dispute "messages" as PayPal shows them: posted_by, time_posted, content
        self.pending = None            # the phase to reach once settled
        self.left = 0
        self.calls = []

    # ---- helpers for the tests ----
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def disputes_gets(self):
        return [c for c in self.calls if c[0] == "GET" and c[1].startswith("/v1/customer/disputes/")]

    # ---- the client interface ----
    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if method == "POST":
            return self._post(path, kw)
        if path.startswith("/v1/reporting/transactions"):
            return TRANSACTIONS
        if path.startswith("/v1/shipping/trackers"):
            return TRACKERS
        if path.startswith("/v1/customer/disputes?") or path == "/v1/customer/disputes":
            return {"items": []}
        if self.get_raises:
            raise self.get_raises
        return self._dispute()

    def _post(self, path, kw):
        assert kw.get("json_body") is not None
        if self.post_raises:
            raise self.post_raises
        if path.endswith("/send-message") and self.lists_messages:
            self.messages.append({"posted_by": "SELLER", "content": kw["json_body"]["message"],
                                  "time_posted": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
        self.pending = "after_offer" if path.endswith("/make-offer") else "after_message"
        self.left = self.settling_reads
        return {}

    def _dispute(self):
        if self.pending:
            if self.left > 0:
                self.left -= 1
                return self._raw("UNDER_REVIEW", ())
            self.phase, self.pending = self.pending, None
            self.status = "WAITING_FOR_BUYER_RESPONSE" if self.phase == "after_offer" else "WAITING_FOR_SELLER_RESPONSE"
        return self._raw(self.status, LINK_SETS[self.phase])

    def _raw(self, status, rels):
        extra = {"messages": list(self.messages)} if self.messages else {}
        return {**extra, "dispute_id": DISPUTE_ID, "status": status, "dispute_life_cycle_stage": self.stage,
                "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "create_time": "2026-10-01T15:00:00Z",
                "dispute_amount": {"currency_code": "USD", "value": self.amount},
                "disputed_transactions": [{"seller_transaction_id": CAPTURE}], "links": _links(rels)}


def paypal_error(status, issue="ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE"):
    return PayPalError(status, issue, "dbg-1", {})
