from pathlib import Path

import pytest

from copilot.features.first_dispute_guide import service, store


def test_guide_uses_case_dates_and_country_setting_without_inference():
    guide = service.build_guide("MX", {"stage": "INQUIRY", "status": "OPEN",
        "create_time": "2026-10-25T10:00:00Z", "update_time": None,
        "seller_response_due_date": "2026-10-27T23:59:59Z"})
    assert guide["policy_country"] == "Mexico"
    assert guide["stage"] == "inquiry"
    assert guide["created_at"] == "2026-10-25 10:00 UTC"
    assert guide["deadline"] == "2026-10-27 23:59 UTC"
    assert guide["updated_at"] is None


def test_unknown_country_and_dates_stay_unavailable():
    guide = service.build_guide("??", {})
    assert guide["policy_country"] == "not identified"
    assert guide["deadline"] is None and guide["created_at"] is None
    assert "not identified" in guide["country_note"]
    assert guide["after_adverse_decision"] is None


@pytest.mark.parametrize("country,expected_url", [
    ("MX", "https://www.gob.mx/profeco/documentos/tuviste-algun-problema-de-consumo"),
    ("US", "https://www.usa.gov/state-consumer"),
])
def test_after_adverse_paths_only_follow_exact_paypal_buyer_favor_read(country, expected_url):
    resolved = {"status": "RESOLVED", "stage": "CHARGEBACK"}
    section = service.build_guide(country, resolved, outcome_code="RESOLVED_BUYER_FAVOUR")["after_adverse_decision"]
    assert section["checked_on"] == "2026-10-25"
    assert section["paths"]["authority_url"] == expected_url
    assert section["paths"]["authority_url"].startswith("https://")
    assert "Proofie stops here" in section["note"]
    assert "eligibility" in section["authority_note"]
    assert service.build_guide(country, resolved, outcome_code="RESOLVED_SELLER_FAVOUR")["after_adverse_decision"] is None
    assert service.build_guide(country, {"status": "OPEN"}, outcome_code="RESOLVED_BUYER_FAVOUR")["after_adverse_decision"] is None


def test_us_after_adverse_guide_links_local_court_information_without_advice():
    section = service.build_guide("US", {"status": "RESOLVED"}, outcome_code="RESOLVED_BUYER_FAVOUR")["after_adverse_decision"]
    assert section["paths"]["court_url"] == "https://www.usa.gov/courts"
    assert "legal advice" in section["authority_note"]


def test_us_narrative_uses_the_configured_country_and_live_case_stage():
    guide = service.build_guide("US", {"stage": "CHARGEBACK", "seller_response_due_date": "2026-11-02T00:00:00Z"})
    assert guide["policy_country"] == "United States"
    assert guide["stage"] == "claim"
    assert guide["deadline"] == "2026-11-02 00:00 UTC"


def test_first_visit_preference_is_separate_from_case_ledger(tmp_path, monkeypatch):
    from copilot import config
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "case.db")
    assert not store.seen()
    store.mark_seen()
    assert store.seen()
    assert (tmp_path / "first_dispute_guide_seen.json").is_file()
    assert not (tmp_path / "case.db").exists()


def test_guide_is_integrated_in_case_detail_and_reads_case_only():
    from pathlib import Path
    detail = Path("copilot/web/templates/case/_other_responses.html").read_text(encoding="utf-8")
    route = Path("copilot/features/first_dispute_guide/routes.py").read_text(encoding="utf-8")
    assert 'feature_enabled("first_dispute_guide")' in detail
    assert "features/first_dispute_guide/_entry.html" in detail
    assert 'client.call("GET", f"/v1/customer/disputes/{dispute_id}"' in route


def test_packaging_choice_posts_and_reads_back_from_real_case_ledger(monkeypatch, tmp_path):
    import json
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.features.ledger import service as ledger_service
    from copilot.web.routes_disputes import get_client
    from fastapi.testclient import TestClient

    monkeypatch.setenv("COPILOT_FEATURE_FIRST_DISPUTE_GUIDE", "true")
    config.set_db_path_override(tmp_path / "case.db")
    sample = json.loads(Path("pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]

    class Fake:
        def call(self, method, path, op, **kwargs):
            assert method == "GET" and op == "show_dispute"
            return sample

    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    client = TestClient(app, client=("127.0.0.1", 12345), follow_redirects=False)
    response = client.get("/disputes/PP-R-DEMO-0000001/first-guide")
    assert response.status_code == 200
    assert "packaging-retention" in response.text
    assert "No retention choice is recorded" in response.text
    capture_template = Path("copilot/web/templates/features/ship_moment_capture/form.html").read_text(encoding="utf-8")
    assert 'data-state="packaging-retention-reminder"' in capture_template
    saved = client.post("/disputes/PP-R-DEMO-0000001/first-guide/packaging-retention",
                        data={"csrf_token": client.cookies.get("csrf_token"), "retained": "true"})
    assert saved.status_code == 303
    reread = client.get(saved.headers["location"])
    assert "Latest case-ledger entry: seller recorded that these items are being kept" in reread.text
    events = [row for row in repo.list_audit(config.db_path(), "PP-R-DEMO-0000001")
              if row["action"] == "packaging_retention_confirmed"]
    assert len(events) == 1 and events[0]["detail"]["retained"] is True
    ledger = ledger_service.read(config.db_path(), "PP-R-DEMO-0000001")
    assert "audit_log" in ledger["available_sources"]
    assert any(row["origin"] == "audit_log" for row in ledger["records"])


def test_guide_template_only_shows_adverse_options_when_code_supplies_section():
    page = Path("copilot/web/templates/features/first_dispute_guide/page.html").read_text(encoding="utf-8")
    assert "guide.after_adverse_decision" in page
    assert 'feature_enabled("lost_package_paths")' in page
    assert "Proofie does not file or send anything." in page


def test_case_guide_renders_adverse_options_only_from_paypal_buyer_favor_code(monkeypatch, tmp_path):
    import json
    from copilot import config
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client
    from fastapi.testclient import TestClient

    monkeypatch.setenv("COPILOT_FEATURE_FIRST_DISPUTE_GUIDE", "true")
    monkeypatch.setenv("COPILOT_FEATURE_LOST_PACKAGE_PATHS", "false")
    config.set_db_path_override(tmp_path / "guide-case.db")
    sample = json.loads(Path("pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]

    class Fake:
        raw = sample
        def call(self, method, path, op, **kwargs):
            return self.raw

    fake = Fake()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = TestClient(app, client=("127.0.0.1", 12345), follow_redirects=False)
    url = "/disputes/PP-R-DEMO-0000001/first-guide"
    fake.raw = {**sample, "status": "RESOLVED", "dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"}}
    adverse = client.get(url)
    assert adverse.status_code == 200 and 'data-state="after-adverse-decision"' in adverse.text
    assert "gob.mx/profeco" in adverse.text
    fake.raw = {**sample, "status": "RESOLVED", "dispute_outcome": {"outcome_code": "RESOLVED_SELLER_FAVOUR"}}
    seller_favor = client.get(url)
    assert 'data-state="after-adverse-decision"' not in seller_favor.text
    fake.raw = {**sample, "status": "RESOLVED"}
    missing = client.get(url)
    assert 'data-state="after-adverse-decision"' not in missing.text
