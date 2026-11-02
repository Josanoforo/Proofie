from datetime import datetime, timezone
from copilot.features.local_autopilot.assistance import case_context


def test_preparation_context_version_and_invalid_model(monkeypatch, tmp_path):
    from copilot import config
    from copilot.db import repo
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "case.sqlite")
    db = config.db_path(); repo.ensure_schema(db)
    case = {"dispute_id": "PP-R-DEMO-0000001", "status": "WAITING_FOR_SELLER_RESPONSE", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "update_time": "v1", "deadline_parse_ok": True, "seller_response_due_date": "2099-01-01T00:00:00Z", "amount_cents": 1501, "currency": "USD"}
    repo.insert_document(db, case["dispute_id"], "seller.txt", "text/plain", 4, "a" * 64, "source record", True)
    initial = case_context(db, case, ["message", "offer"], model_output={"facts": [{"text": "invented", "source": ""}]})
    assert initial["recommendation"] == "message" and len(initial["facts"]) == 1
    assert "invented" not in str(initial) and initial["dispute_amount_cents"] == 1501
    token = repo.claim_auto_prep(db, case["dispute_id"], initial["version"])
    assert token and repo.claim_auto_prep(db, case["dispute_id"], initial["version"]) is None
    assert repo.set_auto_prep_result(db, case["dispute_id"], initial["version"], token, "failed", detail="synthetic failure")
    failed = case_context(db, case, ["message"])
    assert failed["preparation"] == "preparation failed; continue manually"
    changed = case_context(db, {**case, "update_time": "v2"}, ["message"])
    assert "changed" in changed["preparation"] and changed["version"] != initial["version"] and changed["recommendation"] is None
    no_deadline = case_context(db, {**case, "deadline_parse_ok": False}, ["message"])
    assert no_deadline["recommendation"] is None
    no_action = case_context(db, case, [])
    assert no_action["recommendation"] is None


def test_read_failure_and_missing_package_fail_safely(monkeypatch, tmp_path):
    import sqlite3
    from copilot.features.local_autopilot import assistance
    from copilot.db import repo
    norm = {"dispute_id": "PP-R-DEMO-0000001"}
    monkeypatch.setattr(assistance.auto_prep, "compute_version_hash", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("temporary failure")))
    result = assistance.case_context(tmp_path / "db", norm, ["offer"])
    assert result["preparation"] == "unavailable" and result["recommendation"] is None
    monkeypatch.setattr(assistance.auto_prep, "compute_version_hash", lambda *a: "v1")
    monkeypatch.setattr(assistance.auto_prep, "should_prepare", lambda *a: True)
    monkeypatch.setattr(repo, "get_auto_prep", lambda *a: {"version_hash": "v1", "status": "ready", "package_id": 4})
    monkeypatch.setattr(repo, "get_package", lambda *a: {"id": 4, "dispute_id": "OTHER"})
    monkeypatch.setattr(repo, "list_documents", lambda *a: [])
    result = assistance.case_context(tmp_path / "db", norm, [])
    assert result["preparation"] == "prepared package unavailable; continue manually"
    assert result.get("package_id") is None and result["recommendation"] is None
