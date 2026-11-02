"""Cold startup uses only a synthetic env file, independently of pytest isolation."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
CC = ("account_cliff", "active_approval", "carrier_required", "claims", "deadlines",
      "events", "fulfilment", "ledger", "outcome", "package_print", "resolution", "triage")

CHILD = r'''
import json, os, sys
from pathlib import Path
import httpx
def forbidden(*args, **kwargs):
    raise AssertionError("real transport is forbidden")
httpx.HTTPTransport.handle_request = forbidden
httpx.AsyncHTTPTransport.handle_async_request = forbidden
from copilot import config
root = Path(sys.argv[1])
config.ROOT = root
config.DATA_DIR = root / "data"
config.DB_PATH = root / "data" / "test.db"
config.UPLOADS_DIR = root / "uploads"
from copilot.core.disputes import SandboxClient
sdk = sys.modules[SandboxClient.__module__]
sdk.ROOT = root
mode = sys.argv[2]
calls = []
original = getattr(config, "load_env_file", lambda: None)
def observed():
    calls.append(True)
    return original()
config.load_env_file = observed
if mode == "isolated":
    import conftest
from copilot.app import app
from fastapi.testclient import TestClient
with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
    statuses = {path: client.get(path, follow_redirects=False).status_code
                for path in ("/deadlines", "/account",
                             "/disputes/SYNTHETIC/packages/1/active-approval")}
approval = statuses.pop("/disputes/SYNTHETIC/packages/1/active-approval")
print(json.dumps({"statuses": statuses, "approval_registered": approval != 404,
    "startup_calls": len(calls), "deadline_flag": config.feature_enabled("deadlines"),
    "feature_modules": sorted(name for name in sys.modules if name.startswith("copilot.features"))}))
'''


def cold(tmp_path, mode="cold", file=True, shell=None, inherit_isolation=False, feature_names=CC):
    if file:
        values = ["COPILOT_FEATURE_" + name.upper() + "=true" for name in feature_names]
        values += ["PAYPAL_CLIENT_ID=your-client-id-here",
                   "PAYPAL_CLIENT_SECRET=your-client-secret-here"]
        (tmp_path / ".env").write_text("\n".join(values) + "\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COPILOT_", "PAYPAL_", "PROOFIE_"))
           and key not in ("PYTHONPATH", "PYTEST_CURRENT_TEST")
           and (inherit_isolation or key != "PYTHON_DOTENV_DISABLED")}
    env["PROOFIE_EVIDENCE_DIR"] = str(tmp_path / "evidence")
    env.update(shell or {})
    result = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path), mode],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_cold_env_registers_cc_routes(tmp_path):
    result = cold(tmp_path)
    assert result["statuses"] == {"/deadlines": 503, "/account": 503}
    assert result["approval_registered"]
    assert result["startup_calls"] >= 1


def test_shell_false_wins_file_true(tmp_path):
    result = cold(tmp_path, shell={"COPILOT_FEATURE_DEADLINES": "false"})
    assert result["statuses"] == {"/deadlines": 404, "/account": 503}
    assert not result["deadline_flag"]


def test_absent_env_keeps_flags_off_and_calls_loader(tmp_path):
    result = cold(tmp_path, file=False)
    assert result["statuses"] == {"/deadlines": 404, "/account": 404}
    assert not result["approval_registered"]
    assert result["startup_calls"] >= 1


def test_root_conftest_disables_loading_before_app_import(tmp_path):
    result = cold(tmp_path, mode="isolated")
    assert result["statuses"] == {"/deadlines": 404, "/account": 404}
    assert not result["deadline_flag"]
    assert result["startup_calls"] == 0


def test_test_isolation_is_inherited_by_fresh_child(tmp_path):
    """A child drops shell flags, keeps test isolation and never imports conftest."""
    other = ("fulfilment_register", "resolution_writes", "outcome_appeal", "webhooks_verify",
             "webhooks", "draft", "delivery_facts", "conformance")
    result = cold(tmp_path, mode="inherited", inherit_isolation=True, feature_names=CC + other)
    assert result["statuses"] == {"/deadlines": 404, "/account": 404}
    assert result["feature_modules"] == []
    assert not result["approval_registered"]
    assert result["startup_calls"] >= 1
