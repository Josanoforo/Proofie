"""A fake client and synthetic fixtures for the webhook tests (ids like WH-TEST-1, PP-R-TEST-0000001; no recorded body; the only
host in a fixture is the sandbox one, in cert_url). Records every call as (method, path, op, json_body)."""
import json

from copilot.core.disputes import PayPalError

DISPUTE_ID = "PP-R-TEST-0000001"
WEBHOOK_ID = "WH-REGISTERED-1"
HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api-m.sandbox.paypal.com/v1/notifications/certs/CERT-TEST-1",
    "PAYPAL-TRANSMISSION-ID": "TRANS-TEST-1",
    "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJlLWZvci10ZXN0cw==",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-06T16:00:00Z",
}


def event(event_id="WH-TEST-1", event_type="CUSTOMER.DISPUTE.CREATED", dispute_id=DISPUTE_ID, status="OPEN", **extra):
    """The event body carries a status that DIFFERS from the one the fake GET answers, on purpose."""
    resource = {"status": status}
    if dispute_id is not None:
        resource["dispute_id"] = dispute_id
    return {"id": event_id, "event_type": event_type, "resource_type": "dispute", "resource": resource, **extra}


def raw(ev=None) -> bytes:
    return json.dumps(ev if ev is not None else event()).encode("utf-8")


def dispute_get(status="WAITING_FOR_SELLER_RESPONSE"):
    return {"dispute_id": DISPUTE_ID, "status": status, "dispute_life_cycle_stage": "INQUIRY"}


class FakeClient:
    def __init__(self, verify_answer=None, verify_raises=None, get_answer=None, get_raises=None):
        self.verify_answer = verify_answer if verify_answer is not None else {"verification_status": "SUCCESS"}
        self.verify_raises, self.get_raises = verify_raises, get_raises
        self.get_answer = get_answer if get_answer is not None else dispute_get()
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op, kw.get("json_body")))
        if method == "POST":
            if self.verify_raises:
                raise self.verify_raises
            return self.verify_answer
        if self.get_raises:
            raise self.get_raises
        return self.get_answer

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def gets(self):
        return [c for c in self.calls if c[0] == "GET"]


def paypal_error(status, issue="ISSUE"):
    return PayPalError(status, issue, "dbg-1", {})
