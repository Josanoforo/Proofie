"""A route function called from another module takes its form fields by keyword.

resolution.routes.preview gained a parameter before `client` (7e6e52e8) and two callers that passed `client` by position
sent it into the wrong field, so the app answered 500. Scanned: every call to `preview` imported from
copilot.features.resolution.routes; only `request` and `dispute_id` may be positional."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POSITIONAL_ALLOWED = 2


def _calls_into_resolution_preview():
    found = []
    for path in sorted((ROOT / "copilot").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = any(isinstance(n, ast.ImportFrom) and n.module == "copilot.features.resolution.routes"
                       and any(a.name == "preview" and a.asname is None for a in n.names) for n in ast.walk(tree))
        if not imported:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "preview":
                found.append((path.relative_to(ROOT).as_posix(), node.lineno, len(node.args)))
    return found


def test_the_scan_sees_the_known_callers():
    files = {f for f, _l, _n in _calls_into_resolution_preview()}
    assert {"copilot/features/buyer_note/routes.py", "copilot/features/local_autopilot/routes.py"} <= files


def test_preview_is_called_with_keywords_after_request_and_dispute_id():
    bad = [f"{f}:{line} ({n} positional)" for f, line, n in _calls_into_resolution_preview() if n > POSITIONAL_ALLOWED]
    assert bad == [], "pass the form fields and client by keyword: " + ", ".join(bad)
