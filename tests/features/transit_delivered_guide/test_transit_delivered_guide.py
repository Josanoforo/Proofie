import pytest

from copilot.features.transit_delivered_guide.service import guidance_for


@pytest.mark.parametrize("saved, expected", [
    ("In transit", "in_transit"),
    ("en tránsito", "in_transit"),
    ("Shipped", "in_transit"),
    ("Delivered", "delivered"),
    ("entregado", "delivered"),
])
def test_saved_status_selects_distinct_non_verdict_guidance(saved, expected):
    result = guidance_for(saved)
    assert result["status"] == expected
    assert "verified" not in result["body"].casefold()


@pytest.mark.parametrize("saved", [None, "", "not delivered", "no entregado", "unknown"])
def test_unknown_or_negative_status_has_no_delivery_guidance(saved):
    assert guidance_for(saved) is None


def test_render_include_and_live_package_context_are_flag_gated():
    from pathlib import Path
    template = Path("copilot/web/templates/package_review.html").read_text(encoding="utf-8")
    route = Path("copilot/web/routes_package.py").read_text(encoding="utf-8")
    include = Path("copilot/web/templates/features/transit_delivered_guide/_entry.html").read_text(encoding="utf-8")
    assert 'feature_enabled("transit_delivered_guide") and transit_delivered_guidance' in template
    assert "manual_fields.get(\"delivery_status\")" in route
    assert 'data-state="transit-delivered-guidance"' in include
    assert "does not change the checks" in include


@pytest.mark.parametrize("saved, state, heading", [
    ("In transit", "in_transit", "Shipment shown as in transit"),
    ("Delivered", "delivered", "Shipment shown as delivered"),
])
def test_real_package_review_uses_only_the_saved_status(saved, state, heading, monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setenv("COPILOT_FEATURE_TRANSIT_DELIVERED_GUIDE", "true")
    from test_routes_package import DID, _client
    from copilot.db import repo
    client, db, _ = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", False, [])
    repo.set_manual_fields(db, DID, {"delivery_status": {"value": saved, "confidence": "read"}})
    response = client.get(f"/disputes/{DID}/packages/{package}")
    assert response.status_code == 200
    assert f'data-shipment-state="{state}"' in response.text
    assert heading in response.text
    assert "does not change the checks or approval requirements" in response.text


def test_real_package_review_omits_guide_when_flag_is_off(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    monkeypatch.setenv("COPILOT_FEATURE_TRANSIT_DELIVERED_GUIDE", "false")
    from test_routes_package import DID, _client
    from copilot.db import repo
    client, db, _ = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", False, [])
    repo.set_manual_fields(db, DID, {"delivery_status": {"value": "Delivered", "confidence": "read"}})
    response = client.get(f"/disputes/{DID}/packages/{package}")
    assert response.status_code == 200
    assert "transit-delivered-guidance" not in response.text
