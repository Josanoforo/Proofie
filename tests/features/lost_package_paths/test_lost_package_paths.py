from copilot.features.lost_package_paths import service


def test_each_local_carrier_has_five_manual_paths_and_cited_service_qualifiers():
    rows = service.carrier_rows()
    assert len(rows) == 10
    for row in rows:
        view = service.paths_for(row["id"], "PP-R-DEMO-0000001")
        assert [path["key"] for path in view["paths"]] == ["tracking", "search", "claim", "insurance", "paypal"]
        assert all(path["source"]["url"].startswith("https://") for path in view["paths"][:4])
        assert all(path["source"]["checked_on"] == "2026-10-25" for path in view["paths"][:4])
    assert "Public sources conflict" in next(row for row in rows if row["id"] == "99minutos_mx")["claim_window"]
    assert "No Mexico-specific claim deadline verified" in next(row for row in rows if row["id"] == "dhl_mx")["claim_window"]


def test_carrier_reference_rejects_empty_controls_and_overlong_values():
    assert service.validate_reference("  CASE-123  ") == "CASE-123"
    for value in ("", " \n ", "a\x00b", "x" * 121):
        try:
            service.validate_reference(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid carrier reference: {value!r}")


def test_case_page_links_local_manual_guide_only_for_nonreceipt_reason():
    from pathlib import Path
    source = Path("copilot/web/templates/case/_other_responses.html").read_text(encoding="utf-8")
    assert 'feature_enabled("lost_package_paths") and norm.reason == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"' in source
    assert 'features/lost_package_paths/_entry.html' in source


def test_enabled_route_renders_without_provider_access(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from fastapi.testclient import TestClient

    config.set_db_path_override(tmp_path / "local.db")
    monkeypatch.setenv("COPILOT_FEATURE_LOST_PACKAGE_PATHS", "true")
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "false")
    monkeypatch.setenv("COPILOT_FEATURE_CARRIER_DIRECTORY", "true")
    client = TestClient(create_app(), client=("127.0.0.1", 12345), follow_redirects=False)
    response = client.get("/disputes/PP-R-DEMO-0000001/lost-package-paths")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.text.count('data-path="') == 5
    assert "Proofie does not contact a carrier" in response.text
    token = client.cookies.get("csrf_token")
    saved = client.post("/disputes/PP-R-DEMO-0000001/lost-package-paths/reference", data={
        "carrier_id": "estafeta_mx", "carrier_reference": "FOLIO-123", "csrf_token": token,
    })
    assert saved.status_code == 303
    reread = client.get(saved.headers["location"])
    assert reread.status_code == 200
    assert "FOLIO-123" in reread.text
    assert "seller-entered, not independently verified" in reread.text
