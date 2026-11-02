"""Root conftest, shared by pilot/, tools/ and tests/ (run together: `pytest pilot tools tests`).

Three autouse fixtures close the ways a test has accidentally reached something real:

1. _isolated_copilot_data: every test gets its own tmp_path for copilot's DB and uploads, via
   copilot/config.py's db_path()/uploads_dir() override hooks - see that module's docstring for why this
   must be a function-based override and not a monkeypatched module constant.
2. _block_real_network: every test that is not marked `sandbox` or `live_model` gets the real PayPal
   sandbox and the real local Ollama server made unreachable, loudly. This is enforced at httpx's actual
   transport layer (HTTPTransport/AsyncHTTPTransport.handle_request), never by inspecting the request's
   target host - a host-based check would also trip on pilot's own httpx.MockTransport-based fakes, which
   build requests with the same sandbox URL but never let them reach a socket. Only the transport that
   would really dial out is patched, so every existing Fake/Mock-transport test is unaffected.
3. _isolated_vision_evidence_log: found while testing the confirm-reading feature (2026-10-05) - any test
   anywhere in the suite that happens to upload a real image document and reach copilot/web/routes_package.py's
   create_package's AI-success path (even without directly testing vision at all) calls the real
   copilot/core/vision_model.propose_image_claims(), which logs to evidence/copilot_vision.jsonl - _block_
   real_network above stops the actual network call, but the LOG WRITE itself still happened, with real
   "blocked" noise, polluting the committed evidence file from unrelated tests. Redirected globally, the
   same way as #1, rather than requiring every test file that might incidentally touch an image to
   remember its own local fixture.
"""
from __future__ import annotations

import subprocess
import json
import os
import sys
from pathlib import Path

# Fresh application interpreters inherit this standard python-dotenv switch.
# Independent cold-start fixtures explicitly clear it to exercise real loading.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import httpx
import pytest
import dotenv
import dotenv.main


def _skip_env_file(*args, **kwargs):
    """Never read developer settings during imports, collection or test calls."""
    return False


# Install before any app-bearing import; an autouse fixture runs too late.
# The SDK imports the same loader lazily, so suppress that binding as well.
dotenv.load_dotenv = _skip_env_file
dotenv.main.load_dotenv = _skip_env_file

from copilot import config
config.load_env_file = _skip_env_file
from copilot.core import vision_model

sys.path.insert(0, str(Path(__file__).parent / "tests" / "local"))


# A TestClient used without `with` starts a new event loop for every request, and on Windows each loop makes a
# loopback socket pair that stays in TIME_WAIT: a long run of thousands of requests ends in WinError 10055 ("not enough
# buffer space") on an unrelated test. One portal (one loop) shared by all clients removes that source.
import atexit
import contextlib
import anyio.from_thread
from starlette import testclient as _starlette_testclient

_SHARED_PORTAL = {"portal": None, "manager": None}


def _shared_portal():
    if _SHARED_PORTAL["portal"] is None:
        manager = anyio.from_thread.start_blocking_portal()
        _SHARED_PORTAL["portal"] = manager.__enter__()
        _SHARED_PORTAL["manager"] = manager
        atexit.register(manager.__exit__, None, None, None)
    return _SHARED_PORTAL["portal"]


def _portal_factory(self):
    if self.portal is not None:
        yield self.portal
    else:
        yield _shared_portal()


_starlette_testclient.TestClient._portal_factory = contextlib.contextmanager(_portal_factory)


def pytest_configure(config):
    config.addinivalue_line("markers", "local_spec: explicit local model/write specification; hosted counterpart required")
    config.addinivalue_line("markers", "hosted_spec: explicit hosted counterpart to a local specification")


def pytest_generate_tests(metafunc):
    if "web_mode" in metafunc.fixturenames:
        modes = (["hosted"] if metafunc.definition.get_closest_marker("hosted_spec") else
                 ["local-spec"] if metafunc.definition.get_closest_marker("local_spec") else ["local", "hosted"])
        metafunc.parametrize("web_mode", modes, indirect=True, ids=modes)


@pytest.fixture
def web_mode(request, monkeypatch):
    """Opt-in only: named cases visibly declare the mode, independent of suite environment."""
    mode = request.param
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true" if mode == "hosted" else "false")
    if mode == "hosted":
        monkeypatch.setenv("COPILOT_ACCESS_CODE", "test-hosted-access-code")
    assert config.hosted_demo() == (mode == "hosted")
    return mode


def pytest_collection_finish(session):
    destination = os.environ.get("TEST_COLLECTION_MANIFEST")
    if destination:
        path = Path(destination).resolve()
        root = Path(__file__).parent.resolve()
        if not path.is_relative_to(root) or not path.relative_to(root).parts[0].startswith("scratch-"):
            raise ValueError("Collection manifest must stay in the test scratch directory")
        rows = [{"nodeid": item.nodeid,
                 "mode": getattr(item, "callspec", None).params.get("web_mode", "explicit-or-unit")
                 if hasattr(item, "callspec") else "explicit-or-unit"} for item in session.items]
        with path.open("x", encoding="utf-8") as stream:
            json.dump(rows, stream, indent=2)

_BLOCKED_HOSTS = {"127.0.0.1", "api-m.sandbox.paypal.com"}


def in_git_checkout(root):
    """True only when `root` is a real git checkout: `root/.git` exists and `git rev-parse` succeeds there.
    An exported tree (the public ZIP) has no .git, so tests that need the private repository skip through this."""
    root = Path(root)
    if not (root / ".git").exists():
        return False
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True)
        return result.returncode == 0 and Path(result.stdout.decode("utf-8").strip()).resolve() == root.resolve()
    except OSError:
        return False


def is_private_checkout(root):
    """A public Git clone has Git too; only the private control marker identifies this checkout."""
    return (Path(root) / "internal" / "codex-control").is_dir()


# copilot/hosted_ephemeral.create_app() points these two at its session folder in os.environ and keeps them (a hosted
# process owns them). A test that builds that app would leave them set for every later test, and the MCP service's
# owned_root() follows COPILOT_DATA_DIR into a folder pytest has already removed. Restored after every test.
_PROCESS_DATA_ENV = ("COPILOT_DATA_DIR", "PROOFIE_EVIDENCE_DIR")


@pytest.fixture(autouse=True)
def _isolated_copilot_data(tmp_path):
    saved = {name: os.environ.get(name) for name in _PROCESS_DATA_ENV}
    config.set_db_path_override(tmp_path / "test.db")
    config.set_uploads_dir_override(tmp_path / "uploads")
    try:
        yield
    finally:
        config.set_db_path_override(None)
        config.set_uploads_dir_override(None)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def _hosted_pool_scope_opt_in(request, monkeypatch):
    """The hosted demo lists and opens only the judge pool and the record case (copilot/core/pool_scope.py, D-114 item 5). Most
    hosted tests are about something else (the access gate, the footer sentence, a flag's bytes) and read a fixture dispute that
    is in no pool, so the bound is off for them. A test that is about the bound opts in with @pytest.mark.pool_scope."""
    if request.node.get_closest_marker("pool_scope") is None:
        from copilot.core import pool_scope
        monkeypatch.setattr(pool_scope, "active", lambda: False)


@pytest.fixture(autouse=True)
def _isolated_vision_evidence_log(tmp_path, monkeypatch, request):
    if "live_model" not in request.node.keywords:
        monkeypatch.setattr(vision_model, "LOG", tmp_path / "copilot_vision.jsonl")


@pytest.fixture(autouse=True)
def _block_real_network(request, monkeypatch):
    if request.node.get_closest_marker("sandbox") or request.node.get_closest_marker("live_model"):
        return

    def _refuse(self, *args, **kwargs):
        raise RuntimeError(
            "Blocked a real network call from a test not marked 'sandbox' or 'live_model' "
            "(conftest.py's _block_real_network). If this test genuinely needs the real PayPal "
            "sandbox or a real local Ollama, add the matching marker; otherwise fake the transport "
            "(httpx.MockTransport) or the client/dependency, as every other test here does.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse)


@pytest.fixture
def legacy_approval_scenario(monkeypatch):
    """Named original passive-approval scenarios, distinct from flags-on gate coverage.

    Only tests explicitly requesting this fixture use the pre-active-approval
    contract. The candidate environment and unmarked feature cases stay intact.
    """
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "false")
