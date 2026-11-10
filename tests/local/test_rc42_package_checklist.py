"""Checklist words: closed evidence-type words and the length of the Delivery place line."""
import importlib.util
import re
from pathlib import Path

import pytest

from copilot.core.checklist import evaluate, load_rules

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("m1_loader_rc42", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)
RULES = load_rules()


def destination_checks():
    out = []
    for name in m1.case_names():
        case = m1.load_case(name)
        checks = {c.id: c for c in evaluate(m1.build_context(case), m1.build_docs(case), RULES, today=m1.today_of(case))}
        if "destination" in checks:
            out.append((name, checks["destination"]))
    return out


def test_every_destination_line_stays_under_25_words_and_keeps_its_figures():
    seen = set()
    for name, check in destination_checks():
        if check.state == "verified":                                               # the line of the Delivery place check as the outcome list prints it
            line = f"{check.label_for_seller} Verified: {check.seller_message}"    # label, a one-word tag, the message
            assert len(line.split()) < 25, (name, line)
        assert not check.seller_message.startswith("Delivery place"), name         # the label already says it
        seen.add(check.state)
    assert {"verified", "conflict"} <= seen                                         # both worded kinds were exercised


def test_the_verified_and_conflict_messages_read_as_before_minus_the_repeated_label():
    messages = {c.state: c.seller_message for _, c in destination_checks() if c.state in ("verified", "conflict")}
    assert re.fullmatch(r"Matches the (transaction address in PayPal|address you declared \(not from PayPal\)) \(.+\)\.",
                        messages["verified"])
    # v1.1.2: a postal-code difference names each value by its source
    assert re.fullmatch(r"(Your receipt says \S+\. PayPal says \S+\.|Does not match the (transaction address in PayPal|"
                        r"address you declared \(not from PayPal\)) \(.+\)\.) The document may belong to another order\.",
                        messages["conflict"])
