"""The case-linked sale checklist consumes Proofie's existing dispute/capture/document/package stores."""
import hashlib
import html
import os
import re

from fastapi.testclient import TestClient


def test_case_fulfilment_readiness_terms_and_package_share_case_ids(monkeypatch, tmp_path):
    """Expected before run: habitual case detail links to its existing capture; fulfillment loads the same case's saved
    documents/package, saves seller terms through named fields, displays a linked physical checklist and prepared-only
    tracking payload, then the same package view reads that case terms file. Fake provider transport is GET-only.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "configured-data"))
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_SALE_READINESS", "true")
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")
    dispute_id, capture_id = "PP-R-M10-0000003", "5S647034CC1979815"
    raw = {"dispute_id": dispute_id, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
           "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "INQUIRY",
           "dispute_amount": {"currency_code": "USD", "value": "42.00"},
           "create_time": "2026-10-01T12:00:00Z", "seller_response_due_date": "2026-10-30T12:00:00Z",
           "disputed_transactions": [{"seller_transaction_id": capture_id}],
           "links": [{"rel": "self", "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute_id}", "method": "GET"}]}

    class ReadOnlyPayPal:
        def __init__(self):
            self.calls = []

        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET"
            if path.startswith("/v1/customer/disputes/"):
                return raw
            if path.startswith("/v1/reporting/transactions"):
                return {"transaction_details": [{"transaction_info": {"transaction_id": capture_id,
                         "transaction_initiation_date": "2026-10-01T12:00:00Z",
                         "transaction_amount": {"currency_code": "USD", "value": "42.00"}},
                         "shipping_info": {"name": "Synthetic Buyer", "address": {"line1": "1 Sample Road",
                         "city": "Sample City", "state": "CA", "postal_code": "90000", "country_code": "US"}}}]}
            if path.startswith("/v1/shipping/trackers"):
                return {"trackers": []}
            raise AssertionError(f"unexpected transport operation {op}: {path}")

    db = config.db_path()
    doc_ids = []
    for filename, body in (("order.txt", "Order summary"), ("tracking.txt", "Shipment reference"),
                           ("delivery.txt", "Delivery note")):
        digest = hashlib.sha256(body.encode()).hexdigest()
        doc_ids.append(repo.insert_document(db, dispute_id, filename, "text/plain", len(body), digest, body, True))
    package_id = repo.insert_package(db, dispute_id, "human_edit", True, [])
    repo.insert_claims(db, package_id, [{"evidence_index": 0, "claim_index": 0, "text": "Order recorded.",
                                         "source_ids": [f"doc_{doc_ids[0]}"], "basis": "seller_statement"}])
    for document_id in doc_ids:
        repo.attach_document(db, package_id, document_id)

    fake = ReadOnlyPayPal()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))

    detail = client.get(f"/disputes/{dispute_id}/workspace/prepare")
    assert detail.status_code == 200 and 'data-state="case-sale-readiness"' in detail.text
    assert f"/fulfilment/{capture_id}?from_dispute={dispute_id}&amp;from_package={package_id}#sale-readiness" in detail.text      # O4: the link carries the package
    page = client.get(f"/fulfilment/{capture_id}?from_dispute={dispute_id}")
    assert page.status_code == 200 and 'data-state="sale-readiness"' in page.text
    assert "Record sale terms and classify case documents" in page.text
    assert "seller sale terms not supplied" in page.text
    token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
    form = {"csrf_token": token, "dispute_id": dispute_id, "kind": "physical", "buyer_ref": "BUYER-SUPPLIED-3",
            "description": "Handmade lamp", "variant": "Blue", "price_amount": "42.00", "currency": "USD",
            "expected_date": "2026-10-25", "returns": "30 day returns", "contact": "seller support",
            "terms_version": "terms-v3", "acceptance_ref": "acceptance-ref-3", "carrier": "UPS",
            "tracking_number": "SYNTHETIC-TRACK-3", "ship_to_name": "Synthetic Buyer",
            "ship_to_street": "1 Sample Road", "ship_to_city": "Sample City", "ship_to_region": "CA",
            "ship_to_postal_code": "90000", "ship_to_country": "US"}
    form.update({f"evidence_kind_{doc_id}": kind for doc_id, kind in zip(doc_ids, ("order", "tracking", "delivery"))})
    unlinked_capture = "5S647034CC1979816"
    unlinked = client.post(f"/fulfilment/{unlinked_capture}/readiness", data=form)
    assert unlinked.status_code == 400 and "not linked to the current recorded case snapshot" in unlinked.text
    saved = client.post(f"/fulfilment/{capture_id}/readiness", data=form, follow_redirects=False)
    assert saved.status_code == 303 and f"from_dispute={dispute_id}" in saved.headers["location"]
    ready = client.get(saved.headers["location"])
    assert ready.status_code == 200 and "ready for human review" in ready.text
    assert "USD 4200 cents" in ready.text and "prepared local Tracking payload only; no tracking POST" in ready.text
    assert "acceptance-ref-3" in ready.text and all(f"ID {doc_id}" in ready.text for doc_id in doc_ids)
    assert f'href="/disputes/{dispute_id}/packages/{package_id}#attachments"' in ready.text

    current_token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', ready.text).group(1))
    mx_form = {**form, "csrf_token": current_token, "carrier": "Estafeta", "tracking_number": "SELLER-SUPPLIED-MX-1"}
    mx_saved = client.post(f"/fulfilment/{capture_id}/readiness", data=mx_form, follow_redirects=False)
    assert mx_saved.status_code == 303
    mx_ready = client.get(mx_saved.headers["location"])
    assert mx_ready.status_code == 200 and "MEX_ESTAFETA" in mx_ready.text
    assert "local Tracking payload only; no tracking POST" in mx_ready.text
    assert "not independently verified" in mx_ready.text
    assert "SELLER-SUPPLIED-MX-1" in mx_ready.text

    current_token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', mx_ready.text).group(1))
    other_form = {**form, "csrf_token": current_token, "carrier": "Skydropx", "tracking_number": "SELLER-SUPPLIED-AGG-1"}
    other_saved = client.post(f"/fulfilment/{capture_id}/readiness", data=other_form, follow_redirects=False)
    assert other_saved.status_code == 303
    other_ready = client.get(other_saved.headers["location"])
    assert other_ready.status_code == 200 and "Skydropx" in other_ready.text
    assert "underlying carrier&#39;s proof" in other_ready.text or "underlying carrier's proof" in other_ready.text
    assert "Tracking preparation payload" in other_ready.text
    assert "OTHER — seller-supplied name: Skydropx" in other_ready.text
    assert "Carrier risk warning" in other_ready.text
    assert "underlying carrier&#39;s proof" in other_ready.text or "underlying carrier's proof" in other_ready.text
    assert "no carrier response or acceptance is verified" in other_ready.text

    # Visual baseline/browser evidence is an explicit operation, never a side effect of HTTP regression tests.
    latest_page = other_ready
    if os.environ.get("P04_CAPTURE") == "true":
        import importlib.util
        import subprocess
        from pathlib import Path

        base = "801881fe"
        base_service_source = subprocess.check_output(
            ["git", "show", f"{base}:copilot/features/sale_readiness/service.py"], text=True, encoding="utf-8")
        base_template = subprocess.check_output(
            ["git", "show", f"{base}:copilot/web/templates/features/fulfilment/form.html"],
            text=True, encoding="utf-8")
        base_namespace = {"__name__": "sale_readiness_base_801881fe"}
        exec(compile(base_service_source, f"{base}:sale_readiness/service.py", "exec"), base_namespace)
        from jinja2 import ChoiceLoader, DictLoader
        from copilot.features.sale_readiness import service as readiness_service
        from copilot.web.routes_disputes import templates
        current_case_view, current_build = readiness_service.case_view, readiness_service.build
        current_loader = templates.env.loader
        try:
            readiness_service.case_view = base_namespace["case_view"]
            readiness_service.build = base_namespace["build"]
            templates.env.loader = ChoiceLoader([
                DictLoader({"features/fulfilment/form.html": base_template}), current_loader])
            templates.env.cache.clear()
            before = client.get(other_saved.headers["location"])
            assert before.status_code == 200 and "Tracking preparation payload" not in before.text
        finally:
            readiness_service.case_view, readiness_service.build = current_case_view, current_build
            templates.env.loader = current_loader
            templates.env.cache.clear()
        after = client.get(other_saved.headers["location"])
        assert after.status_code == 200 and "OTHER — seller-supplied name: Skydropx" in after.text
        browser_path = (Path(__file__).parents[3] / "internal/lanes/nivel2/mejoras-en-producto-2026-10-31/runs/luna-a/browser.py")
        spec = importlib.util.spec_from_file_location("luna_a_browser", browser_path)
        browser_harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(browser_harness)
        browser_harness.capture(before.text, after.text)
        latest_page = after

    current_token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', latest_page.text).group(1))
    restored = client.post(f"/fulfilment/{capture_id}/readiness",
                           data={**form, "csrf_token": current_token}, follow_redirects=False)
    assert restored.status_code == 303

    package = client.get(f"/disputes/{dispute_id}/packages/{package_id}")
    assert package.status_code == 200 and 'data-state="integrated-sale-readiness"' in package.text
    assert "Ready for you to review." in package.text and "42.00 USD" in package.text
    assert f"transaction <span translate=\"no\">{capture_id}</span>" in package.text
    assert "Prepared locally only; nothing was registered" in package.text
    assert "This internal checklist is not a PayPal requirement or eligibility decision" in package.text
    assert "Delivery is a later-stage record" in package.text
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)


def test_readiness_route_rejects_missing_csrf(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "csrf.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_FEATURE_SALE_READINESS", "true")

    class NoNetwork:
        def call(self, *args, **kwargs):
            raise AssertionError("CSRF rejection must precede provider access")

    app = create_app()
    app.dependency_overrides[get_client] = lambda: NoNetwork()
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    response = client.post("/fulfilment/5S647034CC1979815/readiness", data={"dispute_id": "PP-R-M10-0000003"})
    assert response.status_code == 403
