import json
import uuid

import pytest

from copilot.features.local_autopilot import service


def _enable(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_AUTOPILOT", "true")


def _store():
    folder = service.config.ROOT / "data" / "mejoras10" / "09" / ("test-" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    return folder / "audit.jsonl", folder


def test_simulation_flag_off_and_three_profiles(monkeypatch):
    monkeypatch.delenv("COPILOT_FEATURE_LOCAL_AUTOPILOT", raising=False)
    with pytest.raises(PermissionError): service.simulate(service.EXAMPLES[0])
    _enable(monkeypatch)
    assert service.simulate(service.EXAMPLES[0])["decision"] == "simulated_action"
    assert service.simulate(service.EXAMPLES[1])["decision"] == "simulated_action"
    assert service.simulate(service.EXAMPLES[2])["decision"] == "exception"


def test_durable_dedup_restart_state_change_and_policy_change(monkeypatch):
    _enable(monkeypatch)
    path, folder = _store()
    try:
        first = service.simulate(service.EXAMPLES[1], path)
        restarted = service.simulate(service.EXAMPLES[1], path)
        assert first == restarted and "NOT SENT" in first["result"]
        state_changed = {**service.EXAMPLES[1], "state_version": "snapshot-2"}
        assert service.simulate(state_changed, path)["decision"] == "exception"
        policy_changed = {**service.EXAMPLES[1], "policy": {"actions": ["message"], "max_cents": 15000, "min_evidence": 1}}
        assert "action not allowed by merchant policy" in service.simulate(policy_changed, path)["flags"]
        assert len(path.read_text(encoding="utf-8").splitlines()) == 3
        inbox = service.load_inbox(path)
        assert len(inbox) == 3 and inbox[-1]["decision"] == "exception"
    finally:
        path.unlink(missing_ok=True); path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True); folder.rmdir()


def test_deadline_current_state_evidence_limits_and_corrupt_store_fail_closed(monkeypatch):
    _enable(monkeypatch)
    base = service.EXAMPLES[1]
    for changes in ({"deadline": "2026-10-20T00:00:00-06:00"}, {"case_state": "RESOLVED"}, {"amount_cents": 999999}, {"evidence": []}):
        result = service.simulate({**base, **changes})
        assert result["decision"] == "exception" and result["flags"]
    with pytest.raises(ValueError, match="actual content"):
        service.simulate({**base, "evidence": [{"count": 10}]})
    path, folder = _store()
    path.write_text("corrupt\n", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="corrupt"):
            service.simulate(base, path)
    finally:
        path.unlink(missing_ok=True); path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True); folder.rmdir()


def test_success_then_conflict_and_snapshot_action_removal_refuse_without_reusing_success(monkeypatch):
    _enable(monkeypatch)
    base = service.EXAMPLES[1]
    path, folder = _store()
    try:
        first = service.simulate(base, path)
        assert first["decision"] == "simulated_action"
        conflicted = service.simulate({**base, "conflict": True}, path)
        assert conflicted["decision"] == "exception" and "conflict" in " ".join(conflicted["flags"])
        assert service.simulate({**base, "conflict": True}, path) == conflicted
        removed = service.simulate({**base, "recorded_allowed_actions": ["message"]}, path)
        assert removed["decision"] == "exception" and "action not allowed" in " ".join(removed["flags"])
        assert len(service.load_inbox(path)) == 3
    finally:
        path.unlink(missing_ok=True); path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True); folder.rmdir()


@pytest.mark.parametrize("field,value", [("max_cents", -1), ("min_evidence", -1)])
def test_negative_policy_thresholds_are_rejected(monkeypatch, field, value):
    _enable(monkeypatch)
    base = service.EXAMPLES[1]
    with pytest.raises(ValueError, match="Policy schema"):
        service.simulate({**base, "policy": {**service.PROFILES["standard"], field: value}})


def test_store_works_when_msvcrt_and_fcntl_are_both_unavailable(monkeypatch):
    """S2: no platform-only import may escape; the lock falls back to a process-local one."""
    import sys
    _enable(monkeypatch)
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    path, folder = _store()
    first = service.simulate(service.EXAMPLES[0], path)
    again = service.simulate(service.EXAMPLES[0], path)
    assert first["decision"] == "simulated_action" and again["logical_id"] == first["logical_id"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert "the dispute snapshot was read with a GET" in first["result"]


def test_disputed_amount_boundaries_and_unknowns(monkeypatch):
    _enable(monkeypatch)
    base = service.EXAMPLES[1]
    assert service.evaluate({**base, "dispute_amount_cents": 2500})["decision"] == "simulated_action"
    assert "amount exceeds disputed amount" in service.evaluate({**base, "dispute_amount_cents": 2499})["flags"]
    for changes in ({"dispute_amount_cents": None}, {"dispute_amount_cents": "2500"},
                    {"dispute_amount_cents": True}, {"dispute_amount_cents": -1},
                    {"dispute_currency": "MXN"}, {"dispute_currency": None}):
        assert service.evaluate({**base, **changes})["decision"] == "exception"
