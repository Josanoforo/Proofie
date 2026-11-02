from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_explicit_router_flags_csrf_and_case_bound_token(monkeypatch,tmp_path):
    from copilot import config
    from copilot.features.local_autopilot import observations
    from copilot.features.local_autopilot.preparation_routes import router
    monkeypatch.setattr(config,"_db_path_override",tmp_path / "db.sqlite")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO","false")
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_AUTOPILOT","true")
    monkeypatch.delenv("COPILOT_FEATURE_LOCAL_PREPARATION",raising=False)
    did="PP-R-DEMO-0000001"; foreign="PP-R-DEMO-0000002"
    norm={"dispute_id":did,"status":"WAITING_FOR_SELLER_RESPONSE","reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}
    token=observations.issue(config.db_path(),[{"norm":norm}],observed_at="2026-10-31T12:00:00+00:00",provenance="synthetic_fixture")
    app=FastAPI(); app.include_router(router); client=TestClient(app,base_url="http://127.0.0.1")
    client.cookies.set("csrf_token","safe-token")
    def post(case,csrf): return client.post(f"/disputes/{case}/local-preparation",data={"csrf_token":csrf,"observation_token":token},follow_redirects=False)
    assert post(did,"safe-token").status_code == 404
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_PREPARATION","true")
    assert post(did,"bad").status_code == 403
    wrong=post(foreign,"safe-token"); assert wrong.status_code == 303 and "local_preparation=unavailable" in wrong.headers["location"]
    right=post(did,"safe-token"); assert right.status_code == 303 and "local_preparation=needs_input" in right.headers["location"]
