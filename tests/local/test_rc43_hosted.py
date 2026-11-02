"""RC4.3 W1-4a: in hosted mode the access page says what this copy is.

Offline only. Written first and red for the right reason: before the block existed the page had the form and nothing else.
The pinned wording of the page (h1, hint, the one button) and of the hosted sentence is asserted again here so the new block
can only add text.
"""
import re
from html import unescape

import pytest
from _dom import parse
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.hosted_ephemeral import NOTICE, create_app as create_ephemeral_app

ACCESS_CODE = "test-hosted-access-code"
BLOCK = "hosted-about"
ITEMS = ("about-manual", "about-account", "about-restart", "about-local")


def _hosted(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", ACCESS_CODE)


def _access(app):
    web = TestClient(app, base_url="https://localhost", client=("127.0.0.1", 12345))
    response = web.get("/access")
    assert response.status_code == 200
    return response.text


def _disk_app(monkeypatch):
    _hosted(monkeypatch)
    return create_app()


def _ephemeral_app(monkeypatch, tmp_path):
    _hosted(monkeypatch)
    monkeypatch.setenv("COPILOT_EPHEMERAL_ROOT", str(tmp_path))
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "false")
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)
    return create_ephemeral_app()


def _items(html):
    block = parse(html).find(data_state=BLOCK)
    assert block is not None, "the access page has no 'what this copy is' block"
    return block, {key: block.find(data_state=key) for key in ITEMS}


def test_the_access_page_says_what_this_copy_is(monkeypatch):
    html = _access(_disk_app(monkeypatch))
    block, items = _items(html)
    assert all(items.values()), items
    assert "manual" in items["about-manual"].text().lower() and "switched off" in items["about-manual"].text()
    assert "type the shipment facts yourself" in items["about-manual"].text()
    assert "one shared PayPal sandbox account" in items["about-account"].text()
    assert "same disputes" in items["about-account"].text()
    local = items["about-local"].text()
    assert "local assistant" in local and "MCP server" in local and "your own computer" in local
    assert block.find("h2") is not None and block.find("h2").text() == "What this copy is"


def test_the_restart_line_follows_the_factory(monkeypatch, tmp_path):
    disk = _items(_access(_disk_app(monkeypatch)))[1]["about-restart"].text()
    assert "restart may erase" in disk and "database" in disk
    assert NOTICE not in disk
    ephemeral = _items(_access(_ephemeral_app(monkeypatch, tmp_path)))[1]["about-restart"].text()
    assert NOTICE in ephemeral, ephemeral


def test_the_pinned_access_wording_is_unchanged_and_the_block_is_not_a_second_button(monkeypatch):
    html = _access(_disk_app(monkeypatch))
    dom = parse(html)
    assert dom.find("h1").text() == "Open the demo"
    assert "The code you were given to open this demo." in dom.text()
    primaries = [n for n in dom.find_all("button") if "btn-primary" in n.attrs.get("class", "") or n.attrs.get("data-kind") == "primary"]
    buttons = [n.text() for n in dom.find_all("button")]
    assert buttons == ["Open the demo"], buttons
    assert dom.find("nav", aria_label="Main") is None
    assert len(primaries) <= 1


def test_the_block_sits_after_the_form_and_follows_the_copy_rules(monkeypatch):
    html = _access(_disk_app(monkeypatch))
    block, _ = _items(html)
    assert html.index('data-state="hosted-about"') > html.index("</form>")
    assert block.parent is not None and block.find("form") is None
    text = unescape(re.sub(r"<[^>]+>", " ", html[html.index('data-state="hosted-about"'):html.index("</main>")]))
    assert not re.search(r"\b(we|our|us|please)\b", text, re.I), text


def test_the_block_also_shows_on_a_refused_code(monkeypatch):
    app = _disk_app(monkeypatch)
    web = TestClient(app, base_url="https://localhost", client=("127.0.0.1", 12345))
    assert web.get("/access").status_code == 200
    bad = web.post("/access", data={"code": "wrong code!", "csrf_token": web.cookies.get("csrf_token"), "next": "/disputes"})
    assert bad.status_code == 401
    assert 'data-state="hosted-about"' in bad.text


def test_the_hosted_sentence_and_the_model_off_message_are_unchanged():
    # pinned verbatim elsewhere; this only guards that W1-4 added text next to them and did not edit them
    assert config.HOSTED_MODEL_OFF_MESSAGE == "The AI assistant is switched off in the hosted demo"
    assert config.HOSTED_DEMO_SENTENCE.startswith("Proofie is built to run on your own computer. ")
    assert "It uses one shared PayPal sandbox account, so everyone who opens it sees the same disputes and uploads. " in config.HOSTED_DEMO_SENTENCE
