"""A feature namespace may be left out of an export, so no module outside it may import it at module level; with its flag
unset the namespace is not loaded."""
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _is_feature_import(node):
    if isinstance(node, ast.Import):
        return any(a.name.startswith("copilot.features") for a in node.names)
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        return node.level == 0 and (mod.startswith("copilot.features")
                                    or (mod == "copilot" and any(a.name == "features" for a in node.names)))
    return False


def test_no_module_level_import_of_copilot_features_outside_the_namespace():
    offenders = []
    for path in sorted((ROOT / "copilot").rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[:2] == ("copilot", "features"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        offenders += [f"{rel.as_posix()}:{n.lineno}" for n in tree.body if _is_feature_import(n)]
    assert not offenders, "module-level import of copilot.features at " + ", ".join(offenders)


def test_create_app_with_the_flag_unset_loads_no_feature_module():
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    code = ("from copilot.app import create_app; create_app(); import sys; "
            "print(sorted(m for m in sys.modules if m.startswith('copilot.features')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"



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

# O3: the account page adds the dispute-exposure line behind COPILOT_FEATURE_EXPLAINABLE_QUEUE; the import sits inside the function, after the flag check in the route
_FROZEN_FEATURE_DEPENDENCIES = {'explainable_queue'}


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'account_cliff') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'account_cliff'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'account_cliff'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'account_cliff') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)
