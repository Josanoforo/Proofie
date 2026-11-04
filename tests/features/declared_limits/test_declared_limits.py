from copilot.features.declared_limits.service import LIMITS, flags_snapshot, rc44_snapshot


def test_limits_preserve_human_review_and_withhold_accuracy_claims():
    text = " ".join(LIMITS).lower()
    assert "human approval" in text
    assert "wrong" in text
    assert "release status" in text


def test_flag_snapshot_reads_runtime_accessor_only():
    seen = []
    values = flags_snapshot(lambda name: seen.append(name) or name == "webhooks")
    assert values["webhooks"] is True and values["webhooks_verify"] is False
    assert set(seen) == set(values)


def test_rc44_snapshot_checks_only_public_fact_flags_from_checked_in_source():
    from pathlib import Path
    facts = Path(__file__).resolve().parents[3] / "internal" / "release" / "RC44-FACTS-FOR-TABLES.md"
    snapshot = rc44_snapshot(facts)
    if not facts.is_file():   # internal/ is not part of the public kit: the service must say so and claim nothing
        assert snapshot == {"source_available": False, "public_figures_gated": None, "accuracy_claim_authorized": None,
                            "measured_release_status_for_this_branch": "not established; the checked-in facts source is unavailable"}
        return
    assert snapshot["source_available"] is True
    assert snapshot["public_figures_gated"] is True
    assert snapshot["accuracy_claim_authorized"] is False
    assert snapshot["measured_release_status_for_this_branch"] == "not established by the checked-in facts"
    assert "Measured code" not in str(snapshot)


def test_the_declared_limits_page_names_no_release_id(monkeypatch):
    """HERO filmable text: the limits say what they are, without the release id."""
    import re
    from fastapi.testclient import TestClient
    from copilot.app import create_app
    monkeypatch.setenv("COPILOT_FEATURE_DECLARED_LIMITS", "true")
    page = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)).get("/declared-limits")
    assert page.status_code == 200 and not re.search(r"RC4|D-[0-9]{2,3}\b", re.sub(r"<[^>]+>", " ", page.text))
