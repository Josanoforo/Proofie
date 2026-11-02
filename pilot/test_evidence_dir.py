"""ONE evidence folder setting. PROOFIE_EVIDENCE_DIR when set, otherwise <repo>/evidence (unchanged).

Each case runs in a fresh interpreter because the modules read the setting when they are imported. Nothing here
reaches PayPal or any model: the writers are called with dummy credentials and a made-up record, never a request.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import json, os, sys
from pathlib import Path
root = Path(os.environ["PROBE_ROOT"])
sys.path[:0] = [str(root), str(root / "pilot")]
import client
from copilot.core import vision_model
import step2_order, step3_dispute, step4_evidence_ai, step5_send_evidence
sys.path.insert(0, str(root / "demo"))
import mcp_smoke
from copilot import mcp_server
constants = [client.EVIDENCE, vision_model.EVIDENCE, vision_model.LOG, step2_order.STATE, step3_dispute.STATE,
             step4_evidence_ai.LOG, step5_send_evidence.ATTEMPTS_DIR, mcp_smoke.LOG]
if os.environ.get("PROBE_WRITE"):          # only with the setting pointing away from the repo: never write to the real folder
    os.environ["PAYPAL_CLIENT_ID"] = "x"; os.environ["PAYPAL_CLIENT_SECRET"] = "x"
    c = client.SandboxClient("probe_client")
    c._log({"op": "probe"})
    vision_model._log({"op": "probe"})
    mcp_smoke.log({"op": "probe"})
    mcp_client = mcp_server._make_client()
    mcp_client._log({"op": "probe"})
    constants += [c.log_path, mcp_client.log_path]
print(json.dumps({"constants": [str(p) for p in constants], "evidence_dir": str(client.evidence_dir())}))
"""


def _run(env_value, tmp_path, *, data_dir=None):
    env = os.environ.copy()
    for name in ("PROOFIE_EVIDENCE_DIR", "COPILOT_DATA_DIR", "PROBE_WRITE"):
        env.pop(name, None)
    env.update({"PROBE_ROOT": str(ROOT), "COPILOT_DB": str(tmp_path / "probe.db")})
    if data_dir is not None:
        env["COPILOT_DATA_DIR"] = str(data_dir)
    if env_value:
        env["PROOFIE_EVIDENCE_DIR"] = str(env_value)
        env["PROBE_WRITE"] = "1"            # the writers are exercised only when the folder is somewhere else
    elif env_value is not None:
        env["PROOFIE_EVIDENCE_DIR"] = ""
    done = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                          timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _repo_evidence_snapshot():
    """Names and sizes only (never contents) of the repository's own evidence folder."""
    folder = ROOT / "evidence"
    if not folder.exists():
        return None
    return sorted((p.name, p.stat().st_size) for p in folder.iterdir())


def test_unset_the_default_is_the_repo_evidence_folder_exactly_as_before(tmp_path):
    out = _run(None, tmp_path)
    default = str(ROOT / "evidence")
    assert out["evidence_dir"] == default
    assert out["constants"][0] == default and out["constants"][1] == default
    assert all(Path(p).is_relative_to(ROOT / "evidence") for p in out["constants"])


def test_set_every_writer_writes_there_and_nothing_is_created_under_the_repo_evidence(tmp_path):
    target = tmp_path / "private" / "evidence"                 # does not exist yet; its parent does not either
    fallback_data = tmp_path / "fallback-data"
    before = _repo_evidence_snapshot()
    out = _run(target, tmp_path, data_dir=fallback_data)
    assert out["evidence_dir"] == str(target.resolve())
    assert all(Path(p).is_relative_to(target.resolve()) for p in out["constants"]), out["constants"]
    written = {p.name for p in target.iterdir()}
    assert {"probe_client.jsonl", "copilot_vision.jsonl", "mcp_smoke.jsonl", "copilot_mcp.jsonl"} <= written
    assert not (fallback_data / "evidence").exists()       # explicit PROOFIE_EVIDENCE_DIR wins over data-dir fallback
    assert _repo_evidence_snapshot() == before                 # the repo's folder is untouched (no new file, no growth)


def test_an_empty_value_means_not_set(tmp_path):
    assert _run("", tmp_path)["evidence_dir"] == str(ROOT / "evidence")


def test_data_dir_is_the_default_when_proofie_evidence_dir_is_unset(tmp_path):
    data_dir = tmp_path / "synthetic-data"
    expected = str((data_dir / "evidence").resolve())
    out = _run(None, tmp_path, data_dir=data_dir)
    assert out["evidence_dir"] == expected
    assert all(Path(path).is_relative_to(Path(expected)) for path in out["constants"])


def test_the_mcp_log_name_still_cannot_leave_the_evidence_folder(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    try:
        from copilot import mcp_server
    finally:
        sys.path.remove(str(ROOT))
    for hostile in ("../escape", "..\\escape", "a/b", "C:\\x", "x" * 41, "UPPER", ""):
        monkeypatch.setenv("COPILOT_MCP_LOG_NAME", hostile)
        assert mcp_server.mcp_log_name() == "copilot_mcp", hostile
    monkeypatch.setenv("COPILOT_MCP_LOG_NAME", "mcp_smoke")
    assert mcp_server.mcp_log_name() == "mcp_smoke"
