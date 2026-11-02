"""Static pins on seller-facing copy.

1. No template builds a label with replace('_' ...): labels come from the shared filters.
2. No first person plural and no 'Please' in seller-facing text. Scanned: the string literals (docstrings excluded) of the
   listed Python files; the text nodes and the visible attributes of every template; the string literals inside the
   template tags ({{ }} and {% %}, found with the Jinja lexer); the label and citation fields of the rules files.
   Whole words, case-insensitive.

An allowlist entry carries a reason; an entry that no longer matches anything fails, so the list cannot go stale."""
import ast
import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "copilot" / "web" / "templates"
RULES = ROOT / "copilot" / "rules"

# Files another lane is rewriting at the time of writing: scanned by their own test, so that a hit in in-flight text can
# be marked expected-to-fail for that lane alone.
P2_PYTHON_FILES = ("copilot/web/routes_package.py", "copilot/web/next_step.py", "copilot/web/status.py")
P2_TEMPLATES = ("package_review.html", "dispute_detail.html")
PYTHON_FILES = ("copilot/app.py", "copilot/web/routes_send.py", "copilot/features/events/evlog.py",
                "copilot/core/send_service.py", "copilot/web/presentation.py", "copilot/features/bilingual/service.py")

# "we're" and "we'll" start with the word "we". "US" in capitals is the country code (the rule sets are US and MX), not the
# pronoun, so only "us" and "Us" count.
FIRST_PERSON = re.compile(r"\b(?:(?i:we|our|please)|[Uu]s)\b")
UNDERSCORE_REPLACE = re.compile(r"""replace\(\s*['"]_""")

# (file, whole string literal or template text) -> reason
ALLOWED = {}      # nothing is allowed today; an entry needs a reason, for example a quotation of PayPal's own text

VISIBLE_ATTRIBUTES = re.compile(
    r"""\b(?:title|placeholder|aria-label|aria-description|alt|value)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _templates(only=None, skip=()):
    return sorted(p for p in TEMPLATES.rglob("*.html")
                  if (only is None or p.name in only) and p.name not in skip)


def _rel(path):
    return path.relative_to(ROOT).as_posix()


def test_no_template_labels_a_value_with_replace_underscore():
    hits = [f"{_rel(p)}:{n}" for p in _templates()
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if UNDERSCORE_REPLACE.search(line)]
    assert hits == [], "labels come from the shared filters, not from replace('_', ' '): " + ", ".join(hits)


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def _python_literals(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    skip = _docstring_ids(tree)
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip]


def _tag_literals(source):
    """String literals inside {{ }} and {% %}, read with the Jinja lexer."""
    return [value[1:-1] for _line, kind, value in Environment().lex(source) if kind == "string"]


def _template_texts(path):
    """The pieces a seller can read: text nodes outside script, style and comments, with the template tags removed, the
    visible attributes, and the string literals inside the tags."""
    source = path.read_text(encoding="utf-8")
    pieces = _tag_literals(source)
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    source = re.sub(r"\{%.*?%\}", "", source, flags=re.S)
    source = re.sub(r"\{\{.*?\}\}", "", source, flags=re.S)
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"<(script|style)\b.*?</\1>", "", source, flags=re.S | re.I)
    pieces += [a or b for a, b in VISIBLE_ATTRIBUTES.findall(source)]
    pieces += [t for t in re.split(r"<[^>]*>", source) if t.strip()]
    return pieces


def _rules_texts():
    """(file, text) for every label and citation field of the rules files, wherever it sits in the JSON."""
    found = []

    def walk(node, name):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("label", "citation") and isinstance(value, str):
                    found.append((name, value))
                else:
                    walk(value, name)
        elif isinstance(node, list):
            for item in node:
                walk(item, name)
    for path in sorted(RULES.glob("*.json")):
        walk(json.loads(path.read_text(encoding="utf-8")), _rel(path))
    return found


def _first_person_hits(python_files, templates, rules=True):
    seen, hits = set(), []

    def check(where, text, label):
        if FIRST_PERSON.search(text):
            key = (where, text.strip())
            seen.add(key)
            if key not in ALLOWED:
                hits.append(f"{label}: {text.strip()[:90]!r}")
    for relative in python_files:
        for line, text in _python_literals(relative):
            check(relative, text, f"{relative}:{line}")
    for path in templates:
        for text in _template_texts(path):
            check(_rel(path), text, _rel(path))
    if rules:
        for where, text in _rules_texts():
            check(where, text, where)
    return hits, seen


def test_seller_facing_copy_has_no_first_person_plural_and_no_please():
    templates = _templates(skip=P2_TEMPLATES)
    hits, seen = _first_person_hits(PYTHON_FILES, templates)
    covered = [text for path in _templates() for text in _template_texts(path)]
    assert len(_templates()) > 30 and len(covered) > 500 and any("The appeal call is switched off in this installation" in t for t in covered)
    assert len(_rules_texts()) > 20
    assert hits == [], "Proofie speaks in the third person and does not say please: " + "; ".join(hits)
    p2_hits, p2_seen = _first_person_hits(P2_PYTHON_FILES, _templates(only=P2_TEMPLATES), rules=False)
    assert set(ALLOWED) <= seen | p2_seen, "an allowlist entry matches nothing any more: " + str(sorted(set(ALLOWED) - seen - p2_seen))


def test_first_person_pin_on_the_files_another_lane_is_rewriting():
    hits, _seen = _first_person_hits(P2_PYTHON_FILES, _templates(only=P2_TEMPLATES), rules=False)
    assert hits == [], "Proofie speaks in the third person and does not say please: " + "; ".join(hits)


def test_the_pin_sees_a_violation_when_there_is_one(tmp_path):
    """The scan itself works: a template with first-person text, a tag literal, a value attribute and a replace is caught."""
    page = tmp_path / "x.html"
    page.write_text("{# we in a comment #}<p title=\"Please wait\">Our files and {{ we }}{{ 'Tell us now' }}</p>"
                    "<input value=\"our value\"><script>var we;</script>", encoding="utf-8")
    found = [t for t in _template_texts(page) if FIRST_PERSON.search(t)]
    assert sorted(found) == ["Our files and ", "Please wait", "Tell us now", "our value"]
    assert UNDERSCORE_REPLACE.search("{{ x|replace('_', ' ') }}") and UNDERSCORE_REPLACE.search('{{ x.replace("_"," ") }}')
