"""demo/mcp_smoke.py expects the tool list the server really builds, in both states of the presentation-mode flag.
A judge who exports the flag (or whose MCP client sets it) must still get a pass: five core tools, plus review_case_snapshot exactly
when COPILOT_FEATURE_PRESENTATION_MODE is "true" in the server process's environment."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FLAG = "COPILOT_FEATURE_PRESENTATION_MODE"
CORE = {"list_disputes", "get_dispute_file", "what_is_missing", "prepare_draft", "get_send_status"}


def _smoke():
    sys.path.insert(0, str(ROOT / "pilot"))
    spec = importlib.util.spec_from_file_location("mcp_smoke_under_test", ROOT / "demo" / "mcp_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_expectation_follows_the_flag_the_way_the_server_reads_it():
    smoke = _smoke()
    assert smoke.expected_tools({}) == CORE
    assert smoke.expected_tools({FLAG: "false"}) == CORE
    assert smoke.expected_tools({FLAG: "yes"}) == CORE
    assert smoke.expected_tools({FLAG: "true"}) == CORE | {"review_case_snapshot"}
    assert smoke.expected_tools({FLAG: " TRUE "}) == CORE | {"review_case_snapshot"}


@pytest.mark.parametrize("value", ["", "false", "true"])
def test_the_real_tool_list_equals_the_expectation(monkeypatch, value):
    from copilot import mcp_server
    smoke = _smoke()
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "false")
    if value:
        monkeypatch.setenv(FLAG, value)
    else:
        monkeypatch.delenv(FLAG, raising=False)
    env = {FLAG: value} if value else {}
    listed = {t.name for t in asyncio.run(mcp_server.build_server().list_tools())}
    assert listed == smoke.expected_tools(env)
