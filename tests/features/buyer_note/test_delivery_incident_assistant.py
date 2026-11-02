from __future__ import annotations

import hashlib
import html
import json
import os
import re
import struct
import subprocess
import zlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient


BASELINE = "41245e74e8cb58ad73de7958920aeeaf3791471c"
DISPUTE = "PP-R-O3-0000001"
CAPTURE = "5S647034CC1979815"
OTHER_CAPTURE = "5S647034CC1979816"
FOREIGN_DISPUTE = "PP-R-O3-0000002"


def _png(color: tuple[int, int, int, int]) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    header = struct.pack(">2I5B", 1, 1, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(color))) + chunk(b"IEND", b""))


LABEL = _png((168, 96, 48, 255))
PARCEL = _png((64, 128, 184, 255))


def _csrf(body: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert match
    return html.unescape(match.group(1))


def test_delivery_incidents_use_saved_sources_and_vendor_response(monkeypatch, tmp_path):
    """Expected: three issue paths consume persisted facts; response is a local vendor audit event on the same package."""
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.features.fulfilment import store as fulfilment_store
    from copilot.features.fulfilment.tracker_request import build_tracker_body
    from copilot.features.sale_readiness import service as readiness
    from copilot.features.ship_moment_capture import service as moment_service
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(name, raising=False)
    for feature in ("fulfilment", "sale_readiness", "buyer_note", "ship_moment_capture", "carrier_directory",
                    "delivery_facts", "transit_delivered_guide", "lost_package_paths", "sale_lesson"):
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature.upper()}", "true")

    raw_dispute = {"dispute_id": DISPUTE, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                   "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "INQUIRY",
                   "dispute_amount": {"currency_code": "MXN", "value": "420.00"},
                   "create_time": "2026-10-01T12:00:00Z", "seller_response_due_date": "2026-11-30T12:00:00Z",
                   "disputed_transactions": [{"seller_transaction_id": CAPTURE}], "links": []}

    class ReadOnlyProvider:
        def __init__(self):
            self.calls = []

        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET"
            if path.startswith("/v1/customer/disputes/"):
                return raw_dispute
            if path.startswith("/v1/reporting/transactions"):
                return {"transaction_details": [{"transaction_info": {
                    "transaction_id": CAPTURE, "transaction_initiation_date": "2026-10-01T12:00:00Z",
                    "transaction_amount": {"currency_code": "MXN", "value": "420.00"}}}]}
            if path.startswith("/v1/shipping/trackers"):
                return {"trackers": []}
            raise AssertionError(f"unexpected fake GET {op}: {path}")

    fake = ReadOnlyProvider()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12457))
    package_id = repo.insert_package(config.db_path(), DISPUTE, "human_edit", True, [],
                                     effective_next_step="provide_evidence")

    terms = {"kind": "physical", "buyer_ref": "BUYER-O3-1", "description": "Handmade lamp",
             "variant": "Blue", "price_cents": 42000, "currency": "MXN", "expected_date": "2026-11-04",
             "returns": "30 day returns", "contact": "seller support", "terms_version": "terms-v4",
             "acceptance_ref": "acceptance-o3-1", "carrier": "Skydropx", "tracking_number": "SELLER-O3-1",
             "ship_to": {"name": "Synthetic Buyer", "street": "1 Sample Road", "city": "Sample City",
                         "region": "CDMX", "postal_code": "01000", "country": "MX"}, "evidence_kinds": {}}
    readiness.save_case_terms(DISPUTE, CAPTURE, terms)
    tracker_request = build_tracker_body(CAPTURE, "SELLER-O3-1", "OTHER", "Skydropx")
    request_sha = hashlib.sha256(json.dumps(tracker_request, sort_keys=True).encode()).hexdigest()
    fulfilment_store.save_file(CAPTURE, tracker_request, request_sha, request_sha, None, {},
                               "saved_without_registration", "Synthetic saved request; no Tracking POST was made.")
    moment = moment_service.save(CAPTURE, "Skydropx", ("label.png", "image/png", LABEL),
                                 ("parcel.png", "image/png", PARCEL), dispute_id=DISPUTE)
    linked_docs = moment_service.link_case_documents(DISPUTE, CAPTURE, moment)
    assert len(linked_docs) == 2

    # Consume C's read contract only. This test fixture seeds a local seller-authored row, not through C's writer.
    lesson_path = tmp_path / "data" / "sale_lesson" / "lessons.jsonl"
    lesson_path.parent.mkdir(parents=True, exist_ok=True)
    lesson_path.write_text(json.dumps({"dispute_id": "PP-R-O3-OLD-1", "capture_id": "12345678901234567",
        "sale_type": "physical", "paypal_outcome_code": "RESOLVED_SELLER_FAVOUR",
        "seller_lesson": "Photograph the label before packing.", "created_at": "2026-10-01T00:00:00+00:00",
        "lesson_source": "seller-entered", "outcome_source": "PayPal dispute GET"}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    prepare_url = f"/disputes/{DISPUTE}/workspace/prepare"
    prepare = client.get(prepare_url)
    assert prepare.status_code == 200
    action_url = f"/fulfilment/{CAPTURE}/delivery-incident?from_dispute={DISPUTE}&incident=delivered_not_received&locale=en&from_package={package_id}"
    assert action_url.replace("&", "&amp;") in prepare.text
    assert "Review a delivery issue with the saved shipment" in prepare.text

    delivered = client.get(action_url)
    assert delivered.status_code == 200
    assert "Delivered status, buyer says not received" in delivered.text
    assert "SELLER-O3-1" in delivered.text and "OTHER" in delivered.text and "Skydropx" in delivered.text
    assert "underlying carrier&#39;s proof" in delivered.text or "underlying carrier's proof" in delivered.text
    assert "not independently confirmed delivery" in delivered.text
    assert "tracking-request" not in delivered.text.lower()  # evidence classification is not copied from Tracking
    assert "Tracking and evidence classification are separate" in delivered.text
    assert "label.png" in delivered.text and "parcel.png" in delivered.text
    assert hashlib.sha256(LABEL).hexdigest() in delivered.text and hashlib.sha256(PARCEL).hexdigest() in delivered.text
    assert f"/disputes/{DISPUTE}/packages/{package_id}#attachments" in delivered.text
    assert "Photograph the label before packing." in delivered.text
    assert "Seller interpretation; not a PayPal-confirmed cause" in delivered.text

    delayed = client.get(f"/fulfilment/{CAPTURE}/delivery-incident?from_dispute={DISPUTE}&incident=delay&locale=es&from_package={package_id}")
    assert delayed.status_code == 200 and "Retraso del envío" in delayed.text
    assert "Fecha estimada registrada por el vendedor: 4 nov 2026" in delayed.text
    missing = client.get(f"/fulfilment/{CAPTURE}/delivery-incident?from_dispute={DISPUTE}&incident=missing&locale=en&from_package={package_id}")
    assert missing.status_code == 200 and "Shipment may be missing" in missing.text
    assert "No carrier-specific path was prepared" in missing.text

    foreign_package = repo.insert_package(config.db_path(), FOREIGN_DISPUTE, "human_edit", True, [])
    action = f"/fulfilment/{CAPTURE}/delivery-incident/response?from_dispute={DISPUTE}&from_package={package_id}"
    before_audit = repo.list_audit(config.db_path(), DISPUTE)
    bad_csrf = client.post(action, data={"csrf_token": "invalid", "from_dispute": DISPUTE,
        "from_package": str(package_id), "incident": "delay", "state": "buyer_reports_delay"}, follow_redirects=False)
    assert bad_csrf.status_code == 403
    foreign = client.post(action, data={"csrf_token": _csrf(missing.text), "from_dispute": DISPUTE,
        "from_package": str(foreign_package), "incident": "delay", "state": "buyer_reports_delay"}, follow_redirects=False)
    assert foreign.status_code == 404
    assert repo.list_audit(config.db_path(), DISPUTE) == before_audit
    response = client.post(action, data={"csrf_token": _csrf(missing.text), "from_dispute": DISPUTE,
        "from_package": str(package_id), "incident": "delay", "locale": "es",
        "state": "buyer_reports_delay"}, follow_redirects=False)
    assert response.status_code == 303
    assert f"from_package={package_id}" in response.headers["location"]
    return_page = client.get(response.headers["location"])
    assert return_page.status_code == 200 and f"/disputes/{DISPUTE}/packages/{package_id}#attachments" in return_page.text
    rows = [row for row in repo.list_audit(config.db_path(), DISPUTE)
            if row.get("action") == "delivery_incident_response"]
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "vendor" and row["detail"]["source"] == "seller_recorded"
    assert row["detail"]["capture_id"] == CAPTURE and row["detail"]["incident"] == "delay"
    assert row["detail"]["response"] == "buyer_reports_delay" and row["detail"]["package_id"] == package_id
    assert row["detail"]["readiness_schema_version"] == 1
    assert row["detail"]["source_hashes"]["tracking_request"]
    assert row["detail"]["source_hashes"]["readiness_terms"]
    assert set(row["detail"]["source_hashes"]["shipment_photos"]) == {"label", "parcel"}
    assert repo.list_attachments(config.db_path(), package_id) == []
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)

    # A capture with no readiness link may show sale-level guidance but cannot associate photos or record a case response.
    second_request = build_tracker_body(OTHER_CAPTURE, "SELLER-O3-2", "OTHER", "Skydropx")
    second_sha = hashlib.sha256(json.dumps(second_request, sort_keys=True).encode()).hexdigest()
    fulfilment_store.save_file(OTHER_CAPTURE, second_request, second_sha, second_sha, None, {},
                               "saved_without_registration", "Synthetic request; no Tracking POST was made.")
    missing_link = client.get(f"/fulfilment/{OTHER_CAPTURE}/delivery-incident?from_dispute={DISPUTE}&incident=missing&from_package={package_id}")
    assert missing_link.status_code == 404
    no_case = client.get(f"/fulfilment/{OTHER_CAPTURE}/delivery-incident?from_dispute={DISPUTE}&incident=missing")
    assert no_case.status_code == 200 and "not linked to this case through schema-1 readiness" in no_case.text
    assert "associated with the case" not in no_case.text
    assert "delivery-incident-vendor-response" not in no_case.text
    assert len([row for row in repo.list_audit(config.db_path(), DISPUTE)
                if row.get("action") == "delivery_incident_response"]) == 1
    assert repo.list_attachments(config.db_path(), package_id) == []

    if os.environ.get("OLA3_CAPTURE") == "true":
        from jinja2 import ChoiceLoader, DictLoader
        import copilot.web.routes_disputes as dispute_routes
        from copilot.web.routes_disputes import templates
        class CaptureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                fixed = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        monkeypatch.setattr(dispute_routes, "datetime", CaptureClock)
        monkeypatch.setattr(dispute_routes.timefmt, "now_label", lambda: "2026-11-01T02:00:00+00:00")
        baseline_fragment = subprocess.run(
            ["git", "show", f"{BASELINE}:copilot/web/templates/case/_sale_readiness.html"],
            check=True, capture_output=True, text=True, encoding="utf-8").stdout
        original_loader = templates.env.loader
        try:
            templates.env.loader = ChoiceLoader([DictLoader({"case/_sale_readiness.html": baseline_fragment}), original_loader])
            templates.env.cache.clear()
            before_html = client.get(prepare_url).text
        finally:
            templates.env.loader = original_loader
            templates.env.cache.clear()
        after_html = client.get(prepare_url).text
        harness_path = Path(__file__).parents[3] / "internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola3/luna-a/browser.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("ola3_luna_a_browser", harness_path)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        harness.capture(before_html, after_html, capture_id=CAPTURE, label_bytes=LABEL, parcel_bytes=PARCEL)


def test_known_mx_carrier_uses_existing_transit_and_missing_package_guides(monkeypatch, tmp_path):
    """Expected: a saved known MX code/status uses the existing guides without contacting the carrier."""
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.features.fulfilment import store as fulfilment_store
    from copilot.features.fulfilment.tracker_request import build_tracker_body, request_sha256
    from copilot.features.sale_readiness import service as readiness
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(name, raising=False)
    for feature in ("fulfilment", "sale_readiness", "buyer_note", "carrier_directory", "delivery_facts",
                    "transit_delivered_guide", "lost_package_paths"):
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature.upper()}", "true")

    dispute, capture = "PP-R-O3-0000003", "5S647034CC1979817"
    terms = {"kind": "physical", "buyer_ref": "BUYER-O3-MX", "description": "Ceramic cup", "variant": "Blue",
             "price_cents": 25000, "currency": "MXN", "expected_date": "2026-11-05", "returns": "30 days",
             "contact": "seller support", "terms_version": "terms-v1", "acceptance_ref": "acceptance-o3-mx",
             "carrier": "Estafeta", "tracking_number": "SELLER-ESTAFETA-O3",
             "ship_to": {"name": "Buyer", "street": "1 Sample Road", "city": "CDMX", "region": "CDMX",
                         "postal_code": "01000", "country": "MX"}, "evidence_kinds": {}}
    readiness.save_case_terms(dispute, capture, terms)
    request = build_tracker_body(capture, "SELLER-ESTAFETA-O3", "MEX_ESTAFETA", None)
    request["trackers"][0]["status"] = "IN_TRANSIT"
    digest = request_sha256(request)
    fulfilment_store.save_file(capture, request, digest, digest, None, {}, "saved_without_registration",
                               "Synthetic saved request; no Tracking POST was made.")
    repo.insert_package(config.db_path(), dispute, "human_edit", True, [])

    class NoProvider:
        def call(self, method, path, op, **kwargs):
            raise AssertionError(f"delivery guidance must use saved local sources only: {method} {path} {op}")

    app = create_app()
    app.dependency_overrides[get_client] = lambda: NoProvider()
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12458))
    page = client.get(f"/fulfilment/{capture}/delivery-incident?from_dispute={dispute}&incident=missing&locale=en")
    assert page.status_code == 200
    assert "Shipment shown as in transit" in page.text
    assert "it does not show delivery" in page.text
    assert 'data-state="existing-carrier-path-guide"' in page.text
    assert "Ask the carrier to search/trace" in page.text
    assert "checked 2026-10-25" in page.text
    assert "Proofie does not contact the carrier" in page.text
