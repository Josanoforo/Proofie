"""The bilingual feature is never imported with its flag off, and it imports no other feature."""
import ast
import os
import subprocess
import sys
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FEATURE = ROOT / "copilot" / "features" / "bilingual"
_FROZEN_FEATURE_DEPENDENCIES = set()


def test_flag_off_create_app_imports_no_bilingual_module(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    env.update(COPILOT_DATA_DIR=str(tmp_path), PYTHON_DOTENV_DISABLED="1", COPILOT_RULES_COUNTRY="MX")
    code = '''import sys
from copilot.app import create_app
create_app()
assert not [n for n in sys.modules if n.startswith("copilot.features.bilingual.")], sorted(sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 0, result.stderr


def _feature_dependencies(directory, namespace="bilingual"):
    dependencies = set()
    for source in sorted(directory.rglob("*.py")):
        relative = source.relative_to(directory)
        package = ".".join(("copilot", "features", namespace, *relative.parts[:-1]))
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
                if len(parts) >= 3 and parts[:2] == ["copilot", "features"] and parts[2] != namespace:
                    dependencies.add(parts[2])
    return dependencies


def test_the_feature_imports_no_other_feature():
    assert _feature_dependencies(FEATURE) == _FROZEN_FEATURE_DEPENDENCIES


def test_the_dependency_check_catches_a_planted_import(tmp_path):
    for path in FEATURE.rglob("*.py"):
        target = tmp_path / path.relative_to(FEATURE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    (tmp_path / "planted.py").write_text("from ..draft import check\n", encoding="utf-8")
    assert _feature_dependencies(tmp_path) == {"draft"}
