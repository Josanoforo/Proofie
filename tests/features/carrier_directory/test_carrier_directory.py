from copilot.features.carrier_directory import service


def test_directory_contains_official_tracking_and_source_links():
    entries = service.carriers()
    assert {item["id"] for item in entries} == {
        "estafeta_mx", "dhl_mx", "fedex_mx", "ups_us", "usps_us", "correos_mx",
        "paquetexpress_mx", "redpack_mx", "99minutos_mx", "ivoy_mx"}
    assert all(item["source_url"].startswith("https://") and item["tracking_url"].startswith("https://") for item in entries)
    assert all(item["markets"] and item["proof_note"] for item in entries)


def test_search_is_local_and_market_qualified():
    assert [item["id"] for item in service.find("MX")] == ["estafeta_mx", "dhl_mx", "fedex_mx", "correos_mx", "paquetexpress_mx", "redpack_mx", "99minutos_mx", "ivoy_mx"]
    assert [item["id"] for item in service.find("USPS")] == ["usps_us"]


def test_tracking_formats_are_advisory_not_validators():
    fedex = next(item for item in service.carriers() if item["id"] == "fedex_mx")
    assert "no single format" in fedex["tracking_format"]
    assert "FedEx Express" in fedex["proof_note"]
    assert "release agreements" in fedex["proof_note"]


def test_real_flow_links_to_directory_only_when_flagged():
    from pathlib import Path
    entry = Path("copilot/web/templates/features/ship_moment_capture/_entry.html").read_text(encoding="utf-8")
    assert 'feature_enabled("carrier_directory")' in entry
    assert "features/ship_moment_capture/_carrier_entry.html" in entry
