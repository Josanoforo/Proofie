"""Inbound links use the actual router IDs; fake GETs and a local SQLite file only."""
import sqlite3

import pytest

from _hosted import authenticated_client
from copilot import config
from copilot.app import create_app
from copilot.web.routes_disputes import get_client

DISPUTE = "PP-R-ABC-12345678"
CAPTURE = "DEMO0000000000001"
TRACKING = "DEMONAV0001"
pytestmark = pytest.mark.usefixtures("web_mode")


class ReadOnlyPayPal:
    def __init__(self, captures=None, unavailable=False):
        self.captures = [CAPTURE] if captures is None else captures
        self.unavailable = unavailable
        self.calls = []

    def call(self, method, path, op, **kwargs):
        self.calls.append((method, path, op))
        assert method == "GET", "navigation must never send a PayPal POST"
        if path.startswith("/v1/reporting/transactions"):
            return {"transaction_details": [{"transaction_info": {
                "transaction_id": CAPTURE, "transaction_initiation_date": "2026-10-01T10:00:00+0000",
                "transaction_amount": {"currency_code": "USD", "value": "40.00"}}}]}
        if path.startswith("/v1/shipping/trackers"):
            return {"trackers": [{"transaction_id": CAPTURE, "tracking_number": TRACKING,
                                  "carrier": "FEDEX", "status": "SHIPPED"}]}
        if path.startswith("/v1/customer/disputes?") or path == "/v1/customer/disputes":
            return {"items": []}  # no automatic package preparation/model task in the inbox
        if path.startswith("/v1/customer/disputes/"):
            if self.unavailable:
                raise ConnectionError("synthetic unavailable dispute")
            return {"dispute_id": DISPUTE, "status": "WAITING_FOR_SELLER_RESPONSE",
                    "dispute_life_cycle_stage": "INQUIRY", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    "create_time": "2026-10-01T15:00:00Z",
                    "dispute_amount": {"currency_code": "USD", "value": "40.00"},
                    "disputed_transactions": [{"seller_transaction_id": item} for item in self.captures],
                    "links": [{"rel": "self", "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{DISPUTE}",
                               "method": "GET"}]}
        raise AssertionError("unexpected fake GET path")


def web(monkeypatch, fake=None, **flags):
    # Every test owns its configuration; source defaults and the acceptance runner's modes are unchanged.
    values = {"resolution": True, "draft": True, "fulfilment": True, "delivery_facts": True,
              "resolution_writes": False, "fulfilment_register": False, "account_cliff": False,
              "triage": False, "outcome_appeal": False}
    values.update(flags)
    for name, value in values.items():
        monkeypatch.setenv("COPILOT_FEATURE_" + name.upper(), "true" if value else "false")
    from copilot.features.draft import model
    def forbidden_model(*args, **kwargs):
        raise AssertionError("navigation must not invoke a model")
    monkeypatch.setattr(model, "default_call", forbidden_model)
    fake = fake or ReadOnlyPayPal()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return authenticated_client(app), fake


@pytest.mark.parametrize("resolution,draft", [(True, True), (True, False), (False, True), (False, False)])
def test_dr1_dispute_entry_links_follow_their_parent_flags(monkeypatch, resolution, draft):
    client, fake = web(monkeypatch, resolution=resolution, draft=draft)
    response = client.get(f"/disputes/{DISPUTE}/workspace/other")   # v3: these links sit in the "Other responses" view
    assert response.status_code == 200
    assert (f'href="/disputes/{DISPUTE}/resolution"' in response.text) is resolution
    assert (f'href="/disputes/{DISPUTE}/draft"' in response.text) is draft
    for feature, enabled in (("resolution", resolution), ("draft", draft)):
        assert client.get(f"/disputes/{DISPUTE}/{feature}").status_code == (200 if enabled else 404)
    assert all(method == "GET" for method, _, _ in fake.calls)


@pytest.mark.parametrize("enabled", [True, False])
def test_dr1_inbox_links_to_the_local_fulfilment_list_only_when_enabled(monkeypatch, enabled):
    client, fake = web(monkeypatch, fulfilment=enabled)
    response = client.get("/disputes")
    assert response.status_code == 200
    assert ('href="/fulfilment"' in response.text) is enabled
    before = list(fake.calls)
    assert client.get("/fulfilment").status_code == (200 if enabled else 404)
    assert fake.calls == before  # the local list has no credentialed client call


def test_dr1_resolution_links_to_capture_facts_and_the_real_saved_file(monkeypatch):
    from copilot.db import repo
    from copilot.core.send import sha256_json
    from copilot.features.fulfilment import store
    assert CAPTURE != DISPUTE and len(CAPTURE) == 17
    repo.insert_package(config.db_path(), DISPUTE, "human_edit", True, [])
    body = {"trackers": [{"transaction_id": CAPTURE, "tracking_number": TRACKING,
                          "carrier": "FEDEX", "status": "SHIPPED"}]}
    store.save_file(CAPTURE, body, sha256_json(body), sha256_json(body),
                    {"tracking_number": TRACKING, "carrier": "FEDEX", "status": "SHIPPED",
                     "shipment_date": "2026-10-07"}, {}, "registered")
    client, fake = web(monkeypatch)
    response = client.get(f"/disputes/{DISPUTE}/resolution")
    assert response.status_code == 200
    assert f'href="/delivery-facts/{CAPTURE}"' in response.text
    assert f'href="/delivery-facts/{DISPUTE}"' not in response.text
    with sqlite3.connect(config.db_path()) as connection:
        before = tuple(connection.iterdump())
    calls = list(fake.calls)
    facts = client.get(f"/delivery-facts/{CAPTURE}")
    assert facts.status_code == 200 and TRACKING in facts.text
    assert facts.headers["cache-control"] == "no-store"
    assert fake.calls == calls
    with sqlite3.connect(config.db_path()) as connection:
        assert tuple(connection.iterdump()) == before


def test_dr1_resolution_filters_non_capture_ids_and_duplicates(monkeypatch):
    client, fake = web(monkeypatch, ReadOnlyPayPal([CAPTURE, CAPTURE, DISPUTE, "../outside", "short"]))
    # The link appears only once a local tracking file exists; before that the quoted sentence stands.
    before = client.get(f"/disputes/{DISPUTE}/resolution")
    assert 'href="/delivery-facts/' not in before.text and "Local delivery facts: available after a local tracking file has been saved." in before.text
    from copilot.features.fulfilment import store as fulfilment_store
    fulfilment_store.save_file(CAPTURE, {"tracking_number": "DEMO-1"}, "a" * 64, "a" * 64, None, None, "saved_without_registration")
    response = client.get(f"/disputes/{DISPUTE}/resolution")
    assert response.status_code == 200
    assert response.text.count(f'href="/delivery-facts/{CAPTURE}"') == 1
    for rejected in (DISPUTE, "../outside", "short"):
        assert f'href="/delivery-facts/{rejected}"' not in response.text
    assert all(method == "GET" for method, _, _ in fake.calls)


@pytest.mark.parametrize("captures,unavailable", [([], False), ([CAPTURE], True)])
def test_dr1_absent_or_unreadable_transaction_has_no_guessed_facts_link(monkeypatch, captures, unavailable):
    client, fake = web(monkeypatch, ReadOnlyPayPal(captures, unavailable))
    response = client.get(f"/disputes/{DISPUTE}/resolution")
    assert response.status_code == 200 and 'href="/delivery-facts/' not in response.text
    assert all(method == "GET" for method, _, _ in fake.calls)


def test_dr1_delivery_flag_off_hides_facts_link_and_preserves_route_404(monkeypatch):
    client, fake = web(monkeypatch, delivery_facts=False)
    response = client.get(f"/disputes/{DISPUTE}/resolution")
    assert response.status_code == 200 and 'href="/delivery-facts/' not in response.text
    calls = list(fake.calls)
    assert client.get(f"/delivery-facts/{CAPTURE}").status_code == 404 and fake.calls == calls


def test_dr1_navigation_does_not_relax_csrf_or_write_switch(monkeypatch):
    client, fake = web(monkeypatch)
    calls = list(fake.calls)
    response = client.post(f"/disputes/{DISPUTE}/resolution/register", data={})
    assert response.status_code == 403 and fake.calls == calls
