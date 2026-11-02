from datetime import datetime, timedelta, timezone
import ast
import json

import pytest

from copilot.features.buyer_page import service


NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
CASE = "PP-R-TST-0000001"
VIEW = {"description": "Blue desk lamp", "tracking_status": "In transit",
        "expected_delivery": "2026-10-30", "locale": "en", "photo_data_uri": None}


def test_invalid_expired_and_reused_tokens_are_refused(tmp_path):
    db = tmp_path / "buyer.db"
    with pytest.raises(ValueError):
        service.read_link("bad", now=NOW, db_path=db)
    token = service.create_link(CASE, VIEW, now=NOW, db_path=db)
    expired = service.read_link(token, now=NOW + service.TTL, db_path=db)
    assert expired["state"] == "expired"
    assert expired["locale"] == "en"
    assert service.consume_link(token, "delivery_confirmed", now=NOW + service.TTL, db_path=db)["state"] == "expired"
    assert service.read_link(token, now=NOW, db_path=db)["state"] == "ready"
    assert service.consume_link(token, "delivery_confirmed", now=NOW, db_path=db)["state"] == "recorded"
    assert service.read_link(token, now=NOW, db_path=db)["state"] == "used"
    assert service.consume_link(token, "talk_requested", now=NOW, db_path=db)["state"] == "used"


def test_spanish_response_and_expiration_keep_buyer_language(tmp_path):
    spanish_view = {**VIEW, "locale": "es"}
    token = service.create_link(CASE, spanish_view, now=NOW, db_path=tmp_path / "buyer.db")
    assert service.read_link(token, now=NOW + service.TTL, db_path=tmp_path / "buyer.db")["locale"] == "es"
    response = service.consume_link(token, "talk_requested", now=NOW, db_path=tmp_path / "buyer.db")
    assert response["state"] == "recorded"
    assert response["locale"] == "es"


def test_both_buyer_actions_write_to_existing_audit_ledger(tmp_path):
    db = tmp_path / "buyer.db"
    token1 = service.create_link(CASE, VIEW, now=NOW, db_path=db)
    token2 = service.create_link(CASE, VIEW, now=NOW, db_path=db)
    assert service.consume_link(token1, "delivery_confirmed", now=NOW, db_path=db)["state"] == "recorded"
    assert service.consume_link(token2, "talk_requested", now=NOW, db_path=db)["state"] == "recorded"
    events = service.buyer_events(db, CASE)
    assert [event["action"] for event in events] == ["delivery_confirmed", "talk_requested"]
    assert all(event["recorded_at"] for event in events)


def test_public_view_allows_only_redacted_buyer_fields(tmp_path):
    db = tmp_path / "buyer.db"
    with pytest.raises(ValueError):
        service.create_link(CASE, {**VIEW, "buyer_email": "buyer@example.invalid"}, now=NOW, db_path=db)
    with pytest.raises(ValueError):
        service.create_link(CASE, {**VIEW, "dispute_id": CASE}, now=NOW, db_path=db)
    token = service.create_link(CASE, VIEW, now=NOW, db_path=db)
    public = service.read_link(token, now=NOW, db_path=db)["view"]
    assert set(public) == {"description", "tracking_status", "expected_delivery", "locale", "photo_data_uri"}
    assert CASE not in json.dumps(public)
    assert "@" not in json.dumps(public)


def test_photo_accepts_only_bounded_jpeg_or_png():
    class Upload:
        filename = "packing.jpg"
        content_type = "image/jpeg"
        class File:
            def read(self, size):
                return b"\xff\xd8\xff\xe0valid-jpeg-payload"
        file = File()

    photo = service.image_data_uri(Upload())
    assert photo.startswith("data:image/jpeg;base64,")
    assert service.image_data_uri(None) is None

    class BadUpload(Upload):
        content_type = "image/svg+xml"

    with pytest.raises(ValueError):
        service.image_data_uri(BadUpload())

    class MislabeledUpload(Upload):
        class File:
            def read(self, size):
                return b"not really an image"
        file = File()

    with pytest.raises(ValueError):
        service.image_data_uri(MislabeledUpload())


def test_buyer_page_code_has_no_network_client_imports():
    from pathlib import Path
    for path in Path("copilot/features/buyer_page").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {alias.name.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        assert not names.intersection({"httpx", "requests", "urllib", "socket"})


def test_buyer_page_integration_is_retired_in_favor_of_buyer_note():
    from pathlib import Path
    source = Path("copilot/app.py").read_text(encoding="utf-8")
    assert 'feature_enabled("buyer_page")' not in source
    assert "from copilot.features.buyer_page.routes" not in source
    at = source.index('feature_enabled("buyer_note")')
    assert source.index("from copilot.features.buyer_note.routes", at) > at
    template = "".join(p.read_text(encoding="utf-8") for p in [Path("copilot/web/templates/dispute_detail.html"), Path("copilot/web/templates/case_task.html"), *sorted(Path("copilot/web/templates/case").glob("*.html"))])   # v3: the case page is split into task views
    assert 'feature_enabled("buyer_note")' in template
    assert "features/buyer_note/_seller_entry.html" in template
    assert "features/buyer_page/_seller_entry.html" not in template


def test_retired_buyer_page_explains_loopback_limit_in_historical_entry():
    from pathlib import Path
    entry = Path("copilot/web/templates/features/buyer_page/_seller_entry.html").read_text(encoding="utf-8")
    assert "only after a dispute exists" in entry
    assert "127.0.0.1" in entry and "localhost" in entry
    assert "Hosted pages contain fictitious sample data only" in entry
