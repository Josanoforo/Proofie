"""A tiny DOM for the UX-state tests: the standard library only, no new dependency. Tests
assert on elements, attributes and containment - "this alert has this action inside it" - never only on
whether some words appear somewhere in the page.
"""
from __future__ import annotations

from html.parser import HTMLParser

_VOID = {"input", "br", "hr", "img", "meta", "link"}


class Node:
    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list = []   # Node or str

    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def find_all(self, tag: str | None = None, **attrs) -> list["Node"]:
        """attrs use underscores for dashes: find_all("div", data_state="empty"). `class_` is the one
        exception: it is the HTML class attribute, matched by TOKEN (class_="btn" matches class="btn x")."""
        classes = attrs.pop("class_", None)
        want = {k.replace("_", "-"): v for k, v in attrs.items()}
        return [n for n in self.walk()
                if (tag is None or n.tag == tag)
                and all(n.attrs.get(k) == v for k, v in want.items())
                and (classes is None or classes in n.attrs.get("class", "").split())]

    def find(self, tag: str | None = None, **attrs) -> "Node | None":
        found = self.find_all(tag, **attrs)
        return found[0] if found else None

    def text(self) -> str:
        parts = [c if isinstance(c, str) else c.text() for c in self.children]
        return " ".join(" ".join(parts).split())

    def inside(self, tag: str) -> bool:
        node = self.parent
        while node is not None:
            if node.tag == tag:
                return True
            node = node.parent
        return False


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    return builder.root


def state(dom: Node, name: str) -> Node | None:
    """The element carrying data-state=<name>, if any."""
    return dom.find(data_state=name)


def actions_in(node: Node) -> list[tuple[str, str]]:
    """(kind, target) for every link or form action inside an alert: ('a', href) / ('form', action)."""
    out = [("a", n.attrs.get("href", "")) for n in node.find_all("a")]
    out += [("form", n.attrs.get("action", "")) for n in node.find_all("form")]
    return out
