from datetime import datetime, timezone

import pytest

from copilot.features.second_opinion import service


def test_one_time_expiring_read_only_view_and_separate_note(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    snapshot = {"dispute_id": "PP-R-TST-0000001", "package_id": 3, "version": 1,
                "claims": [{"text": "Tracking was recorded.", "basis": "seller statement"}], "attachments": []}
    token, row = service.create_link(snapshot["dispute_id"], 3, snapshot, now=now)
    assert row["expires_at"] == "2026-10-26T00:00:00+00:00"
    opened = service.open_view(token, now=now)
    assert opened["snapshot"] == snapshot
    with pytest.raises(ValueError, match="already been opened"):
        service.open_view(token, now=now)
    saved = service.save_note(token, "Please add the delivery scan.", now=now)
    assert saved["note"] == "Please add the delivery scan."
    assert (tmp_path / "second_opinion" / "notes.jsonl").is_file()
    with pytest.raises(ValueError, match="already been left"):
        service.save_note(token, "Another note", now=now)


def test_expired_tokens_and_multiline_notes_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    snapshot = {"dispute_id": "PP-R-TST-0000001", "package_id": 3}
    token, _ = service.create_link(snapshot["dispute_id"], 3, snapshot, now=now)
    with pytest.raises(ValueError, match="expired"):
        service.open_view(token, now=now + service.TTL)
    token, _ = service.create_link(snapshot["dispute_id"], 3, snapshot, now=now)
    service.open_view(token, now=now)
    with pytest.raises(ValueError, match="one line"):
        service.save_note(token, "line one\nline two", now=now)


def test_package_review_integration_is_read_only():
    from pathlib import Path
    template = Path("copilot/web/templates/package_review.html").read_text(encoding="utf-8")
    entry = Path("copilot/web/templates/features/second_opinion/_entry.html").read_text(encoding="utf-8")
    assert 'feature_enabled("second_opinion")' in template
    assert "cannot edit or approve" in entry.lower() or "second-opinion" in entry
