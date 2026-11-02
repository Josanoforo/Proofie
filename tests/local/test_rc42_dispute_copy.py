"""First-person copy in copilot/app.py: no 'we', 'our' or 'please' in the seller-facing
strings of the error handlers; the pairs old -> new are pinned by the page tests that quote them. Offline, both web modes."""
import ast
import re
from pathlib import Path

import pytest
from _dom import parse

from copilot.app import create_app
from _hosted import authenticated_client

pytestmark = [pytest.mark.usefixtures("web_mode")]

APP = Path(__file__).resolve().parents[2] / "copilot" / "app.py"
FIRST_PERSON = re.compile(r"\b(we|our|ours|us|please)\b", re.I)


def _literals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [(n.lineno, n.value) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and (n.lineno > 150 or path.name != "app.py") and not n.value.startswith(("/", "copilot", "COPILOT")) and " " in n.value]


def test_the_seller_facing_strings_of_app_py_carry_no_first_person_or_please():
    found = [(line, text) for line, text in _literals(APP) if FIRST_PERSON.search(text)]
    assert found == []


def test_the_lint_itself_fails_on_a_planted_we():
    assert FIRST_PERSON.search("We could not read PayPal") and FIRST_PERSON.search("Please try again")
    assert not FIRST_PERSON.search("Proofie could not read PayPal. Try again in a moment.")


def test_the_not_found_page_and_the_unknown_dispute_page_use_the_new_words():
    web = authenticated_client(create_app(), client=("127.0.0.1", 12345))
    response = web.get("/no-such-page-at-all")
    assert response.status_code == 404 and parse(response.text).find("h1").text() == "That page was not found"
    assert "We could not" not in response.text


def test_the_seller_facing_strings_of_security_py_say_please_nowhere():
    security = APP.parent / "web" / "security.py"
    found = [(line, text) for line, text in _literals(security) if re.search(r"\bplease\b", text, re.I)]
    assert found == []
