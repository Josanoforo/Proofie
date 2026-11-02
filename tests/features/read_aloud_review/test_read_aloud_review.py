from copilot.features.read_aloud_review.service import outgoing_notes
from copilot.core.send import build_request_input


def test_reads_exact_notes_from_existing_send_builder():
    claims = [{"text": " Parcel was shipped ", "basis": "seller"}, {"text": "Tracking was recorded.", "basis": "seller"}]
    expected = build_request_input(claims, "TRACK123", "UPS")["evidences"][0]["notes"]
    assert outgoing_notes(claims, "TRACK123", "UPS") == expected


def test_package_review_requires_local_matching_browser_voice_and_click():
    from pathlib import Path
    template = Path("copilot/web/templates/features/read_aloud_review/_entry.html").read_text(encoding="utf-8")
    assert "localService === true" in template
    assert "getVoices()" in template
    assert "No verified local " in template
    assert "addEventListener(\"click\"" in template


def test_include_is_only_on_unsent_sendable_package_review():
    from pathlib import Path
    page = Path("copilot/web/templates/package_review.html").read_text(encoding="utf-8")
    assert 'feature_enabled("read_aloud_review") and not sent_version' in page
    assert 'features/read_aloud_review/_entry.html' in page
