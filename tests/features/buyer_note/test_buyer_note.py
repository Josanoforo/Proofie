from pathlib import Path

import pytest

from copilot.features.buyer_note import service


def test_note_is_bilingual_and_uses_only_recorded_tracking_facts():
    en = service.build_note(description="Cotton runner", tracking_number="ABC123", carrier="USPS",
                            expected_date="2026-11-02", locale="en")
    es = service.build_note(description="Camino de algodón", tracking_number="ABC123", carrier="USPS",
                            expected_date="2026-11-02", locale="es")
    assert "Recorded estimated delivery: Nov 2, 2026" in en["text"]
    assert "Fecha estimada registrada: 2 nov 2026" in es["text"]
    assert en["tracking_url"] == "https://tools.usps.com/go/TrackConfirmAction_input"
    assert "Does anyone else" in en["text"] and "Have you checked" in en["text"]
    assert "¿Alguien más" in es["text"] and "¿Ya revisaste" in es["text"]
    assert "no ETA is invented" in en["source_note"]


@pytest.mark.parametrize("description", ["buyer@example.com", "PP-R-DEMO-1234567", "Order 123456789"])
def test_note_rejects_buyer_or_paypal_identifiers(description):
    with pytest.raises(ValueError):
        service.build_note(description=description, tracking_number=None, carrier=None, expected_date=None)


def test_note_reports_missing_estimate_without_inventing_a_date():
    note = service.build_note(description="", tracking_number=None, carrier=None, expected_date=None)
    assert "No estimated delivery date is recorded." in note["text"]
    assert note["estimated_date"] is None


@pytest.mark.parametrize("code,name,expected_url", [
    ("MEX_ESTAFETA", "Estafeta", "https://www.estafeta.com/Herramientas/Rastreo"),
    ("UPS", "UPS", "https://www.ups.com/track"),
])
def test_saved_tracking_codes_resolve_to_directory_without_changing_the_code(code, name, expected_url):
    import json
    capture = "5S647034CC1979815"
    row = {"capture_id": capture, "request_json": json.dumps({"trackers": [{
        "transaction_id": capture, "tracking_number": "SELLER-TRACK-44", "carrier": code,
        "carrier_name_other": name, "status": "SHIPPED"}]})}
    note = service.sale_note(capture, row, "en", {"description": "Handmade lamp", "expected_date": "2026-11-04"})
    assert note["carrier_code"] == code
    assert note["carrier_name_other"] == name
    assert note["tracking_url"] == expected_url
    assert note["carrier_source_url"].startswith("https://")
    assert name in note["text"] and "SELLER-TRACK-44" in note["text"]
    assert "Handmade lamp" in note["text"] and "Nov 4, 2026" in note["text"]
    assert note["carrier_warning"] is None
    assert "evidence_carrier" not in note
    assert "not live tracking" in note["source_note"]


def test_saved_other_keeps_name_and_aggregator_warning_separate_from_buyer_copy():
    import json
    capture = "5S647034CC1979815"
    row = {"capture_id": capture, "request_json": json.dumps({"trackers": [{
        "transaction_id": capture, "tracking_number": "SELLER-AGG-44", "carrier": "OTHER",
        "carrier_name_other": "Skydropx", "status": "SHIPPED"}]})}
    note = service.sale_note(capture, row, "es")
    assert note["carrier_code"] == "OTHER"
    assert note["carrier_name_other"] == "Skydropx"
    assert "Skydropx" in note["text"] and "SELLER-AGG-44" in note["text"]
    assert "underlying carrier's proof" in note["carrier_warning"]
    assert note["tracking_url"] is None
    assert note["carrier_source_url"] is None
    assert "Carrier risk warning" not in note["text"]
    assert "OTHER" in note["carrier_source_label"]
    assert "no carrier response" in note["carrier_source_label"]


def test_saved_tracking_request_must_match_capture_id():
    import json
    row = {"capture_id": "5S647034CC1979815", "request_json": json.dumps({"trackers": [{
        "transaction_id": "12345678901234567", "tracking_number": "TRACK-1", "carrier": "UPS"}]})}
    with pytest.raises(ValueError, match="does not contain this sale reference"):
        service.sale_note("5S647034CC1979815", row)


def test_seller_response_is_recorded_in_the_real_case_audit_ledger(tmp_path):
    db = str(tmp_path / "proofie.db")
    service.record_response(db, "PP-R-DEMO-0000001", "delivered")
    rows = service.responses(db, "PP-R-DEMO-0000001")
    assert len(rows) == 1 and rows[0]["state"] == "delivered"
    assert rows[0]["at"]
    with pytest.raises(ValueError):
        service.record_response(db, "PP-R-DEMO-0000001", "anything else")


def test_buyer_note_is_registered_and_no_legacy_buyer_page_route_is_live():
    app = Path("copilot/app.py").read_text(encoding="utf-8")
    template = "".join(p.read_text(encoding="utf-8") for p in [Path("copilot/web/templates/dispute_detail.html"), Path("copilot/web/templates/case_task.html")])   # v3: the entry sits in the Prepare view
    env = Path(".env.example").read_text(encoding="utf-8")
    assert 'feature_enabled("buyer_note")' in app
    assert 'from copilot.features.buyer_note.routes import router as buyer_note_router' in app
    assert 'feature_enabled("buyer_page")' not in app
    assert 'features/buyer_page/_seller_entry.html' not in template
    assert 'COPILOT_FEATURE_BUYER_NOTE=true' in env
    assert "buyer_page" not in env


def test_buyer_note_route_stays_local_and_proposes_through_negotiation_drafts():
    routes = Path("copilot/features/buyer_note/routes.py").read_text(encoding="utf-8")
    template = Path("copilot/web/templates/features/buyer_note/index.html").read_text(encoding="utf-8")
    assert "client.call" not in routes and "httpx" not in routes and "requests" not in routes
    assert "copilot.features.negotiation_drafts import service as drafts" in routes
    assert 'drafts.build(data, drafts.case_history_path(dispute_id))' in routes
    assert 'action="/disputes/{{ dispute_id }}/buyer-note/draft"' in template
    assert "does not confirm current PayPal permission" in template
    assert "Proofie has not sent a message" in template
