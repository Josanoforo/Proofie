import re

import pytest

from _hosted import authenticated_client, configure_hosted
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web.routes_disputes import get_client

DID = "PP-R-DEMO-0000001"
TXN = "DEMO-TXN-000001"

class FakeClient:
    def __init__(self, with_date=True):
        self.with_date = with_date
        self.calls = []

    def call(self, method, path, op, **kwargs):
        self.calls.append((method, path, op))
        assert method == "GET"
        if op == "show_dispute":
            return {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
                    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    "dispute_life_cycle_stage": "INQUIRY", "create_time": "2026-10-21T00:00:00Z",
                    "dispute_amount": {"currency_code": "USD", "value": "25.00"},
                    "disputed_transactions": [{"seller_transaction_id": TXN}], "evidences": []}
        if op == "context_transaction_search":
            if not self.with_date:
                return {"transaction_details": []}
            return {"transaction_details": [{"transaction_info": {
                "transaction_id": TXN, "transaction_initiation_date": "2026-09-01T12:00:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "25.00"}},
                "shipping_info": {"address": {"country_code": "US"}}}]}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected operation: {op}")

@pytest.fixture
def demo_web(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-access-code")
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    monkeypatch.setenv("COPILOT_FEATURE_DEMO_DOCUMENTS", "true")
    config.set_db_path_override(tmp_path / "demo.db")
    config.set_uploads_dir_override(tmp_path / "uploads")
    fake = FakeClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    db = config.db_path()
    package_id = repo.insert_package(db, DID, "human_edit", True, [])
    yield web, fake, db, package_id
    config.set_db_path_override(None)
    config.set_uploads_dir_override(None)

def _csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)

def test_demo_documents_entry_is_hidden_in_local_mode_even_when_flag_is_on(demo_web, monkeypatch):
    web, fake, db, package_id = demo_web
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    response = web.get(f"/disputes/{DID}")
    assert response.status_code == 200
    assert 'data-state="demo-documents-entry"' not in response.text
    assert f"/packages/{package_id}/demo-documents" not in response.text
    assert {method for method, _, _ in fake.calls} == {"GET"}

def test_demo_documents_attach_generated_files_from_verified_sale_date(demo_web):
    web, fake, db, package_id = demo_web
    url = f"/disputes/{DID}/packages/{package_id}/demo-documents"
    page = web.get(url)
    assert page.status_code == 200
    response = web.post(url, data={"csrf_token": _csrf(page.text)}, follow_redirects=False)
    assert response.status_code == 303
    docs = repo.list_documents(db, DID)
    attached = repo.list_attachments(db, package_id)
    assert len(docs) == len(attached) == 5
    assert {d["filename"] for d in docs} == {a["filename"] for a in attached}
    assert {method for method, _, _ in fake.calls} == {"GET"}
    assert {op for _, _, op in fake.calls} <= {"show_dispute", "context_transaction_search", "context_tracking"}

def test_demo_documents_refuse_without_verified_transaction_date(demo_web):
    web, fake, db, package_id = demo_web
    fake.with_date = False
    url = f"/disputes/{DID}/packages/{package_id}/demo-documents"
    page = web.get(url)
    response = web.post(url, data={"csrf_token": _csrf(page.text)}, follow_redirects=False)
    assert response.status_code == 409
    assert repo.list_documents(db, DID) == []
    assert repo.list_attachments(db, package_id) == []
    assert {method for method, _, _ in fake.calls} == {"GET"}

def test_demo_documents_reject_bad_csrf_before_paypal_reads(demo_web):
    web, fake, db, package_id = demo_web
    url = f"/disputes/{DID}/packages/{package_id}/demo-documents"
    response = web.post(url, data={"csrf_token": "wrong"}, follow_redirects=False)
    assert response.status_code == 403
    assert fake.calls == []
    assert repo.list_documents(db, DID) == []
    assert repo.list_attachments(db, package_id) == []

def test_demo_documents_route_is_absent_when_feature_flag_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-access-code")
    monkeypatch.delenv("COPILOT_FEATURE_DEMO_DOCUMENTS", raising=False)
    config.set_db_path_override(tmp_path / "off.db")
    config.set_uploads_dir_override(tmp_path / "off-uploads")
    try:
        web = authenticated_client(create_app(), client=("127.0.0.1", 12345))
        response = web.get(f"/disputes/{DID}/packages/1/demo-documents")
        assert response.status_code == 404
    finally:
        config.set_db_path_override(None)
        config.set_uploads_dir_override(None)
