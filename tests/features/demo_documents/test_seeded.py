"""D-101 r1 (R1-5): the seeded demo case proposes its documents and shipment fields. Local mode, fake PayPal that only answers GET."""
import os
import re

import pytest

from _hosted import authenticated_client
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web.routes_disputes import get_client

DID = "PP-R-DEMO-0000001"
TXN = "DEMO-TXN-000001"
SEEDED_NAME = "Ana Demo Ficticia"


class FakeClient:
    def __init__(self, recipient=SEEDED_NAME, with_date=True):
        self.recipient, self.with_date, self.calls = recipient, with_date, []

    def call(self, method, path, op, **kwargs):
        self.calls.append((method, path, op))
        assert method == "GET"
        if op == "show_dispute":
            return {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
                    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    "dispute_life_cycle_stage": "INQUIRY", "create_time": "2026-10-21T00:00:00Z",
                    "dispute_amount": {"currency_code": "USD", "value": "25.00"},
                    "disputed_transactions": [{"seller_transaction_id": TXN}], "evidences": [],
                    "links": [{"rel": "provide_evidence", "method": "POST", "href": "https://api-m.sandbox.paypal.com/x"}]}
        if op == "context_transaction_search":
            if not self.with_date:
                return {"transaction_details": []}
            return {"transaction_details": [{"transaction_info": {
                "transaction_id": TXN, "transaction_initiation_date": "2026-09-01T12:00:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "25.00"}},
                "shipping_info": {"name": self.recipient, "address": {"country_code": "MX", "city": "Ciudad de Mexico"}}}]}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected operation: {op}")


def _app_env(monkeypatch, tmp_path, *, seeded_flag="true", hosted=False):
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true" if hosted else "false")
    if hosted:
        monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-access-code")
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    if seeded_flag is not None:
        monkeypatch.setenv("COPILOT_FEATURE_DEMO_DOCUMENTS_SEEDED", seeded_flag)
    config.set_db_path_override(tmp_path / "seeded.db")
    config.set_uploads_dir_override(tmp_path / "uploads")


@pytest.fixture
def seeded_web(monkeypatch, tmp_path):
    _app_env(monkeypatch, tmp_path)
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


def _url(package_id=None):
    return f"/disputes/{DID}/seeded-demo"


def test_the_confirm_page_labels_demo_data_and_lists_the_proposal_derived_from_the_verified_sale_date(seeded_web):
    web, fake, db, package_id = seeded_web
    page = web.get(_url(package_id))
    assert page.status_code == 200
    assert "DEMO DATA" in page.text and 'data-state="seeded-demo-proposal"' in page.text
    for shown in ("FEDEX", "DEMO-FDX-000001", "Delivered", "2026-09-02", "2026-09-04", "Ana Demo"):   # sale 2026-09-01: +1 and +3 days
        assert shown in page.text
    assert {method for method, _, _ in fake.calls} == {"GET"}
    assert repo.get_manual_fields(db, DID) == {} and repo.list_attachments(db, package_id) == []      # a page changes nothing


def test_one_confirmed_click_attaches_the_five_documents_and_fills_the_fields_and_sends_nothing(seeded_web):
    web, fake, db, package_id = seeded_web
    page = web.get(_url(package_id))
    response = web.post(_url(package_id), data={"csrf_token": _csrf(page.text)}, follow_redirects=False)
    assert response.status_code == 303
    assert len(repo.list_attachments(db, package_id)) == len(repo.list_documents(db, DID)) == 5
    fields = repo.get_manual_fields(db, DID)
    assert {k: v["value"] for k, v in fields.items()} == {
        "carrier": "FEDEX", "tracking_number": "DEMO-FDX-000001", "delivery_status": "Delivered",
        "ship_date": "2026-09-02", "delivery_date": "2026-09-04", "receiver_name": "Ana Demo"}
    assert {method for method, _, _ in fake.calls} == {"GET"}
    assert repo.get_latest_valid_approval(db, package_id) is None            # the seller still approves
    assert any(a["action"] == "apply_seeded_case_proposal" for a in repo.list_actions(db, DID)) if hasattr(repo, "list_actions") else True


def test_what_the_seller_already_typed_wins_over_the_proposal(seeded_web):
    web, fake, db, package_id = seeded_web
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "MYOWN12345", "confidence": "read"}})
    page = web.get(_url(package_id))
    assert web.post(_url(package_id), data={"csrf_token": _csrf(page.text)}, follow_redirects=False).status_code == 303
    fields = repo.get_manual_fields(db, DID)
    assert fields["tracking_number"]["value"] == "MYOWN12345" and fields["carrier"]["value"] == "FEDEX"


def test_applying_twice_attaches_each_document_once(seeded_web):
    web, fake, db, package_id = seeded_web
    for _ in range(2):
        page = web.get(_url(package_id))
        assert web.post(_url(package_id), data={"csrf_token": _csrf(page.text)}, follow_redirects=False).status_code == 303
    assert len(repo.list_attachments(db, package_id)) == 5 and len(repo.list_documents(db, DID)) == 5


def test_a_case_that_is_not_the_seeded_one_is_refused_and_nothing_is_attached(seeded_web):
    web, fake, db, package_id = seeded_web
    fake.recipient = "Someone Else"
    assert web.get(_url(package_id)).status_code == 409
    ok = web.post(_url(package_id), data={"csrf_token": "x"}, follow_redirects=False)
    assert ok.status_code == 403                                      # the csrf check comes first, nothing was read
    fake.recipient = SEEDED_NAME
    page = web.get(_url(package_id))
    fake.recipient = "Someone Else"
    refused = web.post(_url(package_id), data={"csrf_token": _csrf(page.text)}, follow_redirects=False)
    assert refused.status_code == 409 and "not a seeded demo case" in refused.text
    assert repo.list_attachments(db, package_id) == [] and repo.get_manual_fields(db, DID) == {}


def test_without_a_verified_sale_date_nothing_is_attached(seeded_web):
    web, fake, db, package_id = seeded_web
    fake.with_date = False
    assert web.get(_url(package_id)).status_code == 409
    assert repo.list_attachments(db, package_id) == []


def test_bad_csrf_is_refused_before_any_paypal_read(seeded_web):
    web, fake, db, package_id = seeded_web
    assert web.post(_url(package_id), data={"csrf_token": "wrong"}, follow_redirects=False).status_code == 403
    assert fake.calls == []


def test_the_case_page_offers_the_entry_before_any_package_exists(monkeypatch, tmp_path):
    _app_env(monkeypatch, tmp_path)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    try:
        page = authenticated_client(app, client=("127.0.0.1", 12345)).get(f"/disputes/{DID}/workspace/prepare")
        assert page.status_code == 200 and 'data-state="seeded-demo-entry"' in page.text
        assert f'href="/disputes/{DID}/seeded-demo"' in page.text and "DEMO DATA" in page.text
    finally:
        config.set_db_path_override(None)
        config.set_uploads_dir_override(None)


def test_the_route_is_absent_with_the_flag_off(monkeypatch, tmp_path):
    _app_env(monkeypatch, tmp_path, seeded_flag="false")
    try:
        web = authenticated_client(create_app(), client=("127.0.0.1", 12345))
        assert web.get(_url(1)).status_code == 404
        assert 'data-state="seeded-demo-entry"' not in web.get(f"/disputes/{DID}").text
    finally:
        config.set_db_path_override(None)
        config.set_uploads_dir_override(None)


def test_the_route_is_absent_in_the_hosted_demo_even_with_the_flag_on(monkeypatch, tmp_path):
    _app_env(monkeypatch, tmp_path, hosted=True)
    try:
        web = authenticated_client(create_app(), client=("127.0.0.1", 12345))
        assert web.get(_url(1)).status_code == 404
    finally:
        config.set_db_path_override(None)
        config.set_uploads_dir_override(None)


def test_with_no_package_yet_the_click_starts_the_manual_package_too(monkeypatch, tmp_path):
    _app_env(monkeypatch, tmp_path)
    fake = FakeClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    try:
        web = authenticated_client(app, client=("127.0.0.1", 12345))
        db = config.db_path()
        assert repo.list_packages(db, DID) == []
        page = web.get(_url())
        assert page.status_code == 200 and repo.list_packages(db, DID) == []          # the page itself creates nothing
        response = web.post(_url(), data={"csrf_token": _csrf(page.text)}, follow_redirects=False)
        assert response.status_code == 303
        (package,) = repo.list_packages(db, DID)
        assert response.headers["location"].startswith(f"/disputes/{DID}/packages/{package['id']}")
        assert len(repo.list_attachments(db, package["id"])) == 5 and repo.get_manual_fields(db, DID)
        assert {method for method, _, _ in fake.calls} == {"GET"}
    finally:
        config.set_db_path_override(None)
        config.set_uploads_dir_override(None)


def test_the_first_line_of_the_demo_data_page_says_synthetic_and_that_paypal_receives_nothing(seeded_web):
    web, fake, db, package_id = seeded_web
    html = web.get(_url(package_id)).text
    body = html[re.search(r"<h1[^>]*>Use the demo documents", html).start():]
    first_paragraph = re.search(r"<p[^>]*>(.*?)</p>", body, re.S).group(1)
    text = re.sub(r"<[^>]+>", "", first_paragraph)
    assert text.strip() == "DEMO DATA. The five documents are synthetic, and PayPal receives nothing from this click."
    assert body.index('data-state="seeded-demo-label"') < body.index("<table")
