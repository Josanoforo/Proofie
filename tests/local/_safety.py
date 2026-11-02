"""Safety facts of the case views (rulebook v2, V5 and section 5): a test protects a fact, not the visibility of a sentence.

The five facts carry a stable hook, `data-safety="<key>"`, and each is said once per view in one short line. The long wording
lives in the toggletip content, which stays in the DOM. `assert_safety_fact` checks that and nothing about layout.
"""
from _dom import parse

KEYS = ("due-date", "nothing-sent", "read-back", "files-local", "assistant-only")


def safety_nodes(html: str, key: str) -> list:
    return [n for n in parse(html).walk() if n.attrs.get("data-safety") == key]


def assert_safety_fact(html: str, key: str, short: str | None = None, long: str | None = None) -> None:
    """(a) the fact has exactly one hook on the page, (b) its text starts with `short` when given, and (c) `long` is
    in that element's text (the toggletip body counts: it is part of the element and stays in the DOM when closed)."""
    nodes = safety_nodes(html, key)
    assert len(nodes) == 1, f"{key}: {len(nodes)} elements carry the hook, expected one"
    text = " ".join(nodes[0].text().split())
    if short is not None:
        assert text.startswith(short), f"{key}: {text!r} does not start with {short!r}"
    if long is not None:
        assert long in text, f"{key}: the long wording is missing from the element"
