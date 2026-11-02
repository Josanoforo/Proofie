"""The timeline feature stays out of the way when its flag is off, and out of other features always.

Expected: flag off, rendering a dispute page imports nothing of copilot.features.timeline; the flag read on the next render
turns it on without a restart; the package imports no other feature namespace (its source is scanned, not executed).
"""
import ast
import json
import os
import subprocess
import sys
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "copilot" / "features" / "timeline"
BODY = json.loads((ROOT / "pilot" / "fixtures" / "synthetic" / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json")
                  .read_text(encoding="utf-8"))["body"]

SCRIPT = """
import json, os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "tests", "local"))
from _hosted import authenticated_client
from copilot.app import create_app
from copilot.web.routes_disputes import get_client
BODY = json.loads(os.environ["TL_BODY"])
class Fake:
    def call(self, method, path, op, **kw):
        assert method == "GET"
        return {"show_dispute": BODY, "recovery_get": {"evidences": []}, "context_transaction_search": {"transaction_details": []},
                "context_tracking": {"trackers": []}}[op]
os.environ["COPILOT_FEATURE_TIMELINE"] = "false"
app = create_app()
app.dependency_overrides[get_client] = lambda: Fake()
web = authenticated_client(app, client=("127.0.0.1", 12345))
loaded = lambda: any(m.startswith("copilot.features.timeline") for m in sys.modules)
route = "/disputes/PP-R-DEMO-0000001"
off = web.get(route)
assert off.status_code == 200 and off.url.path == route and "paypal-timeline" not in off.text and not loaded(), (off.status_code, off.url.path, loaded())
os.environ["COPILOT_FEATURE_TIMELINE"] = "true"
on = web.get(route + "/workspace/records")
assert on.status_code == 200 and on.url.path == route + "/workspace/records" and 'data-state="paypal-timeline"' in on.text and loaded(), (on.status_code, on.url.path, loaded())
"""


def test_flag_off_imports_nothing_and_the_flag_is_read_on_the_next_render(tmp_path, web_mode):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    env.update(COPILOT_DATA_DIR=str(tmp_path), TL_BODY=json.dumps(BODY))
    env.pop("PYTHONPATH", None)
    if web_mode == "hosted":
        env["COPILOT_ACCESS_CODE"] = "test-hosted-access-code"
        # The hosted inbox bound (D-114 item 5) answers 403 for any dispute outside the judge pool. This test reads the replay case,
        # which the bound lets through while the replay flag is on. (COPILOT_JUDGE_POOL=PP-R-DEMO-0000001 would not do: the pool
        # allowlist takes three-letter ids, PP-R-ABC-123456, and drops this one.)
        env["COPILOT_FEATURE_REPLAY"] = "true"
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr


def _dependencies(directory):
    found = set()
    for source in sorted(directory.rglob("*.py")):
        package = ".".join(("copilot", "features", "timeline", *source.relative_to(directory).parts[:-1]))
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package) if node.level else (node.module or "")
                names = [base, *(base + "." + alias.name for alias in node.names)]
            else:
                continue
            for name in names:
                parts = name.split(".")
                if len(parts) >= 3 and parts[:2] == ["copilot", "features"] and parts[2] != "timeline":
                    found.add(parts[2])
    return found


def test_the_package_imports_no_other_feature_namespace():
    assert list(PACKAGE.rglob("service.py")), "the package holds its service"
    assert _dependencies(PACKAGE) == set()
