from copilot.features.package_insert.service import build_insert
import pytest


def test_insert_is_bilingual_and_uses_directory_tracking_link():
    result = build_insert(tracking_number="ABC123", carrier="USPS", item_description="Cotton runner",
                          seller_contact="shop@example.test")
    assert "before opening a dispute" in result["en"]
    assert "antes de abrir una disputa" in result["es"]
    assert result["tracking_url"] == "https://tools.usps.com/go/TrackConfirmAction_input"
    assert result["seller_contact"] == "shop@example.test"


@pytest.mark.parametrize("kwargs", [
    {"tracking_number": "", "carrier": "USPS", "item_description": "Item", "seller_contact": "Contact"},
    {"tracking_number": "A", "carrier": "USPS", "item_description": "", "seller_contact": "Contact"},
    {"tracking_number": "A", "carrier": "USPS", "item_description": "Item", "seller_contact": ""},
])
def test_insert_rejects_missing_required_local_fields(kwargs):
    with pytest.raises(ValueError):
        build_insert(**kwargs)


def test_insert_uses_no_network_and_does_not_auto_send():
    result = build_insert(tracking_number="ABC123", carrier="USPS", item_description="Book",
                          seller_contact="call the shop")
    assert result["tracking_number"] == "ABC123"
    assert "client.call" not in result and "send" not in result


def test_insert_is_linked_from_the_existing_saved_shipment_review():
    from pathlib import Path
    route = Path("copilot/features/package_insert/routes.py").read_text(encoding="utf-8")
    check = Path("copilot/web/templates/features/fulfilment/check.html").read_text(encoding="utf-8")
    assert 'store.get_check(capture_id, token)' in route
    assert '"/fulfilment/{capture_id}/check/{token}/package-insert"' in route
    assert 'feature_enabled("package_insert")' in check
    assert 'features/package_insert/_entry.html' in check
