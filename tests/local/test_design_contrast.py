"""The colour contrast promised in docs/DESIGN.md section 6 is recomputed from the CSS that actually
ships (copilot/web/static/app.css), so a later colour edit cannot silently break AA, and the document cannot
drift away from the stylesheet. WCAG 2.x relative luminance; 4.5:1 for text, 3:1 for control outlines, focus
rings and banner edges.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")

# (foreground token, background token, minimum ratio, what it is) - the pairs of DESIGN.md section 6.
PAIRS = [
    ("text", "bg", 4.5), ("text", "surface", 4.5),
    ("text-muted", "bg", 4.5), ("text-muted", "surface", 4.5), ("text-muted", "surface-2", 4.5),
    ("accent-text", "bg", 4.5), ("accent-text", "surface", 4.5),
    ("on-accent", "accent", 4.5),
    ("info-fg", "info-bg", 4.5), ("ok-fg", "ok-bg", 4.5), ("warn-fg", "warn-bg", 4.5), ("error-fg", "error-bg", 4.5),
    ("border", "bg", 3.0), ("border", "surface", 3.0),
    ("focus", "bg", 3.0), ("focus", "surface", 3.0),
    ("info-edge", "info-bg", 3.0), ("ok-edge", "ok-bg", 3.0), ("warn-edge", "warn-bg", 3.0),
    ("error-edge", "error-bg", 3.0),
    ("accent", "bg", 3.0),
    # An excluded document's row is drawn on --surface-2 (no opacity), so its text, links and button
    # outlines are measured on that fill too.
    ("text", "surface-2", 4.5), ("accent-text", "surface-2", 4.5), ("border", "surface-2", 3.0),
    ("focus", "surface-2", 3.0),
    # Palette C: the navy header, its text and focus ring, and the two orange accents (graphics, 3:1).
    ("header-fg", "header-bg", 4.5), ("header-muted", "header-bg", 4.5), ("header-focus", "header-bg", 3.0),
    ("brand-mark", "header-bg", 3.0), ("brand", "header-bg", 3.0), ("brand", "bg", 3.0),
]


def _block(selector_regex: str) -> dict:
    match = re.search(selector_regex + r"\s*\{(.*?)\}", CSS, re.S | re.M)
    assert match, f"CSS block not found: {selector_regex}"
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", match.group(1)))


LIGHT = _block(r"^:root")
DARK_AUTO = _block(r':root:not\(\[data-theme="light"\]\)')
DARK_FORCED = _block(r':root\[data-theme="dark"\]')


def _lin(c: int) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(hex_: str) -> float:
    h = hex_.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_contrast_function_matches_known_wcag_values():
    assert ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert ratio("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)     # the classic just-below-AA grey
    assert ratio("#ffffff", "#ffffff") == pytest.approx(1.0)


@pytest.mark.parametrize("theme_name,theme", [("light", LIGHT), ("dark", DARK_AUTO)])
def test_every_documented_pair_meets_aa_in_the_shipped_css(theme_name, theme):
    failures = [(fg, bg, round(ratio(theme[fg], theme[bg]), 2), need) for fg, bg, need in PAIRS
                if ratio(theme[fg], theme[bg]) < need]
    assert failures == [], f"{theme_name}: {failures}"


def test_the_forced_dark_theme_and_the_automatic_dark_theme_are_the_same_palette():
    assert DARK_AUTO == DARK_FORCED and len(DARK_AUTO) >= 20


def test_light_and_dark_define_the_same_tokens():
    assert set(LIGHT) >= set(DARK_AUTO)


def test_the_design_document_lists_the_exact_values_the_css_ships():
    for theme in (LIGHT, DARK_AUTO):
        for token in ("bg", "surface", "surface-2", "text", "text-muted", "border", "accent", "accent-text", "on-accent",
                      "focus", "header-bg", "header-fg", "header-muted", "header-focus", "brand", "brand-mark",
                      "info-bg", "info-fg", "info-edge", "warn-fg", "error-edge"):
            assert theme[token].lower() in DESIGN.lower(), f"--{token} {theme[token]} missing from DESIGN.md"


def test_the_lowest_ratios_quoted_in_the_design_document_are_the_real_ones():
    assert f"{ratio(LIGHT['warn-fg'], LIGHT['warn-bg']):.2f}" == "7.58"
    assert f"{ratio(LIGHT['border'], LIGHT['bg']):.2f}" == "3.58"
    assert f"{ratio(DARK_AUTO['border'], DARK_AUTO['surface']):.2f}" == "4.18"
    assert f"{ratio(LIGHT['warn-edge'], LIGHT['warn-bg']):.2f}" == "4.46"
    for number in ("7.58", "3.58", "4.18", "4.46"):
        assert number in DESIGN


def test_the_stylesheet_has_no_external_resources_and_no_removed_focus():
    assert "@import" not in CSS and "url(" not in CSS and "http" not in CSS      # local only, no CDN, no web fonts
    assert re.search(r":focus-visible\s*\{[^}]*outline:\s*3px solid var\(--focus\)", CSS)
    assert "outline: none" not in CSS and "outline:none" not in CSS
    assert "--target: 44px" in CSS


# These supersede the old banner-only semantics: verified is green; missing/conflict is red.
# Numeric WCAG palette assertions above remain unchanged.
# --- semantics of colour
CSS_CLEAN = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
RULES = [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS_CLEAN)]


def _uses(body: str, prefix: str) -> bool:
    return re.search(r"var\(--" + prefix, body) is not None


def test_state_colours_only_mark_verified_conflict_error_and_brand_jobs():
    for selector, body in RULES:
        parts = [p.strip() for p in selector.split(",")]
        if _uses(body, "ok-"):
            assert all(p.startswith((".banner-ok", ".tag--ok", 'tr[data-check-state="verified"]')) for p in parts), f"green outside verified/confirmed states: {selector}"
        if _uses(body, "error-"):
            # p[role="alert"] (app.css, 64d9956e, C17/F3): an error paragraph is red with a cross mark, an error job.
            assert all(p.startswith((".banner-error", ".tag--bad", ".risk", ".error-summary", ".field-error", ".approval-blocked", ".claim-conflict-note", 'p[role="alert"]', 'input[aria-invalid="true"]', 'select[aria-invalid="true"]', 'textarea[aria-invalid="true"]', '.claim-card[data-verdict="conflict"]', '.task-table tr[data-status="blocked"]', ".panel--blocked", ".task-blocked", 'tr[data-check-state="conflict"]', 'tr[data-check-state="missing"]')) for p in parts), f"red outside blocked/error/conflict states: {selector}"
        if _uses(body, "brand"):
            assert all(p.startswith((".site-header", ".brand-mark")) for p in parts), f"orange outside the header: {selector}"
    # and each token family is really used somewhere (the test is not vacuous)
    assert any(_uses(b, "ok-") for _s, b in RULES) and any(_uses(b, "error-") for _s, b in RULES)
    assert any(_uses(b, "brand") for _s, b in RULES)


def test_the_semantics_check_would_catch_a_misuse():
    bad = [(".btn", "border-left-color: var(--ok-edge);")]
    assert any(_uses(b, "ok-") and not all(p.strip().startswith(".banner-ok") for p in s.split(",")) for s, b in bad)


def test_check_rows_use_semantic_state_colours_but_never_brand_colours():
    for selector, body in RULES:
        if selector.startswith(("tr[", ".chip", "button", ".btn", "a.btn", "td")) or "data-check-state" in selector:
            assert not _uses(body, "brand"), selector
            if _uses(body, "ok-") or _uses(body, "error-"):
                parts = [part.strip() for part in selector.split(",")]
                assert all(part.startswith(('tr[data-check-state="verified"]', 'tr[data-check-state="missing"]', 'tr[data-check-state="conflict"]')) for part in parts), selector


def test_a_conflict_row_uses_the_red_edge_not_amber_in_both_layouts():
    conflict = [(sel, body) for sel, body in RULES if 'data-check-state="conflict"' in sel]
    assert len(conflict) == 2                                              # the table layout and the stacked-card layout
    for sel, body in conflict:
        assert "var(--error-edge)" in body and "warn" not in body, sel


# --- excluded rows without opacity
def test_an_excluded_row_is_marked_by_a_muted_background_never_by_opacity():
    excluded = [(sel, body) for sel, body in RULES if ".is-excluded" in sel]
    assert excluded
    for sel, body in excluded:
        assert "opacity" not in body, sel
        assert "var(--surface-2)" in body and "var(--text-muted)" in body
    assert "opacity: 0.75" not in CSS_CLEAN


# --- a secondary button is secondary everywhere
def _specificity(selector: str) -> tuple:
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+", selector))
    types = len(re.findall(r"(?:^|[\s>+~])[a-zA-Z][\w-]*", selector))
    return (ids, classes, types)


def test_a_secondary_button_inside_any_banner_outranks_every_banner_button_rule_and_comes_after_them():
    banner_button_rules = []
    secondary = None
    for index, (selector, body) in enumerate(RULES):
        for part in [p.strip() for p in selector.split(",")]:
            if ".banner" in part and (".btn" in part or "button" in part):
                if "btn-secondary" in part and ":hover" not in part:
                    secondary = (index, part, body) if secondary is None or _specificity(part) > _specificity(secondary[1]) else secondary
                elif "btn-secondary" not in part and ":hover" not in part and "background" in body:
                    banner_button_rules.append((index, part))
    assert secondary is not None and banner_button_rules
    index, part, body = secondary
    assert "background: transparent" in body and "color: inherit" in body and "currentColor" in body
    for other_index, other in banner_button_rules:
        assert _specificity(part) > _specificity(other), f"{part} does not outrank {other}"
        assert index > other_index, f"{part} comes before {other}"
    # the families the design names, one by one: none of them can win against the secondary rule
    for family in ("info", "ok", "warn", "error"):
        assert _specificity(part) > _specificity(f".banner-{family} .btn")


# ----------------------------------------------------------------------------------- shell
BANNER_FILLS = ("ok-bg", "warn-bg", "error-bg", "info-bg")


@pytest.mark.parametrize("theme_name,theme", [("light", LIGHT), ("dark", DARK_AUTO)])
def test_the_filled_button_text_and_edge_hold_on_the_accent_and_on_every_banner_fill(theme_name, theme):
    """The filled .btn-primary is the same --accent control inside every banner (the white repaint rule is gone), so the
    accent must stand out from each banner fill as a shape (3:1) and carry its label (--on-accent, 4.5:1)."""
    assert ratio(theme["on-accent"], theme["accent"]) >= 4.5, theme_name
    failures = [(bg, round(ratio(theme["accent"], theme[bg]), 2)) for bg in BANNER_FILLS if ratio(theme["accent"], theme[bg]) < 3.0]
    assert failures == [], f"{theme_name}: {failures}"


@pytest.mark.parametrize("name", ["DARK_AUTO", "DARK_FORCED"])
def test_the_dark_outline_clears_three_to_one_on_every_state_fill(name):
    theme = {"DARK_AUTO": DARK_AUTO, "DARK_FORCED": DARK_FORCED}[name]
    assert theme["border"].lower() == "#77818c"
    failures = [(bg, round(ratio(theme["border"], theme[bg]), 2)) for bg in BANNER_FILLS if ratio(theme["border"], theme[bg]) < 3.0]
    assert failures == [], f"{name}: {failures}"


def test_the_banner_white_repaint_and_the_global_aria_disabled_rule_are_gone():
    no_comments = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for family in ("banner-ok", "banner-warn", "banner-error"):
        assert not re.search(r"\." + family + r" \.btn\s*[,{]", no_comments), family     # the rule that set --surface on .btn
    assert not re.search(r"(^|\})\s*\[aria-disabled=\"true\"\]\s*\{", no_comments)
    assert 'form[data-submitting="1"] [aria-disabled="true"]' in CSS
