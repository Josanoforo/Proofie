"""Fixes after the automated walk on e4561dba (D-88): a sendable package page with the read-aloud entry on, the skip
link target, the words a seller reads on /outcome and /resolution.

Expected, written before the first run:
- a package that can be sent renders (200) with read_aloud_review on, and shows the entry;
- <main id="main"> can take focus (tabindex="-1"), so the skip link moves focus there on every page;
- /outcome and /resolution (appeal workspace, resolution, outcome on) do not say "snapshot".
"""
import re
from pathlib import Path

import pytest
from _dom import parse

from copilot import config
from test_ux_rc41_package import env, approved, page  # noqa: F401  (the fixture and helpers of the package tests)

pytestmark = [pytest.mark.usefixtures("web_mode")]


def _visible_text(html):
    return re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", "", html))


def test_a_sendable_package_page_renders_with_read_aloud_review_on(env, monkeypatch):
    c, db, token, _ = env
    monkeypatch.setenv("COPILOT_FEATURE_READ_ALOUD_REVIEW", "true")
    assert config.feature_enabled("read_aloud_review")
    import importlib
    from copilot.web import routes_package
    importlib.import_module("copilot.features.read_aloud_review.routes")      # what create_app does when the flag is on
    assert "read_aloud_review_text" in routes_package.templates.env.globals, "the package page's own template environment"
    pkg = approved(c, db, token)
    r = page(c, pkg)
    assert r.status_code == 200, r.text[:300]
    assert 'data-state="send-summary"' in r.text or "Tracking:" in r.text
    assert "read_aloud" in r.text.lower() or "Read the text aloud" in r.text or "voice" in r.text.lower()


def test_main_takes_focus_for_the_skip_link(env):
    c, db, token, _ = env
    from test_ux_rc41_package import DID
    html = c.get(f"/disputes/{DID}").text
    assert re.search(r'<main id="main"[^>]*tabindex="-1"', html)
    assert 'href="#main"' in html


@pytest.mark.parametrize("name", ["outcome/outcome.html", "resolution/resolution.html"])
def test_outcome_and_resolution_pages_say_nothing_a_seller_cannot_read(name):
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "copilot" / "web" / "templates" / "features" / name).read_text(encoding="utf-8")
    words = re.sub(r"\{[{%].*?[}%]\}", "", text)          # template expressions are code, not words
    assert "snapshot" not in words.lower()



def test_package_page_and_ledger_words_are_plain():
    root = Path(__file__).resolve().parents[2]
    page = (root / "copilot" / "web" / "templates" / "package_review.html").read_text(encoding="utf-8")
    words = re.sub(r"\{[{%].*?[}%]\}", "", page)
    for banned in ("snapshot", "readiness gaps", " cents"):
        assert banned not in words.lower(), banned
    assert "<time>{{ fact.at }}</time>" not in page and "fact.at|when" in page
    ledger = (root / "copilot" / "features" / "ledger" / "service.py").read_text(encoding="utf-8")
    assert "snapshot" not in ledger.lower()
    dossier = (root / "copilot" / "features" / "evidence_dossier" / "service.py").read_text(encoding="utf-8")
    assert "snapshot" not in dossier.lower()


def test_shipment_summary_says_fill_in_until_something_is_saved():
    page = (Path(__file__).resolve().parents[2] / "copilot" / "web" / "templates" / "package_review.html").read_text(encoding="utf-8")
    assert "'Change the shipment details' if shipment_filled and not shipment_open else 'Fill in the shipment details'" in page


def test_a_new_package_with_nothing_saved_reads_fill_in(env):
    c, db, token, _ = env
    from test_ux_rc41_package import new_package
    pkg = new_package(c, token)
    html = page(c, pkg).text
    assert "<summary>Fill in the shipment details</summary>" in html
