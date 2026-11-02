"""All three flags off preserve core routes/imports and create no deadline tables."""
import os
import subprocess
import sys
from pathlib import Path


def test_all_off_has_no_feature_imports_routes_or_tables(tmp_path, web_mode):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    env["COPILOT_DATA_DIR"] = str(tmp_path)
    code = "import sys; sys.path.insert(0,'tests/local'); from copilot.app import create_app; from _hosted import authenticated_client; a=create_app(); c=authenticated_client(a,client=('127.0.0.1',12345)); assert not any(m.startswith('copilot.features') for m in sys.modules); assert all(c.get(p).status_code==404 for p in ('/conformance','/events/state','/deadlines'))"
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[3], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())



def _feature_dependencies(directory, namespace):
    """Resolve source imports without importing or executing feature modules."""
    import ast
    from importlib.util import resolve_name
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

_FROZEN_FEATURE_DEPENDENCIES = {'events'}


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'deadlines') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'deadlines'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'deadlines'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'deadlines') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)
