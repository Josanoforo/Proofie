import pytest

from copilot.features.negotiation_drafts import service


def test_successive_offers_exact_contract_dedup_and_late_response(tmp_path):
    history = tmp_path / "history.jsonl"
    first = service.build(service.EXAMPLE, history)
    second = service.build(service.EXAMPLE, history)
    assert [p["amount_cents"] for p in first["proposals"]] == [2500, 3200]
    assert all(p["currency"] == "USD" and p["recipient"] == service.EXAMPLE["recipient"] for p in first["proposals"])
    assert len(first["history_versions"]) == 2 == len(second["history_versions"])
    assert len(first["history_responses"]) == 1 == len(second["history_responses"])
    assert first["responses"][0]["outcome"] == "declined"
    assert first["responses"][0]["changes_proposal"] is False
    assert "late response" in first["responses"][0]["classification"] and "newer proposal remains unchanged" in first["responses"][0]["classification"]
    assert "partial resolution" in first["proposals"][0]["draft"]
    assert len(history.read_text(encoding="utf-8").splitlines()) == 3


def test_conflicting_response_history_and_duplicate_fact_ids_fail_closed(tmp_path):
    history = tmp_path / "history.jsonl"
    service.build(service.EXAMPLE, history)
    changed = {**service.EXAMPLE, "responses": [{**service.EXAMPLE["responses"][0], "outcome": "accepted"}]}
    with pytest.raises(ValueError, match="Conflicting recorded response ID"):
        service.build(changed, history)
    facts = [dict(service.EXAMPLE["facts"][0]), dict(service.EXAMPLE["facts"][0])]
    with pytest.raises(ValueError, match="unique"):
        service.build({**service.EXAMPLE, "facts": facts})


def test_kind_templates_conditions_translation_and_allowed_action():
    data = {**service.EXAMPLE, "locale": "es", "allowed_actions": ["message"], "proposals": [{"id": "return-1", "kind": "return", "conditions": "Within 30 days"}], "responses": []}
    result = service.build(data)
    assert "devolución" in result["proposals"][0]["draft"]
    bad = {**service.EXAMPLE, "proposals": [{"id": "x", "kind": "return", "conditions": "x" * 1001}]}
    with pytest.raises(ValueError, match="never truncated"):
        service.build(bad)
    mismatch = {**service.EXAMPLE, "proposals": [{"id": "x", "kind": "partial_offer", "amount_cents": 2500, "conditions": "Return requested", "translated_draft": "Hello Buyer"}]}
    with pytest.raises(ValueError, match="Translation mismatch"):
        service.build(mismatch)


def test_translation_cannot_substitute_amount_with_substring_match():
    data = {**service.EXAMPLE, "proposals": [{"id": "offer", "kind": "partial_offer", "amount_cents": 2500, "conditions": "Only this amount"}], "responses": []}
    data["proposals"][0]["translated_draft"] = "Hello Buyer, we propose USD 125.00 as partial; condition: Only this amount."
    with pytest.raises(ValueError, match="Translation mismatch"):
        service.build(data)


def test_changed_final_template_draft_gets_a_distinct_history_fingerprint(tmp_path, monkeypatch):
    history = tmp_path / "history.jsonl"
    first = service.build(service.EXAMPLE, history)
    original = service.TEMPLATES["en"]["partial_offer"]
    monkeypatch.setitem(service.TEMPLATES["en"], "partial_offer", original + " Updated local wording.")
    second = service.build(service.EXAMPLE, history)
    assert first["proposals"][0]["draft"] != second["proposals"][0]["draft"]
    assert first["proposals"][0]["logical_id"] != second["proposals"][0]["logical_id"]
    assert len(first["history_versions"]) == 2 and len(second["history_versions"]) == 4


@pytest.mark.parametrize("changes", [{"locale": "fr"}, {"allowed_actions": ["message"]}, {"proposals": [{"id": "x", "kind": "partial_offer", "amount_cents": 2.5}]}, {"recipient": " Buyer (recorded case contact)"}])
def test_unsafe_or_unavailable_draft_refused(changes):
    with pytest.raises(ValueError):
        service.build({**service.EXAMPLE, **changes})


@pytest.mark.parametrize("fact", [{"id": "f", "source": "record"}, {"id": "f", "text": "fact", "source": "record", "verified": True}, {"id": "f", "text": " ", "source": "record"}])
def test_fact_schema_requires_exact_nonempty_fields(fact):
    with pytest.raises(ValueError, match="fact"):
        service.build({**service.EXAMPLE, "facts": [fact]})


def test_corrupt_history_fails_closed(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text("{bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        service.build(service.EXAMPLE, path)
