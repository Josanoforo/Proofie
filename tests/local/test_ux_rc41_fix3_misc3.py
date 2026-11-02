"""CSS overflow of long file names, exact-content third wording, history sentence.

Offline only: fake PayPal clients, a temp database per test (conftest.py). The CSS facts are also measured in a real
browser; the tests here pin the rules that produce those numbers.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")


def _all_rules():
    """(selector list, declarations) of every rule, media wrappers ignored."""
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}@/][^{}]*)\{([^{}]*)\}", CSS)]


def _declares(selector_fragment, declaration):
    return [sel for sel, body in _all_rules() if selector_fragment in sel and declaration in body]


# ---- the package file name cell wraps a long name ----------
def test_mc2_1_the_package_file_name_cell_wraps_a_long_name():
    # package_review.html prints the file name in td[data-label="Document"]; a name with no break points must wrap there
    assert _declares('td[data-label="Document"]', "overflow-wrap: anywhere")
    for sel, body in _all_rules():                                              # th/td themselves still never break anywhere
        parts = [p.strip() for p in sel.split(",")]
        assert not ({"th", "td", "th, td"} & set(parts) and "overflow-wrap: anywhere" in body), sel


def test_mc2_1_the_large_button_may_shrink_in_a_cell_up_to_1024px():
    # the Attach button (min-width 14rem) pushed the table past the screen between 640 and 1024 px
    shrink = re.search(r"@media \(max-width: 1024px\) \{ td \.btn-large[^{}]*\{ min-width: 0; \} \}", CSS)
    assert shrink, "inside a cell the large button may shrink up to 1024px"
    assert "min-width: 14rem" in "".join(b for s, b in _all_rules() if s.startswith(".btn-large"))   # outside a cell it keeps its size


# ---- list items definitions and paragraphs break a long file name but ---
def test_mc2_2_3_list_items_definitions_and_paragraphs_break_a_long_file_name_but_not_a_word():
    # break-word wraps a token that does not fit and does not lower min-content, so labels ("Required") stay whole
    hits = _declares("main li", "overflow-wrap: break-word")
    assert hits and all("main dd" in h and "main p" in h for h in hits), hits
    for sel, body in _all_rules():
        if "overflow-wrap: anywhere" in body:
            assert not re.search(r"(^|,)\s*main (li|dd|small|p)\b", sel), sel      # anywhere would break short words
    assert _declares(".field-mark", "white-space: nowrap")


# ---- the exact content note has a third and fourth wording for attempts ---
def test_mc2_4_the_exact_content_note_has_a_third_and_fourth_wording_for_attempts():
    from test_ux_rc41_shell import macro
    from _dom import parse
    notes = {}
    for key, expr in {"will": "{% call exact_content() %}{{ text }}{% endcall %}",
                      "was": "{% call exact_content(sent=true) %}{{ text }}{% endcall %}",
                      "attempted": "{% call exact_content(state='attempted') %}{{ text }}{% endcall %}",
                      "refused": "{% call exact_content(state='refused') %}{{ text }}{% endcall %}"}.items():
        html = macro("exact_content", expr, text="Line one")
        notes[key] = parse(html).find("p", class_="exact-note").text()
        el = parse(html).find(None, data_exact_content="")
        assert el is not None and el.attrs.get("translate") == "no"
    tail = " Your browser will not translate this part."
    assert notes == {"will": "Shown exactly as PayPal will receive it." + tail, "was": "Shown exactly as PayPal received it." + tail,
                     "attempted": "Shown exactly as Proofie sent it." + tail, "refused": "Shown exactly as Proofie tried to send it." + tail}
