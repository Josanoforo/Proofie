"""All-off behaviour and hosted offline checks with no credentials or transport."""
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from _hosted import authenticated_client, configure_hosted

ROOT = Path(__file__).resolve().parents[3]


def test_all_flags_off_loads_no_feature_modules_or_new_routes(web_mode):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    code = "import sys; sys.path.insert(0,'tests/local'); from copilot.app import create_app; from _hosted import authenticated_client; a=create_app(); assert not any(m.startswith('copilot.features') for m in sys.modules); c=authenticated_client(a,client=('127.0.0.1',12345)); assert all(c.get(p).status_code==404 for p in ('/conformance','/events/state','/deadlines'))"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cli_off_and_on_use_no_paypal_or_model(monkeypatch):
    from copilot.features.conformance.__main__ import main
    monkeypatch.delenv("COPILOT_FEATURE_CONFORMANCE", raising=False)
    assert main() == 2
    monkeypatch.setenv("COPILOT_FEATURE_CONFORMANCE", "true")
    monkeypatch.delenv("PROOFIE_SPECS_DIR", raising=False)
    monkeypatch.delenv("PROOFIE_EVIDENCE_DIR", raising=False)
    assert main() == 0


def test_hosted_conformance_page_is_offline(monkeypatch):
    import httpx
    from copilot import config
    from copilot.app import create_app
    from copilot.core.disputes import SandboxClient
    monkeypatch.setenv("COPILOT_FEATURE_CONFORMANCE", "true")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    configure_hosted(monkeypatch)
    monkeypatch.delenv("PROOFIE_SPECS_DIR", raising=False)
    monkeypatch.delenv("PROOFIE_EVIDENCE_DIR", raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external call")
    monkeypatch.setattr(SandboxClient, "call", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    # ASGI TestClient uses its own transport, which remains local.
    client = authenticated_client(create_app(), client=("127.0.0.1", 12345))
    response = client.get("/conformance")
    assert response.status_code == 200 and "14 of 16" in response.text
    assert config.hosted_demo()



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

_FROZEN_FEATURE_DEPENDENCIES = {'webhooks', 'resolution', 'fulfilment', 'outcome', 'account_cliff'}


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'conformance') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'conformance'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'conformance'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'conformance') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)
