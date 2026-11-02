"""The approved delta of copilot/web/templates/package_review.html since the frozen fixture
(package_review-frozen-3.html, frozen by hash in the byte-identity tests). Each edit is a named replacement of whole lines;
applying them to the frozen fixture must give EXACTLY the template in the tree, so any other edit to the template fails
the test and needs its own entry (and a reason) here."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "features" / "active_approval" / "fixtures"
DELTA = FIXTURES / "package_review-frozen-3-delta.json"
FROZEN = FIXTURES / "package_review-frozen-3.html"
TEMPLATE = ROOT / "copilot" / "web" / "templates" / "package_review.html"


def edits() -> list:
    return json.loads(DELTA.read_text(encoding="utf-8"))


def apply(frozen: str) -> str:
    lines = frozen.split("\n")
    for edit in reversed(edits()):                       # from the bottom up, so earlier line numbers stay valid
        at = edit["at"]
        old = edit["old"].split("\n") if edit["old"] != "" else []
        new = edit["new"].split("\n") if edit["new"] != "" else []
        assert lines[at:at + len(old)] == old, "approved edit does not apply: " + edit["id"]
        lines[at:at + len(old)] = new
    return "\n".join(lines)


def current_template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")
