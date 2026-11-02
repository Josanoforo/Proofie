import pytest


def test_observation_boundaries_and_restart(monkeypatch, tmp_path):
    from copilot import config
    from copilot.features.local_autopilot.observations import ObservationStore, MAX_CASES
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    now=[10]; store=ObservationStore(clock=lambda:now[0]); did="PP-R-DEMO-0000001"
    norm={"dispute_id":did,"status":"WAITING_FOR_SELLER_RESPONSE","reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED","raw_private":"never retain"}
    token=store.issue(config.db_path(),[{"norm":norm,"actions":["message"]}],observed_at="2026-10-31T12:00:00+00:00",provenance="synthetic_fixture")
    record=store.get(token,did)
    assert record["case_id"] == did and "raw_private" not in record["norm"]
    record["norm"]["status"]="RESOLVED"
    assert store.get(token,did)["norm"]["status"] == "WAITING_FOR_SELLER_RESPONSE"
    assert store.get(token,"PP-R-DEMO-0000002") is None
    assert ObservationStore().get(token,did) is None
    with pytest.raises(ValueError): store.issue(config.db_path(),[{"norm":norm} for _ in range(MAX_CASES+1)],observed_at="2026-10-31T12:00:00+00:00",provenance="synthetic_fixture")
    now[0]+=300
    assert store.get(token,did) is None
