from pathlib import Path

import pytest


def test_profile_rules_persist_and_adjust_package_insert(tmp_path, monkeypatch):
    from copilot import config
    from copilot.features.trade_profiles import service

    monkeypatch.setattr(config, "_data_dir_from_env", lambda: tmp_path)
    selected = service.select_profile("electronics")
    assert selected["checks"] == ["Record the serial number.", "Photograph the sealed package."]
    assert service.current_profile() == selected
    form = Path("copilot/web/templates/features/package_insert/form.html").read_text(encoding="utf-8")
    printable = Path("copilot/web/templates/features/package_insert/print.html").read_text(encoding="utf-8")
    assert 'data-state="trade-profile-checklist"' in form
    assert 'data-state="trade-profile-reminders"' in printable


def test_unknown_profile_is_rejected():
    from copilot.features.trade_profiles import service

    with pytest.raises(ValueError):
        service.select_profile("paypal-required")


def test_profile_is_integrated_into_saved_shipment_review_and_package_insert(monkeypatch, tmp_path):
    from copilot import config
    from copilot.features.package_insert import routes as insert_routes
    from copilot.features.trade_profiles import service

    monkeypatch.setattr(config, "_data_dir_from_env", lambda: tmp_path)
    monkeypatch.setattr(config, "feature_enabled", lambda name: name == "trade_profiles")
    service.select_profile("crafts")
    assert insert_routes._profile()["label"] == "Crafts"
    check = Path("copilot/web/templates/features/fulfilment/check.html").read_text(encoding="utf-8")
    assert 'feature_enabled("trade_profiles")' in check
    assert 'href="/trade-profile"' in check


def test_profile_is_seller_only_context_for_buyer_note_drafts(monkeypatch, tmp_path):
    from copilot import config
    from copilot.features.buyer_note import routes as note_routes
    from copilot.features.trade_profiles import service

    monkeypatch.setattr(config, "_data_dir_from_env", lambda: tmp_path)
    monkeypatch.setattr(config, "feature_enabled", lambda name: name == "trade_profiles")
    service.select_profile("auto_parts")
    assert note_routes._trade_profile()["label"] == "Auto parts"
    buyer_note = Path("copilot/web/templates/features/buyer_note/index.html").read_text(encoding="utf-8")
    proposal = Path("copilot/web/templates/features/negotiation_drafts/index.html").read_text(encoding="utf-8")
    assert "not included in the buyer message" in buyer_note
    assert "not part of the buyer-facing draft" in proposal
