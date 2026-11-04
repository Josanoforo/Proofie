"""Regression pins for accessibility checks that pass on this release and must keep passing.

Offline only: fake PayPal client, a temp database per test (conftest.py), DOM checks through _dom / _ux_lint (imported, never edited), both web
modes through the web_mode fixture. The pins are structural (what the in-house harness found true); they are not a claim of axe-core or screen-reader
testing. Findings that FAIL on the baseline (captions, novalidate, heading jump on the tracking result, copy lint) are pinned by the lane that fixes them.
"""
import json
import re
import secrets
from pathlib import Path

import pytest
from _dom import parse
from _hosted import authenticated_client, set_test_csrf

from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web import routes_disputes
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "copilot" / "web"
TEMPLATES = WEB / "templates"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE1 = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"


class FakeClient:
    def call(self, method, path, op, **kw):
        if op == "show_dispute":
            return WAITING
        if op == "list_disputes":
            return LIST_PAGE1
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _pages(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")      # the fulfilment router is mounted when the app is created
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, secrets.token_urlsafe(32))
    db = config.db_path()
    package = repo.insert_package(db, DID, "human_edit", True, [])
    paths = {"inbox": "/disputes", "dispute": f"/disputes/{DID}", "package": f"/disputes/{DID}/packages/{package}", "tracking list": "/fulfilment"}
    out = {}
    for name, path in paths.items():
        r = web.get(path)
        assert r.status_code == 200, name
        out[name] = r.text
    r404 = web.get("/no-such-page")
    assert r404.status_code == 404
    out["404"] = r404.text
    return out


def _levels(dom):
    return [int(n.tag[1]) for n in dom.walk() if re.fullmatch(r"h[1-6]", n.tag)]


# ------------------------------------------------------------------------------ rendered pages (both modes)
def test_each_judge_page_has_one_h1_and_headings_never_skip_a_level(web_mode, monkeypatch):
    for name, html in _pages(monkeypatch).items():
        levels = _levels(parse(html))
        assert levels.count(1) == 1, (name, levels)
        for before, after in zip(levels, levels[1:]):
            assert after <= before + 1, (name, "heading level jumps", before, after)


def test_each_judge_page_has_unique_ids_one_main_a_title_and_a_language(web_mode, monkeypatch):
    for name, html in _pages(monkeypatch).items():
        dom = parse(html)
        ids = [n.attrs["id"] for n in dom.walk() if n.attrs.get("id")]
        assert len(ids) == len(set(ids)), (name, [i for i in ids if ids.count(i) > 1])
        assert len(dom.find_all("main")) == 1, name
        assert re.search(r"<title>[^<]+</title>", html), name
        assert re.search(r'<html[^>]*\blang="[a-z]{2}', html), name


def test_each_judge_page_starts_with_a_skip_link_whose_target_exists(web_mode, monkeypatch):
    for name, html in _pages(monkeypatch).items():
        dom = parse(html)
        first = next(n for n in dom.walk() if n.tag == "a" and n.attrs.get("href"))
        assert first.attrs.get("class") == "skip-link" and first.attrs["href"] == "#main", name
        assert dom.find(None, id="main") is not None, name


def test_every_form_control_on_the_judge_pages_has_a_label(web_mode, monkeypatch):
    for name, html in _pages(monkeypatch).items():
        dom = parse(html)
        labelled = {n.attrs["for"] for n in dom.find_all("label") if n.attrs.get("for")}
        for n in dom.walk():
            if n.tag in ("input", "select", "textarea") and n.attrs.get("type") not in ("hidden", "submit", "button"):
                assert n.attrs.get("aria-label") or n.attrs.get("id") in labelled or n.inside("label"), (name, n.tag, n.attrs.get("name"))


# ------------------------------------------------------------------------------ source pins (shell, CSS)
def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_template_uses_a_positive_tabindex():
    for path in _templates():
        assert not re.search(r'tabindex="[1-9]', path.read_text(encoding="utf-8")), path.name


def test_the_viewport_meta_keeps_zoom_available():
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    meta = re.search(r'<meta name="viewport" content="([^"]*)"', base).group(1)
    assert "user-scalable" not in meta and "maximum-scale" not in meta and "width=device-width" in meta


def test_a_sticky_or_fixed_rule_always_comes_with_scroll_padding_top():
    """WCAG 2.4.11: a sticky header must not hide the focused control. The header is sticky on wide screens, so the stylesheet must also set scroll-padding-top."""
    sticky = re.search(r"position:\s*(sticky|fixed)", CSS)
    if sticky:
        assert re.search(r"scroll-padding-top\s*:", CSS), "sticky/fixed positioning without scroll-padding-top"


def test_the_stylesheet_never_removes_a_focus_outline_without_a_replacement():
    for rule in re.finditer(r"([^{}]+)\{([^{}]*\boutline\s*:\s*(?:none|0)\b[^{}]*)\}", CSS):
        assert False, f"outline removed in: {rule.group(1).strip()}"
    assert re.search(r":focus-visible\s*\{[^}]*outline:\s*3px solid", CSS)


def test_forced_colours_are_never_switched_off():
    # One exception (BRAND-SURFACES, 2026-11-02): the header mark is a decorative background image that must keep its two brand
    # colours in forced-colours mode. Nothing else may opt out.
    rest = CSS.replace("@media (forced-colors: active) { .brand-mark { forced-color-adjust: none; } }", "")
    assert "forced-color-adjust" not in rest and CSS.count("forced-color-adjust") == 1


def test_the_error_summary_macro_keeps_its_focus_contract():
    macro = (TEMPLATES / "_form_fields.html").read_text(encoding="utf-8")
    summary = re.search(r'<section class="error-summary"[^>]*>', macro).group(0)
    assert 'role="alert"' in summary and 'tabindex="-1"' in summary and "autofocus" in summary and "aria-labelledby" in summary


# ------------------------------------------------------------------------------ the harness stays out of the suite
def test_the_ux_checks_harness_is_never_collected_by_pytest():
    harness = ROOT / "internal" / "ux-checks"
    if not harness.exists():
        pytest.skip("harness not present in this tree")
    for path in harness.glob("*.py"):
        assert not re.fullmatch(r"test_.*\.py|.*_test\.py", path.name), path.name
        assert not re.search(r"^def test_", path.read_text(encoding="utf-8"), re.M), path.name
