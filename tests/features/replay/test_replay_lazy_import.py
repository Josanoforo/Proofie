"""The replay feature is not imported, and nothing is read from its fixture directory, unless its flag AND the hosted demo are on."""
import os
import subprocess
import sys

import pytest

CODE = '''import sys
from copilot.app import create_app
app = create_app()
imported = sorted(n for n in sys.modules if n.startswith("copilot.features.replay"))
print("IMPORTED", ",".join(imported))
'''


def _run(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COPILOT_FEATURE_", "COPILOT_HOSTED", "COPILOT_ACCESS"))}
    env.update(COPILOT_DATA_DIR=str(tmp_path), PYTHON_DOTENV_DISABLED="1", **extra)
    result = subprocess.run([sys.executable, "-c", CODE], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().split("IMPORTED", 1)[1].strip()


def test_flag_off_imports_nothing(tmp_path):
    assert _run(tmp_path) == ""
    assert _run(tmp_path, COPILOT_HOSTED_DEMO="true", COPILOT_ACCESS_CODE="synthetic-access-code") == ""


def test_flag_on_without_the_hosted_demo_imports_nothing(tmp_path):
    assert _run(tmp_path, COPILOT_FEATURE_REPLAY="true") == ""


def test_flag_on_in_the_hosted_demo_imports_the_routes(tmp_path):
    imported = _run(tmp_path, COPILOT_FEATURE_REPLAY="true", COPILOT_HOSTED_DEMO="true", COPILOT_ACCESS_CODE="synthetic-access-code")
    assert "copilot.features.replay.routes" in imported.split(",")
