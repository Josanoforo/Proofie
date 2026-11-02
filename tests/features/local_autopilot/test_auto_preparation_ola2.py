import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone


def test_real_inbox_preparation_repeat_changed_and_fallback(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.core import auto_prep
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.features.local_autopilot.assistance import case_context
    from copilot.web.routes_disputes import get_client
    from _hosted import authenticated_client
    import httpx
    def blocked(*args, **kwargs):
        raise AssertionError("external transport forbidden")
    def failed_model(*args, **kwargs):
        raise RuntimeError("synthetic model unavailable")
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "case.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_LOCAL_AUTOPILOT", "true")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(auto_prep, "propose_package", failed_model)
    monkeypatch.setattr(auto_prep, "claims_for_dispute_images", lambda *a, **kw: [])
    did = "PP-R-DEMO-0000001"
    raw = json.loads(Path("pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
    raw["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    class Fake:
        calls = []
        def call(self, method, path, operation, **kwargs):
            assert method == "GET"
            self.calls.append(operation)
            if operation == "list_disputes": return {"items": [raw], "links": []}
            if operation in {"show_dispute", "resolution_read"}: return raw
            if operation == "context_transaction_search": return {"transaction_details": []}
            if operation == "context_tracking": return {"trackers": []}
            raise AssertionError(operation)
    fake = Fake()
    data = b"Synthetic carrier proof text"
    sha = hashlib.sha256(data).hexdigest()
    repo.insert_document(config.db_path(), did, "synthetic.pdf", "application/pdf", len(data), sha, data.decode(), True)
    write_raw(config.uploads_dir(), sha, data)
    app = create_app(); app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    assert client.get("/disputes").status_code == 200
    prep = repo.get_auto_prep(config.db_path(), did)
    assert prep["status"] == "ready"
    package = repo.get_package(config.db_path(), prep["package_id"])
    assert package["source"] == "auto_manual" and not package["validation_ok"]
    assert auto_prep.AI_UNAVAILABLE_FLAG in package["review_flags"]
    assert repo.list_claims(config.db_path(), package["id"]) == []
    from copilot.core.disputes import normalize
    norm = normalize(raw, datetime.now(timezone.utc))
    view = case_context(config.db_path(), norm, ["message"])
    assert view["package_id"] == package["id"] and view["preparation"] == "ready for human review"
    page = client.get(f"/disputes/{did}/packages/{package['id']}")
    assert page.status_code == 200 and "Draft prepared automatically" in page.text
    assert client.get("/disputes").status_code == 200
    assert len(repo.list_packages(config.db_path(), did)) == 1
    raw["update_time"] = "2026-10-30T20:01:00Z"
    stale = case_context(config.db_path(), normalize(raw, datetime.now(timezone.utc)), ["message"])
    assert stale["recommendation"] is None and not stale.get("package_id")
    assert client.get("/disputes").status_code == 200
    packages = repo.list_packages(config.db_path(), did)
    assert len(packages) == 2 and repo.get_auto_prep(config.db_path(), did)["package_id"] != package["id"]
    assert all(repo.list_claims(config.db_path(), p["id"]) == [] for p in packages)
    assert all(repo.get_latest_valid_approval(config.db_path(), p["id"]) is None for p in packages)
    assert fake.calls
    # An unexpected upstream-context failure is persisted safely by the actual background wrapper.
    raw["update_time"] = "2026-10-30T20:02:00Z"
    monkeypatch.setattr(auto_prep, "fetch_context", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic context failure")))
    assert client.get("/disputes").status_code == 200
    failed = repo.get_auto_prep(config.db_path(), did)
    assert failed["status"] == "failed" and failed["package_id"] is None
    assert len(repo.list_packages(config.db_path(), did)) == 2
