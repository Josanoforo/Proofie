"""All-off no-import/no-table proof and hosted local-only view."""
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from _hosted import authenticated_client, configure_hosted


def test_all_off_no_events_import_route_or_db(tmp_path, web_mode):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    env["COPILOT_DATA_DIR"] = str(tmp_path)
    code = "import sys; sys.path.insert(0,'tests/local'); from copilot.app import create_app; from _hosted import authenticated_client; c=authenticated_client(create_app(),client=('127.0.0.1',12345)); assert c.get('/events/state').status_code==404; assert not any(m.startswith('copilot.features') for m in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[3], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_hosted_events_page_uses_no_paypal_and_no_database_write(tmp_path, monkeypatch):
    import httpx
    from copilot.app import create_app
    from copilot.core.disputes import SandboxClient
    from copilot import config
    monkeypatch.setenv("COPILOT_FEATURE_EVENTS", "true")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    configure_hosted(monkeypatch)
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_db_path_override", None)
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external call")
    monkeypatch.setattr(SandboxClient, "call", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    result = authenticated_client(create_app(), client=("127.0.0.1", 12345)).get("/events/state")
    assert result.status_code == 200 and "two minutes" in result.text
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

_FROZEN_FEATURE_DEPENDENCIES = set()


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'events') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'events'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'events'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'events') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)
