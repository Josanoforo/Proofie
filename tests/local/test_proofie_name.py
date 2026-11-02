"""The working name "Dispute Copilot" is now Proofie (D-2026-10-05-05) everywhere in the product, the MCP
server id is `proofie`, and the example client configuration is valid JSON under that name. The Python package path
`copilot/` does not change.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_routes_disputes as disputes_tests
from conftest import in_git_checkout, is_private_checkout
from _dom import parse, state
from copilot import mcp_server
from copilot.app import create_app

ROOT = Path(__file__).resolve().parents[2]
OLD_NAMES = ("dispute copilot", "dispute-copilot")
TABLE_1_FILES = ["copilot/app.py", "copilot/mcp_server.py", "copilot/web/static/app.js",
                 "copilot/web/templates/_base.html", ".mcp.json.example", "docs/MCP.md", "docs/UX-CRITERIA.md",
                 ]


def test_every_page_says_proofie_in_the_title_and_the_header():
    c = disputes_tests._client(disputes_tests._app_with_fake(disputes_tests.FakeClient(list_body=disputes_tests.LIST_PAGE1)))
    page = c.get("/disputes").text
    dom = parse(page)
    brand = dom.find("a", class_="brand")
    assert brand is not None and brand.text() == "Proofie" and brand.attrs["href"] == "/disputes"
    assert "dispute copilot" not in page.lower()
    # the header carries the name on every page, including an error page
    err = disputes_tests._client(disputes_tests._app_with_fake(disputes_tests.FakeClient(
        detail_raises=disputes_tests.PayPalError(404, "RESOURCE_NOT_FOUND", "d", {}))))
    assert "Proofie" in parse(err.get("/disputes/PP-R-NOPE-0001").text).find("a", class_="brand").text()


def test_the_app_and_the_mcp_server_carry_the_new_name():
    assert create_app().title == "Proofie"
    assert mcp_server.SERVER_NAME == "proofie" and mcp_server.build_server().name == "proofie"


def test_the_base_template_default_title_is_the_product_name():
    text = (ROOT / "copilot" / "web" / "templates" / "_base.html").read_text(encoding="utf-8")
    assert "{% block title %}Proofie{% endblock %}" in text


def test_the_example_client_configuration_is_valid_json_and_registers_proofie():
    config = json.loads((ROOT / ".mcp.json.example").read_text(encoding="utf-8"))     # was invalid JSON (\S) before
    assert list(config["mcpServers"]) == ["proofie"]
    server = config["mcpServers"]["proofie"]
    assert server["args"] == ["-m", "copilot.mcp_server"] and server["env"]["COPILOT_MCP_ALLOW_SEND"] == "false"
    assert "\\" in server["command"]                                                  # a real Windows path survived the parse


@pytest.mark.parametrize("path", TABLE_1_FILES)
def test_no_table_1_file_still_uses_the_old_name(path):
    if not (ROOT / path).exists() and not is_private_checkout(ROOT):
        pytest.skip("internal file, not published")          # in a private checkout an absent file still fails
    text = (ROOT / path).read_text(encoding="utf-8").lower()
    assert not [name for name in OLD_NAMES if name in text], path


def test_in_git_checkout_is_true_in_the_clone_and_false_in_a_dir_without_git(tmp_path):
    """The helper that lets the suite run inside an export tree tells the two cases apart."""
    assert (ROOT / ".git").exists() == in_git_checkout(ROOT)
    assert not in_git_checkout(tmp_path)
    (tmp_path / ".git").mkdir()                                  # a .git that is not a repository is not a checkout
    assert not in_git_checkout(tmp_path)


def test_the_package_path_is_unchanged():
    assert (ROOT / "copilot" / "mcp_server.py").exists() and not (ROOT / "proofie").exists()


@pytest.mark.parametrize("value,expected", [
    ("mcp_smoke", "mcp_smoke"), ("copilot_mcp", "copilot_mcp"), ("", "copilot_mcp"), ("../x", "copilot_mcp"),
    ("Upper", "copilot_mcp"), ("a" * 41, "copilot_mcp"), ("with space", "copilot_mcp"), ("a/b", "copilot_mcp")])
def test_the_mcp_evidence_log_name_can_only_be_a_simple_file_name_inside_evidence(monkeypatch, value, expected):
    monkeypatch.setenv(mcp_server.LOG_NAME_ENV, value)
    assert mcp_server.mcp_log_name() == expected
    monkeypatch.delenv(mcp_server.LOG_NAME_ENV)
    assert mcp_server.mcp_log_name() == "copilot_mcp"


def test_the_server_client_uses_the_configured_log_name(monkeypatch):
    seen = []

    class Recorder:
        def __init__(self, log_name, *args, **kwargs):
            seen.append(log_name)

    monkeypatch.setattr(mcp_server, "SandboxClient", Recorder)
    monkeypatch.setenv(mcp_server.LOG_NAME_ENV, "mcp_smoke")
    mcp_server._make_client()
    monkeypatch.delenv(mcp_server.LOG_NAME_ENV)
    mcp_server._make_client()
    assert seen == ["mcp_smoke", "copilot_mcp"]


def test_private_marker_distinguishes_public_git_from_private_checkout(tmp_path):
    assert not is_private_checkout(tmp_path)
    (tmp_path / ".git").mkdir()
    assert not is_private_checkout(tmp_path)
    (tmp_path / "internal" / "codex-control").mkdir(parents=True)
    assert is_private_checkout(tmp_path)
