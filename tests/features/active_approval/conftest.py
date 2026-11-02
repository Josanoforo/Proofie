"""Only synthetic recorded-shape GET responses; no PayPal transport."""
import copy
import pytest
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.features.claims.model import digest
from copilot.web.routes_disputes import get_client
from copilot.web.security import ACCESS_COOKIE_NAME, access_cookie_value

DID = "PP-R-DEMO-0000001"


class FakeClient:
    def __init__(self):
        self.raw = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"currency_code": "USD", "value": "25.00"},
                    "create_time": "2026-09-25T16:00:00Z", "disputed_transactions": [{"seller_transaction_id": "DEMOTXN0000000001"}],
                    "evidences": [], "links": [{"rel": "provide_evidence", "method": "POST",
                         "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{DID}/provide-evidence"}]}
        self.calls = []

    def call(self, method, path, op, **kwargs):
        assert method == "GET", "Approval cannot write to PayPal"
        self.calls.append((method, path, op))
        if "transactions" in path:
            return {"transaction_details": []}
        if "trackers" in path:
            return {"trackers": []}
        return copy.deepcopy(self.raw)


def _prepare(monkeypatch, *, with_hosted_access=False):
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    if with_hosted_access:
        monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-active-approval-test-code")
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    fake = FakeClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    db = config.db_path()
    package = repo.insert_package(db, DID, "human_edit", True, [])
    doc_text = "DEMO DATA\nTracking number: DEMO-FDX-001\nCarrier: FedEx\nStatus: En tránsito"
    doc = repo.insert_document(db, DID, "demo.pdf", "application/pdf", len(doc_text), digest(doc_text), doc_text, True)
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "Shipment is in transit.",
                                    "source_ids": [f"doc_{doc}"], "basis": "seller_statement"}])
    web = TestClient(app, base_url="https://testserver" if with_hosted_access else "http://testserver",
                     client=("127.0.0.1", 12345))
    if with_hosted_access:
        web.cookies.set(ACCESS_COOKIE_NAME, access_cookie_value())
    return web, fake, db, package, doc


@pytest.fixture
def prepared(monkeypatch):
    return _prepare(monkeypatch)


@pytest.fixture
def prepared_hosted_access(monkeypatch):
    return _prepare(monkeypatch, with_hosted_access=True)
