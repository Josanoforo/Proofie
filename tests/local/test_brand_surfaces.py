"""BRAND-SURFACES (2026-11-02): the brand reaches every product surface, and the pieces that cannot be seen in a screenshot
are pinned: favicon links, the static files behind the hosted gate, the header and print CSS, the evidence-package print
view colours and contrast, Open Graph, titles and the footer mark of the buyer's status page."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _hosted import TEST_ACCESS_CODE  # noqa: F401
from copilot.app import create_app
from test_design_contrast import ratio

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "copilot" / "web" / "templates"
BRAND = ROOT / "copilot" / "web" / "static" / "brand"
CSS = (ROOT / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
FILES = {
    "proofie-mark.svg": "image/svg+xml", "proofie-mark-on-ink.svg": "image/svg+xml", "proofie-mark-mono-ink.svg": "image/svg+xml",
    "favicon-32.png": "image/png", "apple-touch-icon.png": "image/png", "proofie-social-1280x640.png": "image/png",
}
ICON_LINKS = ('<link rel="icon" type="image/svg+xml" href="/static/brand/proofie-mark.svg">',
              '<link rel="icon" type="image/png" sizes="32x32" href="/static/brand/favicon-32.png">',
              '<link rel="apple-touch-icon" href="/static/brand/apple-touch-icon.png">')


def _read(rel: str) -> str:
    return (TEMPLATES / rel).read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S)
    assert match, selector
    return match.group(1)


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    return TestClient(create_app(), base_url="https://demo.example.test", client=("10.0.0.1", 1))


# ---- E1 favicon ------------------------------------------------------------------------------------------------------

def test_e1_no_template_keeps_the_empty_data_favicon():
    offenders = [str(p.relative_to(TEMPLATES)) for p in TEMPLATES.rglob("*.html") if 'href="data:,"' in p.read_text(encoding="utf-8")]
    assert offenders == []


@pytest.mark.parametrize("rel", ["_base.html", "features/status_link/public.html"])
def test_e1_base_and_status_page_carry_the_three_icon_links(rel):
    text = _read(rel)
    for link in ICON_LINKS:
        assert text.count(link) == 1, (rel, link)


# ---- E2 static files, also behind the hosted gate without a cookie -------------------------------------------------

@pytest.mark.parametrize("name,ctype", sorted(FILES.items()))
def test_e2_brand_files_are_served_with_the_right_type_behind_the_gate(hosted, name, ctype):
    assert (BRAND / name).is_file()
    response = hosted.get(f"/static/brand/{name}", follow_redirects=False)       # hosted gate on, no access cookie
    assert response.status_code == 200 and response.headers["content-type"].split(";")[0] == ctype
    assert response.content == (BRAND / name).read_bytes()                              # the file as it is on disk (no hash or size pinned)
    if name == "proofie-social-1280x640.png":
        assert response.content[:4] == b"\x89PNG" and int.from_bytes(response.content[16:20], "big") == 1280 and int.from_bytes(response.content[20:24], "big") == 640


# ---- E3 / E4 header, rails and the mark ------------------------------------------------------------------------------

def test_e3_case_workspace_header_uses_the_header_token_and_keeps_the_split_rule():
    assert "#123a70" not in CSS.lower()
    rule = _rule("body:has(.case-primary-nav) .site-header")
    assert "background: var(--header-bg)" in rule and "border-bottom: 0" not in rule and "border-image" not in rule
    assert "padding-top: 0" in rule and "padding-bottom: 0" in rule                # the compact band stays
    assert "border-bottom: 4px solid var(--brand)" in _rule(".site-header") and "linear-gradient" in _rule(".site-header")
    assert len(re.findall(r"border-top: 5px solid var\(--ink\)", CSS)) == 3        # the rails at app.css 774, 805 and 852
    assert "--ink: #102a43;" in CSS


def test_e4_header_mark_is_the_ink_svg_as_a_24px_background():
    rule = _rule(".brand-mark")
    assert 'url("/static/brand/proofie-mark-on-ink.svg")' in rule and "24px" in rule
    assert "border-image" not in rule and ".brand-mark::after" not in CSS
    assert re.search(r"@media \(forced-colors: active\) \{ \.brand-mark \{ forced-color-adjust: none; \} \}", CSS)
    assert 'class="brand-mark" aria-hidden="true"' in _read("_base.html")


# ---- E5 print CSS ----------------------------------------------------------------------------------------------------

def test_e5_print_block_hides_chrome_and_prints_the_header_in_ink():
    block = CSS[CSS.index("(pages that extend _base)"):CSS.index("case pages: one content edge")]
    assert "@media print" in block
    hide = re.search(r"(\.site-nav[^{]*)\{[^}]*display: none", block)
    assert hide
    for selector in (".site-nav", ".site-menu", ".env-tag", ".skip-link", "button"):
        assert selector in hide.group(1)
    assert re.search(r"\.site-header \{ background: none; color: #102a43;", block)
    assert 'url("/static/brand/proofie-mark-mono-ink.svg")' in block
    footer = re.findall(r"\.site-footer|\.hosted-demo-note", block)
    assert footer == []                                                           # the footer and the demo note are NOT hidden


# ---- E6 / E7 print views ---------------------------------------------------------------------------------------------

def test_e6_package_print_uses_the_brand_values_and_keeps_contrast():
    text = _read("features/package_print/print.html")
    for old in ("#17212b", "#0b4aa8", "#526779"):
        assert old not in text
    ink, focus, muted = "#102a43", "#0e6e67", "#486581"
    assert f"color: {ink}" in text and f"outline: 3px solid {focus}" in text and f"border-left: 3px solid {muted}" in text
    # the app's own tokens are the source of the values
    assert re.search(r"--text: " + ink + ";", CSS) and re.search(r"--focus: " + focus + ";", CSS)
    assert re.search(r"--text-muted: " + muted + ";", CSS)
    assert ratio(ink, "#ffffff") >= 4.5 and ratio(muted, "#ffffff") >= 4.5          # text on the white sheet
    assert ratio(focus, "#ffffff") >= 3.0 and ratio(focus, "#edf1f4") >= 3.0        # focus ring on the sheet and the screen backdrop
    assert ratio(muted, "#ffffff") >= 3.0                                           # the notice and footer rules
    first = text[text.index("{% if loop.first %}"):]
    head = first[:first.index("<h1>")]
    assert 'src="/static/brand/proofie-mark-mono-ink.svg" alt="" aria-hidden="true"' in head and ">Proofie</span>" in head
    assert "width: 10mm; height: 10mm" in text


def test_e7_package_insert_extends_base_so_it_prints_with_the_mark_and_without_chrome():
    text = _read("features/package_insert/print.html")
    assert text.startswith('{% extends "_base.html" %}') and 'class="btn btn-secondary"' in text    # the Print button is hidden by E5


# ---- E8 Open Graph ---------------------------------------------------------------------------------------------------

def test_e8_open_graph_on_a_base_page_and_not_on_the_buyer_status_page(hosted):
    page = hosted.get("/access").text
    assert '<meta property="og:type" content="website">' in page and '<meta property="og:title" content="Proofie">' in page
    assert ('<meta property="og:image" content="https://demo.example.test/static/brand/proofie-social-1280x640.png">' in page)
    assert '<meta property="og:image:width" content="1280">' in page and '<meta property="og:image:height" content="640">' in page
    assert '<meta name="twitter:card" content="summary_large_image">' in page
    text = ('Proofie helps a small seller answer a PayPal "item not received" dispute with sourced evidence they approve, '
            "then reads PayPal's record back. PayPal sandbox build, not affiliated with PayPal.")
    import html
    for prop in ('property="og:description"', 'name="twitter:description"'):
        assert html.unescape(re.search(r'<meta ' + prop + r' content="([^"]+)">', page).group(1)) == text
    assert '&quot;item not received&quot;' in page                                  # the quotes are escaped inside the attribute
    status = _read("features/status_link/public.html")
    assert "og:" not in status and "twitter:" not in status and "noindex" in status


# ---- E9 footer mark, E10 titles, E11 doc ------------------------------------------------------------------------------

def test_e9_status_page_footer_has_the_20px_mono_mark_before_the_existing_sentence():
    text = _read("features/status_link/public.html")
    footer = text[text.index('<footer class="site-footer">'):]
    assert footer.index("proofie-mark-mono-ink.svg") < footer.index("Independent project. Not affiliated with or endorsed by PayPal.")
    assert 'alt="" aria-hidden="true" width="20" height="20"' in footer


def test_e10_no_title_ends_with_the_middle_dot_form():
    offenders = [str(p.relative_to(TEMPLATES)) for p in TEMPLATES.rglob("*.html")
                 if re.search(r"\{% block title %\}.*· Proofie\{% endblock %\}", p.read_text(encoding="utf-8"))]
    assert offenders == []
    assert "Open the demo - Proofie" in _read("access.html")


def test_e11_design_doc_lists_the_brand_surfaces_and_the_paypal_rule():
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "## Product brand surfaces" in design
    section = design[design.index("## Product brand surfaces"):]
    for name in FILES:
        assert name in section
    assert "nothing sent to paypal carries the proofie brand" in section.lower()


def test_e14_case_pages_footer_has_the_same_content_edge():
    assert "body:has(.case-primary-nav) .site-footer { max-width: 1320px; }" in CSS


def test_e16_case_bar_wraps_instead_of_overlapping():
    # the measured proof is logs-RC44/D101/brand-surfaces/e16/overlap.py (Playwright, 1024/1280/1440/375); these are the rules it measured
    assert ".b-case-bar .case-line { flex-wrap: wrap; }" in CSS and ".b-case-bar > * { min-width: 0; }" in CSS
    assert re.search(r"@media \(min-width: 640px\) and \(max-width: 1439\.98px\) \{\s*\.b-case-bar \{ grid-template-columns: minmax\(0, 1fr\) minmax\(0, 1\.6fr\); \}", CSS)


def test_e17_header_tag_text_on_every_base_page(hosted):
    tag = '<span class="env-tag" data-safety="assistant-only">PayPal sandbox &middot; Proofie prepares - you decide</span>'
    assert tag in _read("_base.html")
    page = hosted.get("/access").text
    assert tag in page
    old_tag = "assistant only, you " + "review " + "everything"                    # spelled in pieces: the old text appears nowhere in the tree
    assert old_tag not in page
