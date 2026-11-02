"""Shared helpers for the page tests. Standard library plus tests/local/_dom.

Everything works on rendered HTML (what the seller's browser receives) except template_lint, which scans template
source for the patterns the shell contract forbids in flow templates (a bare <button>, a kind-less .btn, a
hard-coded disabled look, the hard-coded 'Fields marked Required' sentence, maxlength outside the date parts).
"""
from __future__ import annotations

import re
from pathlib import Path

from _dom import Node, parse


KIND_CLASSES = ("btn-primary", "btn-secondary", "btn-link")


def parse_html(html: str) -> Node:
    return parse(html)


def _classes(node: Node) -> set:
    return set(node.attrs.get("class", "").split())


def _controls(dom: Node):
    return [n for n in dom.walk() if n.tag == "button" or (n.tag == "a" and "btn" in _classes(n))]


def bare_buttons(html: str) -> list:
    """Every <button>, and every a.btn, that carries none of btn-primary / btn-secondary / btn-link."""
    return [n for n in _controls(parse(html)) if not _classes(n) & set(KIND_CLASSES)]


def primaries(html: str) -> list:
    return [n for n in parse(html).walk() if "btn-primary" in _classes(n)]


def assert_single_primary(html: str, allow_zero: bool = False) -> None:
    found = primaries(html)
    labels = [n.text() for n in found]
    if allow_zero:
        assert len(found) <= 1, f"more than one .btn-primary: {labels}"
    else:
        assert len(found) == 1, f"expected exactly one .btn-primary, found {labels}"


def main_nav_links(html: str) -> list:
    """[(text, href, aria-current or None)] of the header's nav[aria-label=Main], in document order."""
    nav = parse(html).find("nav", aria_label="Main")
    if nav is None:
        return []
    return [(a.text(), a.attrs.get("href"), a.attrs.get("aria-current")) for a in nav.find_all("a")]


def breadcrumb_items(html: str) -> list:
    """[(label, href or None)] of nav[aria-label=Breadcrumb] > ol > li; the current page has no href."""
    nav = parse(html).find("nav", aria_label="Breadcrumb")
    if nav is None:
        return []
    items = []
    for li in nav.find_all("li"):
        link = li.find("a")
        items.append((link.text() if link else li.text(), link.attrs.get("href") if link else None))
    return items


def back_links(html: str) -> list:
    """Anchors that are the old per-page 'back' navigation: .back-link, text starting 'Back to', or an arrow.
    A jump inside the same page (href starting with #) is not navigation away, so the package page's return anchors are not counted."""
    found = []
    for a in parse(html).find_all("a"):
        text = a.text()
        if a.attrs.get("href", "").startswith("#"):
            continue
        if "back-link" in _classes(a) or text.startswith(("Back to", "←", "←")):
            found.append(a)
    return found


def required_sentence_count(html: str) -> int:
    return len(re.findall(r"Fields marked Required must be completed", html))


def large_labels_ok(html: str) -> bool:
    """Each of the eight primary labels that appears as a control is large (btn-large) or primary (always large)."""
    from copilot.web.presentation import LARGE_LABELS
    for control in _controls(parse(html)):
        text = control.text()
        if any(text == label or text.startswith(label + " ") for label in LARGE_LABELS):
            if not _classes(control) & {"btn-large", "btn-primary"}:
                return False
    return True


_CLASS_ATTR = re.compile(r"""class\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def template_lint(paths) -> list:
    """['path:line problem', ...] for template source that breaks the shell contract."""
    problems = []
    for path in paths:
        path = Path(path)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            where = f"{path}:{number}"
            if re.search(r"<button\b", line):
                problems.append(f"{where} bare <button> (use the button or post_button macro)")
            for match in _CLASS_ATTR.finditer(line):
                tokens = set((match.group(1) or match.group(2) or "").split())
                if "btn" in tokens and not tokens & set(KIND_CLASSES):
                    problems.append(f"{where} class 'btn' without a kind")
            if re.search(r"\baria-disabled\b", line) or re.search(r"<[^>]*\sdisabled(?:\s|>|=|/)", line):
                problems.append(f"{where} disabled or aria-disabled in markup (list the fixes instead)")
            if "Fields marked Required" in line:
                problems.append(f"{where} hard-coded 'Fields marked Required' sentence (use required_note)")
            if re.search(r"\bmaxlength\b", line) and "date-part" not in line and "part" not in line:
                problems.append(f"{where} maxlength outside the date parts")
    return problems
