"""The public documents say the same thing about the feature settings as .env.example does.

Read from .env.example: how many COPILOT_FEATURE_* lines it has, how many are true, and which are false. Then, in README.md,
docs/JUDGE-RUN.md, docs/DEVPOST.md and docs/CONFORMANCE.md (a document the kit does not carry is skipped):
- every "N feature settings" / "N settings" sentence gives that count, and every "M enabled" / "M of them enabled" next to it gives
  the true count;
- every sentence that lists settings as `COPILOT_FEATURE_X=false` names exactly the false ones, and every `COPILOT_FEATURE_X=true`
  or `=false` token agrees with the line of the same name in .env.example;
- README.md and docs/JUDGE-RUN.md each carry the count sentence at least once.
A lane that adds or flips a setting has to update the documents in the same change, or this test fails."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ["README.md", "docs/JUDGE-RUN.md", "docs/DEVPOST.md", "docs/CONFORMANCE.md"]
MUST_CARRY = ["README.md", "docs/JUDGE-RUN.md"]
LINE = re.compile(r"^(COPILOT_FEATURE_[A-Z0-9_]+)=(\w+)\s*$", re.M)
COUNT = re.compile(r"\b(\d+) (?:feature )?settings\b")
ENABLED = re.compile(r"\b(\d+)(?: of them)? enabled\b")
TOKEN = re.compile(r"`(COPILOT_FEATURE_[A-Z0-9_]+)=(\w+)`")


def example():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return {name: value.lower() for name, value in LINE.findall(text)}


def doc_text(rel):
    path = ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} is not carried in this tree")
    return path.read_text(encoding="utf-8")


def test_the_example_file_is_what_this_test_reads():
    flags = example()
    assert len(flags) >= 60 and set(flags.values()) <= {"true", "false"}


@pytest.mark.parametrize("rel", DOCS)
def test_every_count_the_document_gives_is_the_count_of_the_example_file(rel):
    flags = example()
    total, enabled = len(flags), sum(v == "true" for v in flags.values())
    text = doc_text(rel)
    for found in COUNT.finditer(text):
        window = text[found.start(): found.start() + 120]
        if int(found.group(1)) < 20:      # "ten settings", "3 settings" of some other list are not this count
            continue
        assert int(found.group(1)) == total, f"{rel}: '{found.group(0)}' but .env.example has {total} COPILOT_FEATURE_* lines"
        match = ENABLED.search(window)
        if match:
            assert int(match.group(1)) == enabled, f"{rel}: '{match.group(0)}' but {enabled} lines of .env.example are true"


@pytest.mark.parametrize("rel", DOCS)
def test_every_setting_the_document_shows_with_a_value_has_that_value_and_every_off_list_is_the_off_set(rel):
    flags = example()
    off = {name for name, value in flags.items() if value == "false"}
    text = doc_text(rel)
    for name, value in TOKEN.findall(text):
        if name in flags:
            assert flags[name] == value.lower(), f"{rel}: `{name}={value}` but .env.example has {name}={flags[name]}"
    for found in COUNT.finditer(text):
        sentence = re.split(r"\.\s", text[found.start(): found.start() + 600], maxsplit=1)[0]
        if int(found.group(1)) >= 20 and "=false" in sentence:
            listed = {name for name, value in TOKEN.findall(sentence) if value.lower() == "false"}
            assert listed == off, f"{rel}: the off list names {sorted(listed)} but .env.example has {sorted(off)} off"


@pytest.mark.parametrize("rel", MUST_CARRY)
def test_the_readme_and_the_judge_guide_carry_the_count_sentence(rel):
    text = doc_text(rel)
    flags = example()
    assert any(int(m.group(1)) == len(flags) for m in COUNT.finditer(text)), f"{rel} does not state the {len(flags)} settings"
    assert any(int(m.group(1)) == sum(v == "true" for v in flags.values()) for m in ENABLED.finditer(text)), \
        f"{rel} does not state the enabled count"


SITTINGS_SENTENCE = ("A run from scratch asks two sittings of you: your sandbox credentials and one setup command. The sandbox disputes are "
                     "opened for you by one command on the seller side; if you prefer the buyer's own path, that is one more sitting as the "
                     "sandbox buyer.")


@pytest.mark.parametrize("rel", ["README.md", "docs/JUDGE-RUN.md"])
def test_the_sittings_sentence_is_strategys_wording_of_record_word_for_word(rel):
    """D-111: the wording of record for how many sittings a run asks, in both public documents. The buyer's own path is the alternative:
    the old 'three sittings' sentence must not come back."""
    text = " ".join((ROOT / rel).read_text(encoding="utf-8").split())
    assert SITTINGS_SENTENCE in text
    assert "asks three sittings" not in text


SITTINGS_LIST = ("(1) your sandbox credentials; (2) one setup command (`tools/sandbox_seed.py`); the dispute then comes from one command on the "
                 "seller side (`tools/sandbox_card.py`), or, on the buyer's own path, (3) one more sitting as the sandbox buyer.")


def test_the_numbered_list_of_the_sittings_is_strategys_wording_of_record_word_for_word():
    """D-111: the list that follows the sittings sentence in JUDGE-RUN.md's 'Your own hands' paragraph, so the two never contradict."""
    text = " ".join((ROOT / "docs/JUDGE-RUN.md").read_text(encoding="utf-8").split())
    assert SITTINGS_LIST in text
    assert text.index(SITTINGS_SENTENCE) < text.index(SITTINGS_LIST) < text.index("You type any password yourself")
    assert "(3) One sitting as the sandbox buyer: approve the sale" not in text           # the old item (3) must not come back
