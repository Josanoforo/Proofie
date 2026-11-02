import json
from datetime import datetime, timedelta, timezone


def test_history_and_unknown_handoff(monkeypatch, tmp_path):
    from copilot import config
    from copilot.features.local_autopilot import routes, service
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_AUTOPILOT", "true")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(routes, "_evidence", lambda _: [{"id": "ev-1", "source": "synthetic fixture", "content": "buyer order and shipping facts"}])
    now = datetime.now(timezone.utc)
    due = (now + timedelta(days=1)).isoformat()
    data = {**service.EXAMPLES[1], "observed_at": now.isoformat(), "deadline": due}
    path = routes._store_path()
    path.parent.mkdir(parents=True)
    legacy = service.evaluate(data)
    legacy.pop("snapshot_version")
    legacy["logical_id"] = "a" * 64
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    norm = {"currency": "USD", "amount_cents": 2500, "status": data["case_state"], "update_time": data["state_version"], "seller_response_due_date": due, "deadline_parse_ok": True}
    assert routes.handoff_for_case(data["case_id"], legacy["logical_id"], norm, data["recorded_allowed_actions"]) is None
    fresh = service.simulate(data, path)
    assert fresh["decision"] == "simulated_action"
    assert routes.handoff_for_case(data["case_id"], fresh["logical_id"], norm, data["recorded_allowed_actions"])
    for amount in (None, "bad", True, -1):
        assert routes.handoff_for_case(data["case_id"], fresh["logical_id"], {**norm, "amount_cents": amount}, data["recorded_allowed_actions"]) is None
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0]) == legacy
