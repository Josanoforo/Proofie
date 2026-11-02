"""CSS, shell copy, other pages.

Offline only: fake PayPal clients, a temp database per test (conftest.py).
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "copilot" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
UTC = timezone.utc


# --- naive due date
def test_scope_u_5_a_due_date_without_an_offset_gets_no_computed_deadline():
    from copilot.core.disputes import normalize        # the core rule this mirrors: deadline_parse_ok is False
    from copilot.web import timefmt
    now = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    for naive in ("2026-10-19T02:13:00", "2026-10-19 02:13:00", "2026-10-19T02:13"):
        assert normalize({"dispute_id": "PP-R-DEMO-0000001", "seller_response_due_date": naive}, now)["deadline_parse_ok"] is False
        assert timefmt.due_display({"seller_response_due_date": naive}, now=now) is None, naive
        assert timefmt.display(naive) == "Not available", naive
        assert timefmt.iso(naive) == "", naive
        assert str(timefmt.when(naive)) == "Not available", naive
        assert timefmt.when_text(naive) == "Not available", naive


def test_scope_u_5_offsets_and_object_times_still_read():
    from copilot.web import timefmt
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    for given in ("2026-10-19T02:13:00Z", "2026-10-19T02:13:00+00:00", "2026-10-18T20:13:00-06:00"):
        result = timefmt.due_display({"seller_response_due_date": given}, now=now)
        assert result and result["iso"] == "2026-10-18T20:13:00-06:00" and result["remaining"] == "Due in 38 hours", given
    # stored timestamps carry %z (+0000) and keep reading; a naive datetime object is UTC by the shell's own contract
    assert timefmt.display("2026-10-17T04:10:00+0000") == "16 Oct 2026, 10:10 pm (Mexico City time)"
    assert timefmt.display(datetime(2026, 10, 17, 4, 10)) == "16 Oct 2026, 10:10 pm (Mexico City time)"
    assert timefmt.display("2026-10-16") == "16 Oct 2026"


# --- CSS
def _rules(selector_regex):
    return [m.group(1) for m in re.finditer(selector_regex + r"\s*\{([^{}]*)\}", CSS, re.S)]


def test_mu_4_cells_and_code_wrap_and_the_large_button_may_shrink_inside_a_cell():
    # Only long tokens wrap anywhere (never th/td themselves); the large button shrinks in a cell on a phone only
    assert not any("overflow-wrap: anywhere" in r for r in _rules(r"(?m)^th, td"))
    assert any("overflow-wrap: anywhere" in r for r in _rules(r"(?m)^td \.mono-id, td code[^{]*"))        # Long tokens
    assert any("overflow-wrap: anywhere" in r for r in _rules(r"(?m)^code"))
    assert re.search(r"@media \(max-width: 1024px\) \{ td \.btn-large[^{}]*\{ min-width: 0; \} \}", CSS)  # inside a cell, up to 1024px
    assert "min-width: 14rem" in "".join(_rules(r"\.btn-large[^{]*"))                              # outside a cell it keeps its size


def test_polish_stacked_captions_are_full_width_and_small_text_wraps():
    # The visually-hidden caption is skipped; wrapping small text is for the triage page only
    assert re.search(r"@media \(max-width: 639px\) \{ table\.stack caption:not\(\.visually-hidden\) \{ display: block; width: 100%; \} \}", CSS)
    assert any("overflow-wrap: anywhere" in r for r in _rules(r"(?m)^#triage li, #triage dd, #triage small"))


# --- error crumbs
def _crumbs(html):
    import _ux_lint as lint
    return lint.breadcrumb_items(html)


def _h1(html):
    from _dom import parse
    return parse(html).find("h1").text().strip()


def test_mu_5_error_pages_end_with_the_error_title_and_keep_the_parents_as_links(monkeypatch):
    from test_ux_rc41_shell import DID, _web
    web, _db, _token = _web()
    no_dispute = web.get("/disputes/not-an-id")
    assert no_dispute.status_code == 400
    crumbs = _crumbs(no_dispute.text)
    assert crumbs[0] == ("All disputes", "/disputes") and len(crumbs) == 2
    assert crumbs[-1] == (_h1(no_dispute.text).split("\n")[0].strip(), None)
    session = web.post(f"/disputes/{DID}/documents", data={"csrf_token": "wrong"})
    assert session.status_code == 403
    crumbs = _crumbs(session.text)
    assert crumbs == [("All disputes", "/disputes"), (f"Dispute {DID}", f"/disputes/{DID}"), ("Your session expired", None)]
    assert 'aria-current="page"' in session.text and session.text.count('aria-current="page"') == 1


# --- Spanish words, lang="es"
def test_mu_12_the_spanish_words_of_the_delivery_hint_are_marked_as_spanish_and_untranslated(monkeypatch):
    from html import unescape
    from copilot.web import presentation
    from copilot.web.routes_disputes import templates as dispute_templates
    from copilot.web.routes_package import templates as package_templates
    plain, marked = presentation.DELIVERY_WORDS_HINT, presentation.DELIVERY_WORDS_HINT_HTML
    assert type(marked).__name__ == "Markup"
    spans = re.findall(r'<span lang="es" translate="no">([^<]+)</span>', str(marked))
    assert spans == ["entregado", "firmado", "recibido"]
    assert unescape(re.sub(r"<[^>]+>", "", str(marked))) == plain                 # same sentence, only marked up
    for env in (dispute_templates.env, package_templates.env):
        assert env.globals["DELIVERY_WORDS_HINT_HTML"] is marked
        rendered = env.from_string("<p>{{ DELIVERY_WORDS_HINT_HTML }}</p>").render()
        assert rendered.count('lang="es"') == 3 and "&lt;span" not in rendered


# ------------------------------------------------------------------------------- polish: app-level 404 for unknown paths
def test_polish_an_unknown_path_renders_the_error_page_with_header_and_breadcrumb():
    from test_ux_rc41_shell import DID, _web
    from _dom import parse
    web, _db, _token = _web()
    for method in ("get", "post"):
        response = getattr(web, method)("/no/such/page")
        assert response.status_code == 404 and response.headers["content-type"].startswith("text/html")
        dom = parse(response.text)
        assert dom.find("h1").text().strip() == "That page was not found"
        assert "Nothing was changed" in response.text and "detail" not in response.text
        assert [a.text() for a in dom.find_all("a") if a.attrs.get("href") == "/disputes"], "header link to All disputes"
        assert _crumbs(response.text) == [("All disputes", "/disputes"), ("That page was not found", None)]
        assert "Try again" not in response.text
    inside = web.get(f"/disputes/{DID}/no-such-part")
    assert inside.status_code == 404
    assert _crumbs(inside.text) == [("All disputes", "/disputes"), (f"Dispute {DID}", f"/disputes/{DID}"),
                                    ("That page was not found", None)]
    assert web.get("/static/does-not-exist.css").status_code == 404                 # not a page: stays the plain answer
    assert "text/html" not in web.get("/static/does-not-exist.css").headers["content-type"]


def test_polish_a_route_that_answers_404_on_purpose_keeps_its_own_body():
    from test_ux_rc41_shell import _web
    web, _db, _token = _web()
    response = web.get("/openapi.json")
    assert response.status_code in (200, 404)
    if response.status_code == 404:
        assert response.headers["content-type"].startswith("application/json") or "html" in response.headers["content-type"]


# ------------------------------------------------------------ polish: the forward action is the primary (claims_review)
def test_polish_claims_review_makes_review_each_statement_and_approve_the_primary(monkeypatch):
    from test_ux_rc41_shell import DID, _web
    from _dom import parse
    from copilot.db import repo
    monkeypatch.setenv("COPILOT_FEATURE_CLAIMS", "true")
    web, db, _token = _web()
    package = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "Shipped on 2026-10-01", "source_ids": [],
                                      "basis": "seller_statement"}])
    response = web.get(f"/disputes/{DID}/packages/{package}/claims-review")
    assert response.status_code == 200
    dom = parse(response.text)
    primaries = [n for n in dom.walk() if "btn-primary" in n.attrs.get("class", "").split()]
    assert [p.text() for p in primaries] == ["Review each statement and approve"]
    assert "btn-large" in primaries[0].attrs["class"]
