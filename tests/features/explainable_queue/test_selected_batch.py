import time
import pytest
from copilot.features.explainable_queue.batch import InvalidSelection, run_selected


def test_mixed_selection_is_sequential_and_isolated():
    calls = []
    def get(token, cid):
        return {"case_id": cid, "version": "v", "observed_at": "2026-10-31T12:00:00Z", "provenance": "synthetic_fixture"} if token == "held" and cid in ("a", "b", "c") else None
    def prepare(db, obs, *, expected_version):
        calls.append(obs["case_id"])
        assert expected_version == "v"
        if obs["case_id"] == "b": raise RuntimeError("synthetic failure")
        return {"case_id": obs["case_id"], "state": "ready" if obs["case_id"] == "a" else "needs_input", "package_id": 1 if obs["case_id"] == "a" else None, "reasons": []}
    t = time.perf_counter()
    result = run_selected(None, "held", ["a", "b", "c"], get_observation=get, prepare=prepare)
    print(f"mixed_fixture_elapsed_seconds={time.perf_counter()-t:.6f}")
    assert calls == ["a", "b", "c"]
    assert [x["state"] for x in result["results"]] == ["ready", "error", "needs_input"]
    assert [x["case_id"] for x in result["results"] if x["retryable"]] == ["b"]
    assert result["selected"] == calls and result["unprocessed"] == []


def test_invalid_selection_fails_before_any_work():
    calls = []
    def get(token, cid): return {"case_id": cid, "version": "v"} if token == "valid" and cid == "a" else None
    def prepare(*args, **kwargs): calls.append(1)
    for token, ids in (("valid", []), ("valid", ["a", "a"]), ("valid", ["a", "b", "c", "d"]), ("expired", ["a"]), ("valid", ["a", "foreign"])):
        with pytest.raises(InvalidSelection): run_selected(None, token, ids, get_observation=get, prepare=prepare)
    assert calls == []


def test_mismatched_case_result_never_navigates_foreign_package():
    result = run_selected(None, "valid", ["a"], get_observation=lambda token, cid: {"case_id": cid},
                          prepare=lambda *args, **kwargs: {"case_id": "other", "state": "ready", "package_id": 9})
    assert result["results"][0]["state"] == "error" and "package_id" not in result["results"][0]


def test_selected_route_uses_real_observation_handoff_without_transport(monkeypatch, tmp_path):
    import os, re
    from fastapi.testclient import TestClient
    from copilot import config
    from copilot.app import create_app
    from copilot.features.local_autopilot import observations
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(name, raising=False)
    for flag in ("EXPLAINABLE_QUEUE", "LOCAL_AUTOPILOT", "LOCAL_PREPARATION", "SELECTED_PREPARATION"):
        monkeypatch.setenv("COPILOT_FEATURE_" + flag, "true")
    ids = ["PP-R-O4-0000001", "PP-R-O4-0000002"]
    cases = [{"norm": {"dispute_id": cid, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "status": "WAITING_FOR_SELLER_RESPONSE", "update_time": "2026-10-30T12:00:00Z"}, "actions": []} for cid in ids]
    token = observations.issue(config.db_path(), cases, observed_at="2026-10-31T12:00:00Z", provenance="synthetic_fixture")
    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    form = client.get("/features/explainable_queue")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    url = "/features/explainable_queue/prepare-selected"
    rejected = client.post(url, data={"csrf_token": "bad", "observation_token": token, "case_ids": ids[0]})
    assert rejected.status_code == 403
    result = client.post(url, data={"csrf_token": csrf, "observation_token": token, "case_ids": ids})
    assert result.status_code == 200 and result.text.count('data-state="needs_input"') == 2
    assert "synthetic_fixture" in result.text and "Nothing was approved or sent" in result.text
    assert "Retry selected pending cases" not in result.text
    # New token from the results page; foreign or expired observations must not execute.
    csrf = re.search(r'csrf_token=([^;]+)', result.headers.get('set-cookie', '')).group(1)
    rejected = client.post(url, data={"csrf_token": csrf, "observation_token": "expired", "case_ids": ids[0]})
    assert rejected.status_code == 400 and "Reload the inbox" in rejected.text


def test_disabled_flags_refuse_batch(monkeypatch, tmp_path):
    import os
    from fastapi.testclient import TestClient
    from copilot import config
    from copilot.app import create_app
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")
    web = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    assert web.post("/features/explainable_queue/prepare-selected", data={}).status_code == 404


def test_batch_fragment_discloses_truncated_scope_and_selection_limit():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("copilot/web/templates"), autoescape=True)
    template = env.get_template("features/explainable_queue/_batch.html")
    base = {"selected_preparation_token": "opaque", "selected_preparation_csrf": "csrf",
            "selected_preparation_cases": [{"case_id": f"case-{n}"} for n in range(50)]}
    truncated = template.render(**base, selected_preparation_scope={"shown": 50, "total": 72})
    assert "first 50 of 72" in truncated and "up to three cases" in truncated
    assert truncated.count('name="case_ids"') == 50
    assert 'name="selected_preparation_scope"' not in truncated and 'name="norm"' not in truncated
    complete = template.render(**{**base, "selected_preparation_cases": base["selected_preparation_cases"][:3]},
                               selected_preparation_scope={"shown": 3, "total": 3})
    assert 'data-state="selected-preparation-scope"' not in complete and "up to three cases" in complete
