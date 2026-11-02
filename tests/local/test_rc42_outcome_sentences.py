"""The outcome page's checks list: the rule label is a sentence of its own and no rendered sentence is over 25 words.

The label of a check can be long ("Delivery place matches the address on the PayPal transaction", 9 words); joined to the
state word and the checklist's own message it made one sentence of 26 words. The list is rendered here from the real
checklist over every synthetic case, for both rule sets (MX and US), in its three shapes: problems, information after a
confirmed send, and a claim under review. Offline: no PayPal call, no model call.
"""
import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot.core.checklist import evaluate, load_rules
from copilot.web import routes_disputes
from copilot.web.dispute_state import REVIEWING_NEUTRAL_TEXT

_spec = importlib.util.spec_from_file_location("m1_loader_sentences", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)

LIMIT = 25
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
LONG_LABEL = "Delivery place matches the address on the PayPal transaction"


def _missing_now(case, country):
    checks = evaluate(m1.build_context(case), m1.build_docs(case), load_rules(country=country), today=m1.today_of(case))
    return [{"id": c.id, "label": c.label_for_seller, "state": c.state, "message": c.seller_message, "action": c.action}
            for c in checks if c.state in ("missing", "conflict")]


def _render(missing_now, after_send=False, reviewing=False):
    attempts = [{"state": "confirmed"}] if after_send else []
    gaps = SimpleNamespace(at_send_recorded=False, at_send_note="not recorded at send time", missing_at_send=(),
                           missing_now=missing_now, addable=(), limitations=())
    return routes_disputes.templates.env.get_template("features/outcome/_gaps.html").render(
        file={"send_attempts": attempts}, read=SimpleNamespace(links=[]), gaps=gaps, rc3_reviewing=reviewing)


def _rows(html):
    """The visible text of each row of the 'Now' list (tags removed, the aria-hidden glyph pruned)."""
    now = html.split('data-state="missing-now"', 1)[1].split("</ul>", 1)[0]
    rows = []
    for li in now.split("<li")[1:]:
        li = re.sub(r'<span aria-hidden="true">.*?</span>', "", li.split("</li>", 1)[0], flags=re.S)
        rows.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", li.split(">", 1)[1])).replace(" .", ".").replace(" :", ":").strip())
    return rows


def _cases():
    return [(name, country) for country in ("MX", "US") for name in m1.case_names()]


@pytest.mark.parametrize("shape", ["problems", "after_send", "reviewing"])
@pytest.mark.parametrize("name,country", _cases())
def test_no_sentence_in_the_checks_list_is_over_25_words(name, country, shape):
    missing = _missing_now(m1.load_case(name), country)
    html = _render(missing, after_send=shape == "after_send", reviewing=shape == "reviewing")
    rows = _rows(html)
    assert len(rows) == len(missing)
    for row in rows:
        for sentence in SENTENCE_BREAK.split(row):
            assert len(sentence.split()) <= LIMIT, (name, country, shape, len(sentence.split()), sentence)


@pytest.mark.parametrize("country", ["MX", "US"])
def test_the_longest_label_of_each_rule_set_stays_inside_the_limit_with_the_longest_message(country):
    rules = load_rules(country=country)
    longest_message = max((m["message"] for name in m1.case_names() for m in _missing_now(m1.load_case(name), country)),
                          key=lambda s: len(s.split()), default="")
    assert any(c.get("label") == LONG_LABEL for c in rules.checks.values())      # the 9-word label is in both rule sets
    row = _rows(_render([{"id": "destination", "label": LONG_LABEL, "state": "conflict", "message": longest_message, "action": "review_difference"}]))[0]
    assert row.startswith(LONG_LABEL + ". ")                                       # the label is a sentence of its own
    assert all(len(s.split()) <= LIMIT for s in SENTENCE_BREAK.split(row)), row


def test_a_problem_row_reads_label_then_state_and_message_as_two_sentences():
    row = _rows(_render([{"id": "destination", "label": LONG_LABEL, "state": "conflict",
                          "message": "The address on the documents differs from the one on the transaction.", "action": "review_difference"}]))[0]
    assert row == LONG_LABEL + ". Does not match: The address on the documents differs from the one on the transaction."


def test_the_claim_under_review_row_keeps_its_neutral_text():
    missing = [{"id": "carrier", "label": "Carrier", "state": "missing", "message": "Add the carrier.", "action": "add_document"}]
    row = _rows(_render(missing, reviewing=True))[0]
    assert REVIEWING_NEUTRAL_TEXT in row and "Add the carrier" not in row and row.startswith("Carrier. ")
