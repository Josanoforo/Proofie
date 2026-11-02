import pytest

from copilot.features.appeal_workspace import service


def test_recorded_action_source_status_and_content_delta():
    out = service.build(service.EXAMPLE)
    assert out["appeal_preview_allowed"] is True
    assert out["eligibility_asserted"] is False
    assert out["recorded_appeal_action_sources"][0]["id"] == "action-log-9"
    assert len(out["recorded_appeal_action_sources"][0]["sha256"]) == 64
    assert out["proposed_evidence"][0]["delta"] == "new content"
    assert out["original_snapshot_sha256"]
    assert out["original_frozen"][0]["sha256"] != out["proposed_evidence"][0]["sha256"]


def test_identical_content_under_renamed_id_is_not_new():
    data = {**service.EXAMPLE, "new": [{"id": "renamed", "name": "copy", "content": "original bytes", "source_id": "annex-new"}]}
    row = service.build(data)["proposed_evidence"][0]
    assert row["delta"] == "unchanged content" and row["same_content_as"] == "proof"


@pytest.mark.parametrize("change", [{"recorded_actions": []}, {"recorded_actions": [{"action": "APPEAL", "status": "available"}]}, {"action_sources": []}, {"original_version": None}, {"outcome": {"status": "UNKNOWN"}}])
def test_missing_action_provenance_or_outcome_withholds_preview(change):
    assert service.build({**service.EXAMPLE, **change})["appeal_preview_allowed"] is False


def test_contentless_or_duplicate_evidence_is_rejected():
    for rows in ([{"id": "x", "name": "n", "content": "", "source_id": "s"}], [{"id": "x", "name": "n", "content": "a", "source_id": "s"}, {"id": "x", "name": "n", "content": "b", "source_id": "s"}]):
        with pytest.raises(ValueError):
            service.build({**service.EXAMPLE, "new": rows})
