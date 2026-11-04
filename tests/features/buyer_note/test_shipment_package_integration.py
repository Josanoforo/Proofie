from __future__ import annotations

import hashlib
import html
import json
import os
import re
import struct
import subprocess
import zlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


DISPUTE = "PP-R-M10-0000014"
CAPTURE = "5S647034CC1979815"


def _png(color: tuple[int, int, int, int]) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    header = struct.pack(">2I5B", 1, 1, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(color))) + chunk(b"IEND", b""))


LABEL = _png((170, 100, 40, 255))
PARCEL = _png((60, 130, 180, 255))
OTHER_CAPTURE = "5S647034CC1979816"


def _token(body: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert match
    return html.unescape(match.group(1))


@pytest.mark.parametrize("country,currency,carrier_input,carrier_code,carrier_name,tracking_url", [
    ("MX", "MXN", "Estafeta", "MEX_ESTAFETA", "Estafeta", "https://www.estafeta.com/Herramientas/Rastreo"),
    ("US", "USD", "UPS", "UPS", "UPS", "https://www.ups.com/track"),
    ("MX", "MXN", "Skydropx", "OTHER", "Skydropx", None),
], ids=["mx-known", "us-known", "mx-other-aggregator"])
def test_saved_shipment_photos_note_and_response_reach_same_case_package(
        monkeypatch, tmp_path, country, currency, carrier_input, carrier_code, carrier_name, tracking_url):
    """Expected: one fake GET-only case/capture/package; shipment-time originals become case docs; the saved
    Tracking OTHER name and readiness terms feed bilingual copy; seller response stays local to this capture.
    Visual before/after runs only when OLA2_CAPTURE=true.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.features.fulfilment import store as fulfilment_store
    from copilot.features.fulfilment.tracker_request import build_tracker_body
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "configured-data"))
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(name, raising=False)
    for feature in ("fulfilment", "sale_readiness", "buyer_note", "ship_moment_capture", "carrier_directory"):
        monkeypatch.setenv(f"COPILOT_FEATURE_{feature.upper()}", "true")

    raw_dispute = {"dispute_id": DISPUTE, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                   "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "INQUIRY",
                   "dispute_amount": {"currency_code": currency, "value": "420.00"},
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
                return {"transaction_details": [{"transaction_info": {"transaction_id": CAPTURE,
                        "transaction_initiation_date": "2026-10-01T12:00:00Z",
                        "transaction_amount": {"currency_code": currency, "value": "420.00"}}}]}
            if path.startswith("/v1/shipping/trackers"):
                return {"trackers": []}
            raise AssertionError(f"unexpected fake GET {op}: {path}")

    fake = ReadOnlyProvider()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    package_id = repo.insert_package(config.db_path(), DISPUTE, "human_edit", True, [],
                                     effective_next_step="provide_evidence")

    case_page = client.get(f"/disputes/{DISPUTE}/workspace/prepare")
    assert case_page.status_code == 200
    # Before the sale terms and the shipment are saved, the case page offers only what works: the plain photo page, no
    # case link and no sale buyer note (LINK-FIX: both answered an error here).
    assert f'href="/fulfilment/{CAPTURE}/moment"' in case_page.text
    assert f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}" not in case_page.text
    assert f"/fulfilment/{CAPTURE}/buyer-note?locale=en" not in case_page.text

    form_page = client.get(f"/fulfilment/{CAPTURE}?from_dispute={DISPUTE}")
    assert form_page.status_code == 200
    terms = {"csrf_token": _token(form_page.text), "dispute_id": DISPUTE, "kind": "physical",
             "buyer_ref": "BUYER-MX-14", "description": "Handmade lamp", "variant": "Blue",
             "price_amount": "420.00", "currency": currency, "expected_date": "2026-11-04",
             "returns": "30 day returns", "contact": "seller support", "terms_version": "terms-v4",
             "acceptance_ref": "acceptance-mx-14", "carrier": carrier_input,
             "tracking_number": f"SELLER-{carrier_code}-14", "ship_to_name": "Synthetic Buyer",
             "ship_to_street": "1 Sample Road", "ship_to_city": "Sample City", "ship_to_region": "CDMX",
             "ship_to_postal_code": "01000" if country == "MX" else "10001",
             "ship_to_country": country}
    saved_terms = client.post(f"/fulfilment/{CAPTURE}/readiness", data=terms, follow_redirects=False)
    assert saved_terms.status_code == 303

    tracking_number = f"SELLER-{country}-14"
    request_body = build_tracker_body(CAPTURE, tracking_number, carrier_code, carrier_name)
    request_sha = hashlib.sha256(json.dumps(request_body, sort_keys=True).encode()).hexdigest()
    fulfilment_store.save_file(CAPTURE, request_body, request_sha, "a" * 64, None, {},
                               "saved_without_registration", "Local fixture; no tracking POST was made.")

    saved_case_page = client.get(f"/disputes/{DISPUTE}/workspace/prepare")
    assert f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}" in saved_case_page.text
    assert f"/fulfilment/{CAPTURE}/buyer-note?locale=en" in saved_case_page.text

    moment_url = f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}"
    moment = client.get(moment_url)
    assert moment.status_code == 200 and "Save shipment-time evidence" in moment.text
    moment_post = client.post(moment_url, data={"csrf_token": _token(moment.text), "from_dispute": DISPUTE,
                                               "carrier": carrier_input},
                             files={"label": ("label.png", LABEL, "image/png"),
                                    "parcel": ("parcel.png", PARCEL, "image/png")},
                             follow_redirects=False)
    assert moment_post.status_code == 303
    saved_moment = client.get(moment_post.headers["location"])
    assert saved_moment.status_code == 200
    assert "Available to the same case" in saved_moment.text
    assert "contents are not extracted or authenticated" in saved_moment.text
    assert f"/disputes/{DISPUTE}/packages/{package_id}#attachments" in saved_moment.text
    assert "View saved original" in saved_moment.text

    from copilot.features.ship_moment_capture import service as moment_service
    record = moment_service.read(CAPTURE)
    assert record["dispute_id"] == DISPUTE
    assert (config.uploads_dir() / "ship_moment_capture" / CAPTURE / record["files"]["label"]).read_bytes() == LABEL
    assert (config.uploads_dir() / "ship_moment_capture" / CAPTURE / record["files"]["parcel"]).read_bytes() == PARCEL
    docs = moment_service.case_documents(DISPUTE, CAPTURE, record)
    assert len(docs) == 2
    wrong_case = client.get(f"/fulfilment/{OTHER_CAPTURE}/moment?from_dispute={DISPUTE}")
    assert wrong_case.status_code == 409
    cross_photo = client.get(f"/fulfilment/{CAPTURE}/moment/label?from_dispute=PP-R-M10-0000099")
    assert cross_photo.status_code == 404

    package_url = f"/disputes/{DISPUTE}/packages/{package_id}"
    package_before_attach = client.get(package_url)
    assert package_before_attach.status_code == 200
    for doc in docs:
        assert doc["filename"] in package_before_attach.text
    assert "not attached to latest package" in package_before_attach.text
    for doc in docs:
        package_page = client.get(package_url)
        attach = client.post(f"{package_url}/attach/{doc['id']}",
                             data={"csrf_token": _token(package_page.text)}, follow_redirects=False)
        assert attach.status_code == 303
    package_after_attach = client.get(package_url)
    assert package_after_attach.status_code == 200
    assert "attached to latest package" in package_after_attach.text
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)

    note_url = f"/fulfilment/{CAPTURE}/buyer-note?locale=es"
    spanish = client.get(note_url)
    assert spanish.status_code == 200, spanish.text
    assert "Handmade lamp" in spanish.text and "4 nov 2026" in spanish.text
    assert carrier_code in spanish.text and carrier_name in spanish.text
    assert tracking_number in spanish.text
    assert f'href="{tracking_url}"' in spanish.text if tracking_url else "No static carrier tracking link is available" in spanish.text
    if carrier_code == "OTHER":
        assert "underlying carrier&#39;s proof" in spanish.text or "underlying carrier's proof" in spanish.text
        assert "No carrier response, acceptance, tracking status, delivery, or receipt is verified" in spanish.text
        assert "no carrier response" in spanish.text
    else:
        assert "buyer-note-carrier-warning" not in spanish.text
    assert 'data-copy-target="sale-buyer-note"' in spanish.text
    assert "never sends it" in spanish.text
    assert "Saved label and parcel originals" in spanish.text
    assert "seller-entered" in spanish.text.lower()
    assert package_url in spanish.text
    assert "/send" not in spanish.text
    english = client.get(f"/fulfilment/{CAPTURE}/buyer-note?locale=en")
    assert english.status_code == 200 and "Hello. Here are the shipment details." in english.text
    assert "Copy for PayPal message" in english.text

    bad_csrf = client.post(f"/fulfilment/{CAPTURE}/buyer-note/response",
                           data={"csrf_token": "wrong-token", "state": "not_received", "locale": "es"},
                           cookies={"csrf_token": "other-token"}, follow_redirects=False)
    assert bad_csrf.status_code == 403
    response = client.post(f"/fulfilment/{CAPTURE}/buyer-note/response",
                           data={"csrf_token": _token(english.text), "state": "not_received", "locale": "es"},
                           follow_redirects=False)
    assert response.status_code == 303
    reread = client.get(response.headers["location"])
    assert reread.status_code == 200 and "Buyer still says item is missing" in reread.text
    rows = repo.list_sale_actions(config.db_path(), CAPTURE)
    assert len(rows) == 1 and rows[0]["actor"] == "vendor" and rows[0]["capture_id"] == CAPTURE
    assert rows[0]["detail"]["response"] == "not_received"
    assert client.get(f"/fulfilment/{OTHER_CAPTURE}/buyer-note").status_code == 404
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)

    if os.environ.get("OLA2_CAPTURE") == "true" and carrier_code == "OTHER":
        base = "fb67d24bf0b14b573f96d3bc70d2e1d9d5cf3665"
        old_service = subprocess.check_output(
            ["git", "show", f"{base}:copilot/features/buyer_note/service.py"], text=True, encoding="utf-8")
        old_template = subprocess.check_output(
            ["git", "show", f"{base}:copilot/web/templates/features/buyer_note/sale.html"],
            text=True, encoding="utf-8")
        baseline = {"__name__": "buyer_note_service_fb67d24"}
        exec(compile(old_service, f"{base}:buyer_note/service.py", "exec"), baseline)
        from jinja2 import ChoiceLoader, DictLoader
        from copilot.features.buyer_note import service as note_service
        from copilot.web.routes_disputes import templates
        current_sale_note, current_loader = note_service.sale_note, templates.env.loader
        try:
            note_service.sale_note = lambda capture, row, locale="en", terms=None: baseline["sale_note"](
                capture, row, locale)
            templates.env.loader = ChoiceLoader([
                DictLoader({"features/buyer_note/sale.html": old_template}), current_loader])
            templates.env.cache.clear()
            before = client.get(note_url)
            assert before.status_code == 200
        finally:
            note_service.sale_note = current_sale_note
            templates.env.loader = current_loader
            templates.env.cache.clear()
        after = client.get(note_url)
        assert after.status_code == 200 and "Saved label and parcel originals" in after.text
        browser_path = Path(__file__).parents[3] / "internal/lanes/nivel2/mejoras-en-producto-2026-10-31/ola2/luna-a/browser.py"
        import importlib.util
        browser_spec = importlib.util.spec_from_file_location("ola2_luna_a_browser", browser_path)
        browser_harness = importlib.util.module_from_spec(browser_spec)
        browser_spec.loader.exec_module(browser_harness)
        browser_harness.capture(before.text, after.text, capture_id=CAPTURE, dispute_id=DISPUTE,
                               label_bytes=LABEL, parcel_bytes=PARCEL)


def test_explicit_package_context_survives_moment_note_posts_and_rejects_foreign_ids(monkeypatch, tmp_path):
    """Expected: older same-case package A remains selected across moment/note GET, POST and return;
    malformed/foreign IDs refuse without latest fallback or writes. No attach, approval, send, or browser.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.features.fulfilment import store as fulfilment_store
    from copilot.features.fulfilment.tracker_request import build_tracker_body
    from copilot.features.ship_moment_capture import service as moment_service
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "configured-data"))
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(name, raising=False)
    for feature in ("fulfilment", "sale_readiness", "buyer_note", "ship_moment_capture", "carrier_directory"):
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
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12346))
    package_a = repo.insert_package(config.db_path(), DISPUTE, "human_edit", True, [],
                                    effective_next_step="provide_evidence")
    package_b = repo.insert_package(config.db_path(), DISPUTE, "human_edit", True, [],
                                    effective_next_step="provide_evidence")
    foreign_package = repo.insert_package(config.db_path(), "PP-R-M10-0000099", "human_edit", True, [],
                                          effective_next_step="provide_evidence")

    form_page = client.get(f"/fulfilment/{CAPTURE}?from_dispute={DISPUTE}")
    assert form_page.status_code == 200
    terms = {"csrf_token": _token(form_page.text), "dispute_id": DISPUTE, "kind": "physical",
             "buyer_ref": "BUYER-CONTEXT-1", "description": "Handmade lamp", "variant": "Blue",
             "price_amount": "420.00", "currency": "MXN", "expected_date": "2026-11-04",
             "returns": "30 day returns", "contact": "seller support", "terms_version": "terms-v1",
             "acceptance_ref": "acceptance-context-1", "carrier": "Estafeta",
             "tracking_number": "SELLER-MX-CTX-1", "ship_to_name": "Synthetic Buyer",
             "ship_to_street": "1 Sample Road", "ship_to_city": "Sample City", "ship_to_region": "CDMX",
             "ship_to_postal_code": "01000", "ship_to_country": "MX"}
    saved_terms = client.post(f"/fulfilment/{CAPTURE}/readiness", data=terms, follow_redirects=False)
    assert saved_terms.status_code == 303
    tracker_body = build_tracker_body(CAPTURE, "SELLER-MX-CTX-1", "MEX_ESTAFETA", "Estafeta")
    request_sha = hashlib.sha256(json.dumps(tracker_body, sort_keys=True).encode()).hexdigest()
    fulfilment_store.save_file(CAPTURE, tracker_body, request_sha, "b" * 64, None, {},
                               "saved_without_registration", "Synthetic local row; no tracking POST was made.")

    prepare = client.get(f"/disputes/{DISPUTE}/workspace/prepare")
    assert prepare.status_code == 200
    assert f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}&amp;from_package={package_b}" in prepare.text
    assert f"/fulfilment/{CAPTURE}/buyer-note?locale=en&amp;from_package={package_b}" in prepare.text

    moment_url = f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}&from_package={package_a}"
    moment_page = client.get(moment_url)
    assert moment_page.status_code == 200
    package_a_url = f"/disputes/{DISPUTE}/packages/{package_a}#attachments"
    package_b_url = f"/disputes/{DISPUTE}/packages/{package_b}#attachments"
    assert package_a_url in moment_page.text and package_b_url not in moment_page.text
    assert f'name="from_package" value="{package_a}"' in moment_page.text

    assert client.get(f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}&from_package=not-an-id").status_code == 404
    assert client.get(f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}&from_package={foreign_package}").status_code == 404
    invalid_photo_post = client.post(f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}&from_package={package_a}",
                                    data={"csrf_token": _token(moment_page.text), "from_dispute": DISPUTE,
                                          "from_package": str(foreign_package), "carrier": "Estafeta"},
                                    files={"label": ("label.png", LABEL, "image/png")}, follow_redirects=False)
    assert invalid_photo_post.status_code == 404
    assert moment_service.read(CAPTURE) is None

    photo_post = client.post(moment_url, data={"csrf_token": _token(moment_page.text), "from_dispute": DISPUTE,
                                               "from_package": str(package_a), "carrier": "Estafeta"},
                             files={"label": ("label.png", LABEL, "image/png")}, follow_redirects=False)
    assert photo_post.status_code == 303
    assert f"from_package={package_a}" in photo_post.headers["location"]
    moment_return = client.get(photo_post.headers["location"])
    assert moment_return.status_code == 200 and package_a_url in moment_return.text
    assert package_b_url not in moment_return.text
    assert f"/moment/label?from_dispute={DISPUTE}&amp;from_package={package_a}" in moment_return.text

    note_url = f"/fulfilment/{CAPTURE}/buyer-note?locale=es&from_package={package_a}"
    note_page = client.get(note_url)
    assert note_page.status_code == 200
    assert package_a_url in note_page.text and package_b_url not in note_page.text
    assert f'name="from_package" value="{package_a}"' in note_page.text
    assert client.get(f"/fulfilment/{CAPTURE}/buyer-note?locale=es&from_package=bad").status_code == 404
    assert client.get(f"/fulfilment/{CAPTURE}/buyer-note?locale=es&from_package={foreign_package}").status_code == 404

    bad_note_post = client.post(f"/fulfilment/{CAPTURE}/buyer-note/response",
                                data={"csrf_token": _token(note_page.text), "state": "delivered", "locale": "es",
                                      "from_package": str(foreign_package)}, follow_redirects=False)
    assert bad_note_post.status_code == 404
    assert repo.list_sale_actions(config.db_path(), CAPTURE) == []
    note_post = client.post(f"/fulfilment/{CAPTURE}/buyer-note/response",
                            data={"csrf_token": _token(note_page.text), "state": "not_received", "locale": "es",
                                  "from_package": str(package_a)}, follow_redirects=False)
    assert note_post.status_code == 303
    assert f"from_package={package_a}" in note_post.headers["location"]
    note_return = client.get(note_post.headers["location"])
    assert note_return.status_code == 200 and package_a_url in note_return.text
    assert package_b_url not in note_return.text

    generic_moment = client.get(f"/fulfilment/{CAPTURE}/moment?from_dispute={DISPUTE}")
    generic_note = client.get(f"/fulfilment/{CAPTURE}/buyer-note?locale=es")
    assert generic_moment.status_code == generic_note.status_code == 200
    assert package_b_url in generic_moment.text and package_b_url in generic_note.text
    assert repo.list_attachments(config.db_path(), package_a) == []
    assert repo.list_attachments(config.db_path(), package_b) == []
    assert len(repo.list_sale_actions(config.db_path(), CAPTURE)) == 1
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
