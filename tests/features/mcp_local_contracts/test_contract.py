import json
import os
import uuid

import pytest

from copilot.features.mcp_local_contracts import service


def test_actor_scoped_registry_strict_schema_and_sanitized_audit(tmp_path):
    from copilot.db import repo
    from copilot.features.mcp_local_contracts import service
    work = service.ROOT / "data" / "mejoras10" / "06" / ("acceptance-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    registry, audit = work / "registry.json", work / "audit.jsonl"
    db = work / "proofie.sqlite3"
    case_a, case_b = "PP-R-MCP-0000001", "PP-R-MCP-0000002"
    repo.insert_document(db, case_a, "a.txt", "text/plain", 1, "a" * 64, "a", True)
    repo.insert_document(db, case_b, "b.txt", "text/plain", 1, "b" * 64, "b", True)
    try:
        registry.write_text(json.dumps({"actor_cases": {"alice": [case_a], "bob": [case_b]}}), encoding="utf-8")
        assert [x["case_id"] for x in service.invoke("list_local_cases", {}, "alice", registry_path=registry, audit_path=audit, db_path=db)] == [case_a]
        with pytest.raises(PermissionError): service.invoke("read_local_case", {"case_id": case_b}, "alice", registry_path=registry, audit_path=audit, db_path=db)
        with pytest.raises(ValueError): service.invoke("read_local_case", {"case_id": case_a, "actor": "bob"}, "alice", registry_path=registry, audit_path=audit, db_path=db)
        note = "private note"
        result = service.invoke("prepare_local_note", {"case_id": case_a, "note": note}, "alice", registry_path=registry, audit_path=audit, db_path=db)
        assert "not sent" in result["status"]
        lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        assert [x["result"] for x in lines] == ["handled", "refused", "refused", "handled"]
        assert note not in audit.read_text(encoding="utf-8")
    finally:
        note_path = service._case_dir(case_a) / "prepared-notes.json"
        if note_path.exists(): note_path.unlink(); note_path.parent.rmdir()
        registry.unlink(missing_ok=True)
        audit.unlink(missing_ok=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = work / (db.name + suffix)
            candidate.unlink(missing_ok=True)
        work.rmdir()


def test_unknown_excluded_tools_and_malformed_args_are_refused():
    import uuid
    work = service.OWNED_ROOT / ("unknown-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    audit = work / "audit.jsonl"
    for name, args in [("accept-claim", {}), ("send", {}), ("approve", {}), ("read_local_case", {"case_id": "SYNTH-CASE-1", "extra": 1})]:
        with pytest.raises((ValueError, PermissionError)):
            service.invoke(name, args, audit_path=audit)
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 4
    audit.unlink(); work.rmdir()


def test_registry_and_audit_paths_are_owned_bounded_and_no_symlinks():
    import uuid
    import subprocess
    work = service.OWNED_ROOT / ("path-check-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    registry, audit = work / "registry.json", work / "audit.jsonl"
    link = work / "registry-link.json"
    junction_target, junction = work / "junction-target", work / "junction-link"
    registry.write_text(json.dumps({"actor_cases": {"alice": ["A"]}}), encoding="utf-8")
    try:
        assert service._registry(registry)["actor_cases"]["alice"] == ["A"]
        with pytest.raises(PermissionError, match="inside the MCP local data folder"):
            service._registry(service.ROOT / "copilot" / "app.py")
        with pytest.raises(PermissionError, match="traversal"):
            service._registry(work / ".." / "outside.json")
        with pytest.raises(PermissionError, match="inside the MCP local data folder"):
            service._audit("read_local_case", {"case_id": "A"}, "alice", "handled", service.ROOT / "copilot" / "app.py")
        registry.write_text(json.dumps({"actor_cases": {"alice": ["A"]}, "extra": "not allowed"}), encoding="utf-8")
        with pytest.raises(PermissionError, match="schema invalid"):
            service._registry(registry)
        registry.write_text(json.dumps({"actor_cases": {"alice": ["invalid id"]}}), encoding="utf-8")
        with pytest.raises(PermissionError, match="authorization is invalid"):
            service._registry(registry)
        registry.write_text(json.dumps({"actor_cases": {"alice": ["PP-R-MCP-0000001"]}}), encoding="utf-8")
        try:
            link.symlink_to(registry)
        except OSError as exc:
            # Preserve the exact capability limitation; exercise a real Windows
            # directory junction below, which does not require symlink privilege.
            assert getattr(exc, "winerror", None) == 1314, f"Unexpected symlink creation failure: {exc}"
        else:
            with pytest.raises(PermissionError, match="linked storage"):
                service._registry(link)
            link.unlink()

        if os.name == "nt":
            junction_target.mkdir()
            created = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(junction_target)], capture_output=True, text=True, check=False)
            assert created.returncode == 0, f"Could not create local directory junction: {created.stdout} {created.stderr}"
            assert junction.is_junction(), "Fixture must be an actual Windows directory junction."
            with pytest.raises(PermissionError, match="linked storage"):
                service._registry(junction / "registry.json")
        else:
            pytest.skip("Directory junction regression is Windows-specific.")
    finally:
        if junction.is_junction(): junction.rmdir()
        if link.is_symlink(): link.unlink()
        if junction_target.exists(): junction_target.rmdir()
        registry.unlink(missing_ok=True); audit.unlink(missing_ok=True); work.rmdir()


def test_registry_and_audit_file_size_limits_refuse_before_read_or_append():
    work = service.OWNED_ROOT / ("size-check-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    registry, audit = work / "large-registry.json", work / "large-audit.jsonl"
    try:
        registry.write_bytes(b" " * (service.MAX_REGISTRY_BYTES + 1))
        with pytest.raises(PermissionError, match="exceeds 2 MB"):
            service._registry(registry)
        audit.write_bytes(b" " * (service.MAX_AUDIT_BYTES + 1))
        before = audit.stat().st_size
        with pytest.raises(PermissionError, match="exceeds 10 MB"):
            service._audit("read_local_case", {"case_id": "A"}, "alice", "handled", audit)
        assert audit.stat().st_size == before
    finally:
        registry.unlink(missing_ok=True); audit.unlink(missing_ok=True); work.rmdir()


def test_stdio_server_main_refuses_to_start_with_its_flag_off(monkeypatch):
    from copilot import config
    from copilot.features.mcp_local_contracts import server
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.delenv("COPILOT_FEATURE_MCP_LOCAL_CONTRACTS", raising=False)
    with pytest.raises(SystemExit, match="disabled"):
        server.main()
