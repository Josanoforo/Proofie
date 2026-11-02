from pathlib import Path

from copilot.features.buyer_note.service import build_note


def test_ship_notice_reuses_buyer_note_template_and_only_recorded_estimate():
    notice = build_note(description="", tracking_number="ABC123", carrier="USPS", expected_date=None, locale="es")
    assert "Guía: ABC123" in notice["text"]
    assert "No hay una fecha estimada registrada" in notice["text"]
    assert notice["estimated_date"] is None


def test_ship_notice_is_linked_to_the_saved_shipment_review_and_never_sends():
    route = Path("copilot/features/ship_notice/routes.py").read_text(encoding="utf-8")
    check = Path("copilot/web/templates/features/fulfilment/check.html").read_text(encoding="utf-8")
    page = Path("copilot/web/templates/features/ship_notice/page.html").read_text(encoding="utf-8")
    assert "store.get_check(capture_id, token)" in route
    assert "build_note(" in route
    assert 'feature_enabled("ship_notice")' in check
    assert "Proofie does not send this text" in page
    assert "client.call" not in route and "httpx" not in route
