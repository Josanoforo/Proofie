from pathlib import Path

import pytest

from conftest import is_private_checkout
from copilot.features.third_party_statement import service, store


def test_sources_and_text_are_bounded_and_labeled_unverified():
    assert service.validate_source("  Buyer ") == "buyer"
    assert service.validate_source("Reception") == "reception"
    for source in ("seller", "", "unknown"):
        with pytest.raises(ValueError):
            service.validate_source(source)
    assert service.validate_text("  Package left with neighbor.  ") == "Package left with neighbor."
    with pytest.raises(ValueError):
        service.validate_text(" " )
    with pytest.raises(ValueError):
        service.validate_text("x" * (service.MAX_TEXT_CHARS + 1))
    assert "neighbor" in service.uncorroborated_label("neighbor", "2026-10-25T10:00:00+00:00")
    assert "uncorroborated" in service.uncorroborated_label("neighbor", "2026-10-25T10:00:00+00:00")


def test_photo_requires_real_jpeg_or_png_signature_and_size():
    png = b"\x89PNG\r\n\x1a\n" + b"synthetic"
    assert service.validate_image(png) == "image/png"
    with pytest.raises(ValueError):
        service.validate_image(b"not an image")
    with pytest.raises(ValueError):
        service.validate_image(png + b"x" * service.MAX_IMAGE_BYTES)


def test_drawer_is_case_scoped_timestamped_and_not_the_case_ledger(tmp_path):
    db = tmp_path / "third-party.sqlite3"
    first = store.add(db, "PP-R-DEMO-0000001", "packer", "statement", "text/plain; charset=utf-8", None, b"Packed at 4 pm")
    rows = store.list_for_case(db, "PP-R-DEMO-0000001")
    assert [row["id"] for row in rows] == [first]
    assert rows[0]["source_party"] == "packer"
    assert rows[0]["payload"] == b"Packed at 4 pm"
    assert rows[0]["recorded_at"] and rows[0]["uncorroborated"] is True
    assert store.list_for_case(db, "PP-R-DEMO-0000002") == []
    assert Path(db).is_file()


def test_actual_flagged_drawer_keeps_text_attribution_and_photo_transfer_seller_only(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from fastapi.testclient import TestClient

    monkeypatch.setenv("COPILOT_FEATURE_THIRD_PARTY_STATEMENT", "true")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    config.set_db_path_override(tmp_path / "case.db")
    config.set_uploads_dir_override(tmp_path / "uploads")
    client = TestClient(create_app(), client=("127.0.0.1", 12345), follow_redirects=False)
    page = client.get("/disputes/PP-R-DEMO-0000001/third-party-statements")
    assert page.status_code == 200
    token = client.cookies.get("csrf_token")
    saved_text = client.post(page.request.url.path, data={
        "csrf_token": token, "source_party": "neighbor", "kind": "statement", "statement": "Neighbor accepted it."})
    assert saved_text.status_code == 303
    reread = client.get(saved_text.headers["location"])
    assert "Neighbor · third-party statement, uncorroborated" in reread.text
    assert "Neighbor accepted it." in reread.text
    token = client.cookies.get("csrf_token")
    saved_photo = client.post(page.request.url.path, data={
        "csrf_token": token, "source_party": "reception", "kind": "photo"},
        files={"file": ("door.png", b"\x89PNG\r\n\x1a\nimage", "image/png")})
    assert saved_photo.status_code == 303
    assert store.list_for_case(tmp_path / "data" / "third_party_statements.sqlite3", "PP-R-DEMO-0000001")[1]["source_party"] == "reception"
    transfer = client.post("/disputes/PP-R-DEMO-0000001/third-party-statements/2/case-document",
                           data={"csrf_token": token})
    assert transfer.status_code == 303
    documents = repo.list_documents(config.db_path(), "PP-R-DEMO-0000001")
    assert len(documents) == 1
    assert documents[0]["excluded"] is True
    audit = repo.list_audit(config.db_path(), "PP-R-DEMO-0000001")
    assert any(row["action"] == "third_party_statement_copied_to_case_document" for row in audit)
    assert not any("buyer_contribution" in row["action"] for row in audit)


def test_old_buyer_only_runtime_surface_is_removed_with_p19_history_retained():
    detail = Path("copilot/web/templates/case_task.html").read_text(encoding="utf-8")
    app = Path("copilot/app.py").read_text(encoding="utf-8")
    env = Path(".env.example").read_text(encoding="utf-8")
    historical = Path("internal/lanes/nivel2/P19-BUYER-CONTRIBUTION-2026-10-25.md")
    assert 'feature_enabled("third_party_statement")' in detail
    assert 'feature_enabled("buyer_contribution")' not in detail
    assert 'feature_enabled("buyer_contribution")' not in app
    assert "COPILOT_FEATURE_BUYER_CONTRIBUTION" not in env
    assert "COPILOT_FEATURE_THIRD_PARTY_STATEMENT" in env
    if is_private_checkout(Path(".")):   # the history note lives under internal/, which the public kit does not carry
        assert historical.is_file()
