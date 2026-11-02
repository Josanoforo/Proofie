"""Synthetic gate fixtures; no feature-service dependency."""
import copy
import hashlib
import json
import sqlite3
import pytest
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web.routes_disputes import get_client
from _hosted import authenticated_client
DID = "PP-R-DEMO-0000001"
MESSAGE = "Pick a carrier: PayPal rejects evidence with no carrier"
def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()

class FakeClient:
    def __init__(self):
        self.calls = []
        self.raw = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "dispute_life_cycle_stage": "INQUIRY",
            "dispute_amount": {"currency_code": "USD", "value": "25.00"},
            "create_time": "2026-09-25T16:00:00Z",
            "disputed_transactions": [{"seller_transaction_id": "DEMOTXN0000000001"}],
            "evidences": [], "links": []}
    def call(self, method, path, op, **kw):
        assert method == "GET"
        self.calls.append((method, path, op))
        if "transactions" in path: return {"transaction_details": []}
        if "trackers" in path: return {"trackers": []}
        return copy.deepcopy(self.raw)

@pytest.fixture
def prepared(web_mode, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_FEATURE_CARRIER_REQUIRED", "true")
    monkeypatch.setenv("COPILOT_ACCOUNT_COUNTRY", "MX")
    db = config.db_path()
    package = repo.insert_package(db, DID, "human_edit", True, [])
    text = "DEMO DATA\nTracking number: DEMO-FDX-001\nStatus: En tránsito"
    doc = repo.insert_document(db, DID, "demo.txt", "text/plain", len(text), digest(text), text, True)
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0,
        "text": "Shipment is in transit.", "source_ids": [f"doc_{doc}"], "basis": "seller_statement"}])
    return db, package, doc, FakeClient()

def web(fake):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return authenticated_client(app)

def rows(db):
    with sqlite3.connect(db) as c:
        tables = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: c.execute(f'SELECT * FROM "{name}"').fetchall() if name in tables else []
                for name in ("claims", "approvals", "active_approval_receipts", "audit_log")}

def choices(review):
    return {c.key: "seller_statement" if c.verdict == "unverifiable" else "confirmed" for c in review.claims}
