from datetime import date
from pathlib import Path

from copilot.features.season_prep import service


def test_mexico_season_windows_are_source_dated_and_code_selected():
    view = service.for_country("MX", today=date(2026, 10, 25))
    seasons = {item["id"]: item for item in view["seasons"]}
    assert seasons["buen_fin_2026"]["state"] == "upcoming"
    assert seasons["buen_fin_2026"]["start"] == "2026-11-13"
    assert "elbuenfin.org" in seasons["buen_fin_2026"]["source_url"]
    assert seasons["school_cycle_2026_27"]["state"] == "active"
    assert seasons["school_cycle_2026_27"]["start"] == "2026-08-31"
    assert "SEP" in seasons["school_cycle_2026_27"]["source_name"]


def test_usps_cutoffs_are_service_specific_and_not_guarantees():
    view = service.for_country("US", today=date(2026, 10, 25))
    holiday = next(item for item in view["seasons"] if item["id"] == "holiday_2026")
    assert [(item["service"], item["date"]) for item in holiday["shipping_cutoffs"]] == [
        ("USPS Ground Advantage and First-Class Mail", "2026-12-17"),
        ("USPS Priority Mail", "2026-12-18"),
        ("USPS Priority Mail Express", "2026-12-19"),
    ]
    assert "actual delivery varies" in holiday["source_note"]
    assert "not other-carrier or all-service cutoffs" in holiday["source_note"]
    assert all("USPS" in item["service"] or item["service"] == "USPS Ground Advantage and First-Class Mail"
               for item in holiday["shipping_cutoffs"])


def test_unknown_country_has_no_invented_seasons():
    view = service.for_country("CA", today=date(2026, 10, 25))
    assert view["available"] is False
    assert view["seasons"] == []
    assert view["carriers"] == []


def test_carrier_notes_are_market_filtered_and_service_qualified():
    view = service.for_country("MX", today=date(2026, 10, 25))
    assert all("MX" in item["markets"] for item in view["carriers"])
    fedex = next(item for item in view["carriers"] if item["id"] == "fedex_mx")
    assert "FedEx Express" in fedex["proof_note"]
    assert "release agreements" in fedex["proof_note"]
    assert fedex["source_url"].startswith("https://")


def test_real_shipment_page_links_to_flagged_case_bound_season_guide():
    source = Path("copilot/web/templates/features/fulfilment/form.html").read_text(encoding="utf-8")
    entry = Path("copilot/web/templates/features/season_prep/_entry.html").read_text(encoding="utf-8")
    assert 'feature_enabled("season_prep")' in source
    assert 'features/season_prep/_entry.html' in source
    assert 'href="/season-prep/{{ capture_id }}"' in entry


def test_local_route_renders_real_guide_without_provider_read(monkeypatch):
    import copilot.config as config
    from copilot.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("COPILOT_FEATURE_SEASON_PREP", "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    client = TestClient(create_app(), client=("127.0.0.1", 12345), follow_redirects=False)
    response = client.get("/season-prep/PP-CAPTURE-TEST")
    assert response.status_code == 200
    assert 'data-season="holiday_2026"' in response.text
    assert "USPS Priority Mail: 2026-12-18" in response.text
    assert "/fulfilment/PP-CAPTURE-TEST" in response.text
    assert "not a delivery guarantee" in response.text
