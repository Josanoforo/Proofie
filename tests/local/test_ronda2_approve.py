"""Approving a package when the account country has no rule set stays refused, with the plain
no-rule-set line (the addendum's text, country from code) and never a page that tells the seller to edit a config file."""
import pytest
from _hosted import authenticated_client, reset_csrf
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.core import checklist_input
from copilot.db import repo
from test_uic1_copy import DID, WAITING, Fake, _text, _web
from test_uic2_copy import LINE


def _approve(monkeypatch, country):
    monkeypatch.setattr(checklist_input, "RULES_COUNTRY", country)
    monkeypatch.setattr(checklist_input, "_RULES_CACHE", [])
    c, token = _web(Fake(detail=WAITING))
    db = config.db_path()
    repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    pid = repo.insert_package(db, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pid}/approve", data={"csrf_token": token}, follow_redirects=False)
    return r, pid, db


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_unknown_country_refuses_approval_with_the_line_and_no_env_mention(monkeypatch):
    r, pid, db = _approve(monkeypatch, "ZZ")
    t = _text(r)
    assert r.status_code == 400 and LINE.format("ZZ") in t
    assert ".env" not in r.text and "COPILOT_RULES_COUNTRY" not in r.text and "500" not in t
    assert f"/disputes/{DID}/packages/{pid}" in r.text                   # a way back to the review page
    assert repo.get_latest_valid_approval(db, pid) is None              # approval stays refused


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_unknown_country_with_no_country_known_drops_the_parenthesis(monkeypatch):
    r, _, _ = _approve(monkeypatch, "")
    assert r.status_code == 400 and LINE.format("").replace(" ()", "") in _text(r)
    assert ".env" not in r.text


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
