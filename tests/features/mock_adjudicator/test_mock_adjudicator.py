from pathlib import Path


def test_unassessable_notice_timing_requirement_is_not_integrated_as_an_evaluator():
    app = Path("copilot/app.py").read_text(encoding="utf-8")
    package = Path("copilot/web/templates/package_review.html").read_text(encoding="utf-8")
    rules_mx = Path("copilot/rules/mx_seller_protection_2025-01-13.json").read_text(encoding="utf-8")
    rules_us = Path("copilot/rules/us_seller_protection_2026-01-26.json").read_text(encoding="utf-8")
    schema = Path("copilot/db/schema.py").read_text(encoding="utf-8")
    fields_section = schema.split("CREATE TABLE IF NOT EXISTS manual_fields", 1)[1].split("CREATE TABLE", 1)[0]
    for rules in (rules_mx, rules_us):
        assert "unauthorized_shipment_within_two_days" in rules
    assert "notice_time" not in fields_section and "notified_at" not in fields_section
    assert "mock_adjudicator" not in app
    assert "mock_adjudicator" not in package
    assert "CREATE TABLE IF NOT EXISTS audit_log" in schema
