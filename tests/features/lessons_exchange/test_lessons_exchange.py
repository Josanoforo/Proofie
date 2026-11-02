import json

import pytest


def test_export_contains_only_seller_selected_case_free_categories():
    from copilot.features.lessons_exchange.service import export_payload

    payload = export_payload(country="MX", amount_band="25_to_100", trade_profile="electronics",
        reported_topic="tracking", preparation_reminder="save_tracking_receipt")
    assert payload["context_source"].startswith("seller-selected")
    assert not {"dispute_id", "capture_id", "seller_lesson", "outcome", "exact_amount"} & payload.keys()
    assert json.loads(json.dumps(payload)) == payload


def test_import_requires_exact_schema_and_rejects_case_identifiers():
    from copilot.features.lessons_exchange.service import export_payload, validate_payload

    payload = export_payload(country="US", amount_band="unknown", trade_profile="clothing",
        reported_topic="delivery", preparation_reminder="check_delivery_window")
    with pytest.raises(ValueError):
        validate_payload({**payload, "dispute_id": "PP-R-DEMO-0000001"})
    with pytest.raises(ValueError):
        validate_payload({**payload, "amount_band": []})


def test_import_stores_only_validated_categories_on_this_device(tmp_path, monkeypatch):
    from copilot import config
    from copilot.features.lessons_exchange import service

    monkeypatch.setattr(config, "_data_dir_from_env", lambda: tmp_path)
    payload = service.export_payload(country="MX", amount_band="unknown", trade_profile="crafts",
        reported_topic="packaging", preparation_reminder="photograph_package")
    encoded = (json.dumps(payload) + "\n").encode("utf-8")
    assert service.import_payload(encoded) == payload
    assert service.imported_reminders() == [payload]
    saved = (tmp_path / "lessons_exchange" / "imported.json").read_text(encoding="utf-8")
    assert "PP-" not in saved and "capture_id" not in saved


def test_upload_size_is_bounded():
    from copilot.features.lessons_exchange.service import import_payload

    with pytest.raises(ValueError):
        import_payload(b"{}" + b" " * 8192)


def test_http_export_and_import_keep_case_data_out_of_download(tmp_path, monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from copilot import config
    from copilot.app import create_app
    from copilot.features.sale_lesson import service as sale_lessons
    from copilot.features.lessons_exchange import service

    monkeypatch.setattr(config, "_data_dir_from_env", lambda: tmp_path)
    monkeypatch.setenv("COPILOT_FEATURE_LESSONS_EXCHANGE", "true")
    sale_lessons.record_lesson("PP-R-PRIVATE-CASE", "A" * 17, "physical", "OTHER",
                               "private case text with customer details")
    client = TestClient(create_app(), client=("127.0.0.1", 12345))
    page = client.get("/lessons-exchange")
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    downloaded = client.post("/lessons-exchange/export", data={"csrf_token": token,
        "amount_band": "unknown", "trade_profile": "crafts", "reported_topic": "packaging",
        "preparation_reminder": "photograph_package"})
    assert downloaded.status_code == 200
    assert "PP-R-PRIVATE-CASE" not in downloaded.text
    assert "private case text" not in downloaded.text
    assert "dispute_id" not in downloaded.json() and "capture_id" not in downloaded.json()
    uploaded = client.post("/lessons-exchange/import", data={"csrf_token": token},
        files={"reminder_file": ("reminder.json", downloaded.content, "application/json")},
        follow_redirects=False)
    assert uploaded.status_code == 303
    assert service.imported_reminders()[0]["trade_profile"] == "crafts"


def test_package_insert_reads_imported_reminders_only_when_feature_is_on(tmp_path, monkeypatch):
    from pathlib import Path
    from copilot import config
    from copilot.features.lessons_exchange import service
    from copilot.features.package_insert import routes

    monkeypatch.setattr(config, "_data_dir_from_env", lambda: tmp_path)
    payload = service.export_payload(country="MX", amount_band="unknown", trade_profile="crafts",
        reported_topic="packaging", preparation_reminder="photograph_package")
    service.import_payload((json.dumps(payload) + "\n").encode("utf-8"))
    monkeypatch.setattr(config, "feature_enabled", lambda name: name == "lessons_exchange")
    assert routes._imported_reminders() == [payload]
    form = Path("copilot/web/templates/features/package_insert/form.html").read_text(encoding="utf-8")
    assert 'feature_enabled("lessons_exchange")' in form
    assert 'data-state="shared-seller-reminders"' in form
    assert 'item.country|field_word' in form and 'item.amount_band|field_word' in form
    page = Path("copilot/web/templates/features/lessons_exchange/index.html").read_text(encoding="utf-8")
    assert "field_word('topic_' ~ v)" in page
    assert "field_word('topic_' ~ item.reported_topic)" in page
    assert "item.preparation_reminder|field_word" in page


def test_tracking_topic_keeps_its_topic_label_in_both_real_template_consumers(monkeypatch):
    from copilot.web.routes_disputes import templates
    monkeypatch.setitem(templates.env.globals, "feature_enabled", lambda name: name == "lessons_exchange")

    payload = {"country": "MX", "amount_band": "unknown", "trade_profile": "crafts",
               "reported_topic": "tracking", "preparation_reminder": "save_tracking_receipt"}
    lesson_page = templates.env.get_template("features/lessons_exchange/index.html").render(
        csrf_token="csrf", error=None, profiles={}, imported=[payload], lesson_count=1, country="MX")
    package_page = templates.env.get_template("features/package_insert/form.html").render(
        capture_id="A" * 17, token="local-token", csrf_token="csrf",
        tracker={"carrier": "USPS", "tracking_number": "TRACK12345"}, profile=None,
        imported_reminders=[payload], error=None, item_description="", seller_contact="")

    assert '<option value="tracking">Tracking</option>' in lesson_page
    assert "Mexico · Unknown · Crafts · Tracking · Save tracking receipt" in lesson_page
    assert "Mexico · Unknown · Crafts · Tracking · Save tracking receipt" in package_page
