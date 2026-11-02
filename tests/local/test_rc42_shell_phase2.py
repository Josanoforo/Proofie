"""Sticky header, stage strip CSS, amount alignment, the case strip at phone width, the stage_label switch, the single status
vocabulary. CSS is asserted as text (the layout itself is measured in a browser by the UX checks);
behaviour is asserted on the helpers and, in the feature tests, on rendered pages. Offline, no PayPal, no model."""
import re
from pathlib import Path

import pytest

from copilot.web import presentation, status

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
TEMPLATES = ROOT / "copilot" / "web" / "templates"


def media_blocks(css):
    """[(condition, body)] for every top-level @media block (brace matching)."""
    blocks = []
    for match in re.finditer(r"@media ([^{]+)\{", css):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        blocks.append((match.group(1).strip(), css[match.end():i - 1]))
    return blocks


# ---------------------------------------------------------------------------------------------- sticky header
def test_the_header_is_sticky_only_from_640_by_520_with_scroll_padding():
    # O4 pins the side rails of the case pages from 960 px (min-width only, beside the content, never over it);
    # the header rule below stays the single sticky header
    sticky = [(cond, body) for cond, body in media_blocks(CSS) if ".site-header" in body and "position: sticky" in body]
    assert len(sticky) == 1
    rails = [(cond, body) for cond, body in media_blocks(CSS) if "position: sticky" in body and ".site-header" not in body]
    assert all(cond == "(min-width: 960px)" for cond, _ in rails) and rails
    cond, body = sticky[0]
    assert "(min-width: 40em)" in cond and "(min-height: 32.5em)" in cond and "max-" not in cond
    assert re.search(r"\.site-header \{[^}]*position: sticky;[^}]*top: 0;[^}]*z-index: 50;", body)
    padding = re.search(r"scroll-padding-top: ([0-9.]+)rem", body)
    assert padding and float(padding.group(1)) >= 6.5
    assert CSS.count("position: sticky") == 1 + sum(body.count("position: sticky") for _, body in rails)      # nowhere outside the media blocks (nothing below 640 px)
    assert not re.search(r"\.site-header \{[^}]*position:", CSS.split("ux:shell start")[0])   # the base header stays in the flow


def test_the_skip_link_stays_above_the_sticky_header():
    skip = int(re.search(r"\.skip-link \{[^}]*z-index: (\d+)", CSS).group(1))
    header = int(re.search(r"\.site-header \{ position: sticky; top: 0; z-index: (\d+)", CSS).group(1))
    assert skip > header


# ---------------------------------------------------------------------------------------------- case strip
def test_the_case_strip_is_two_columns_at_phone_width_and_wider_gaps_from_640():
    base = re.search(r"\n\.facts \{([^}]*)\}", CSS).group(1)
    assert "repeat(2, minmax(0, 1fr))" in base                       # two columns without a media query
    wide = [body for cond, body in media_blocks(CSS) if cond == "(min-width: 640px)" and ".facts" in body]
    assert wide and "grid-template-columns: 1fr;" not in "".join(wide)


# ---------------------------------------------------------------------------------------------- stage strip CSS
def test_the_stage_strip_is_inline_on_desktop_and_stacks_on_phones():
    base = re.search(r"\n\.stage-strip \{([^}]*)\}", CSS).group(1)
    assert "flex-direction: column" in base and "list-style: none" in base
    rows = [body for cond, body in media_blocks(CSS) if cond == "(min-width: 640px)" and ".stage-strip" in body]
    assert rows and "flex-direction: row" in "".join(rows)
    compact = re.search(r"\.stage-strip \{ display: flex; flex-wrap: wrap; gap:[^}]+\}", CSS)
    assert compact and "flex-wrap: wrap" in compact.group(0)
    current = re.findall(r'\.stage-strip li\[aria-current="step"\] \{([^}]*)\}', CSS)[-1]
    assert "font-weight: 700" in current and "border: 3px" not in current  # current stage is still named and emphasized


# ---------------------------------------------------------------------------------------------- amounts
def test_amount_cells_use_tabular_figures_and_align_right_only_from_640():
    assert re.search(r"\.num, td\.amount, th\.amount \{ font-variant-numeric: tabular-nums; \}", CSS)
    assert not re.search(r"(^|\n)td\.amount[^{]*\{[^}]*text-align: right", CSS)             # not outside a media query
    wide = [body for cond, body in media_blocks(CSS) if cond == "(min-width: 640px)" and "td.amount" in body]
    assert wide and "text-align: right" in "".join(wide)


# ---------------------------------------------------------------------------------------------- stage_label switch
@pytest.mark.parametrize("value, words", [("INQUIRY", "Inquiry"), ("CHARGEBACK", "Chargeback"),
                                          ("PRE_ARBITRATION", "Pre-arbitration"), ("ARBITRATION", "Arbitration"),
                                          ("CLAIM", "Claim"), (None, "Not returned by PayPal"), ("", "Not returned by PayPal"),
                                          ("SOMETHING_NEW", "Stage not recognized")])
def test_stage_label_reads_the_stage_table(value, words):
    assert presentation.stage_label(value) == words


# ---------------------------------------------------------------------------------------------- single vocabulary
ATTEMPT_TO_RECORD = {"pending": "reserved", "sent_unconfirmed": "attempted", "confirmed": "confirmed", "failed": "refused",
                     "blocked": "blocked", "abandoned": "abandoned"}


@pytest.mark.parametrize("stored", ATTEMPT_TO_RECORD)
def test_an_attempt_reads_the_same_on_the_outcome_page_and_the_package_page(stored):
    view = status.evidence_status(status.SendRecord(state=ATTEMPT_TO_RECORD[stored]))
    assert presentation.state_label(stored) == view.label
    assert presentation.state_kind(stored) == view.kind          # words and class are shared; abandoned is red on both


@pytest.mark.parametrize("stored", ["registered", "registered_unconfirmed", "saved_without_registration"])
def test_a_tracking_state_reads_as_the_tracking_table_says(stored):
    view = status.tracking_status(stored)
    assert (presentation.state_label(stored), presentation.state_kind(stored)) == (view.label, view.kind)


def test_the_second_vocabulary_is_gone_from_the_states_table():
    for gone in ("pending", "sent_unconfirmed", "confirmed", "blocked", "failed", "abandoned", "reserved", "registered",
                 "registered_unconfirmed", "saved_without_registration"):
        assert gone not in presentation._STATES, gone
    for kept in ("verified", "conflict", "SHIPPED", "sent_confirmed", "refused_422"):
        assert kept in presentation._STATES, kept
    assert presentation.state_label("verified") == "Verified" and presentation.state_label("nope") == "Not available"


# ---------------------------------------------------------------------------------------------- captions (source level)
@pytest.mark.parametrize("name", ["features/outcome/outcome.html", "features/outcome/_sent.html",
                                  "features/account_cliff/account.html"])
def test_every_table_of_the_record_pages_has_a_caption(name):
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    tables = re.findall(r"<table[^>]*>\s*(<caption[^>]*>)?", source)
    assert tables and all(tables), name


# ---------------------------------------------------------------------------------------------- target sizes and spacing
def _rule(selector_regex):
    return " ".join(re.findall(selector_regex + r"\s*\{([^}]*)\}", CSS))      # every rule of that selector


def test_a_checkbox_cannot_shrink_in_its_label_and_is_at_least_24_px():
    rule = _rule(r"\.claim-confirm")
    assert "flex: none" in rule and "width: 44px" in rule and "height: 44px" in rule and "min-width: 44px" in rule
    generic = _rule(r'input\[type="checkbox"\], input\[type="radio"\]')
    assert "flex: none" in generic and "min-width: 24px" in generic and "min-height: 24px" in generic


def test_the_actions_row_leaves_at_least_12_px_between_a_primary_and_its_neighbours():
    assert "--space-3: 12px" in CSS
    assert "gap: var(--space-3) var(--space-4)" in _rule(r"\n\.actions")


def test_a_task_row_link_is_a_full_44_px_target():
    rule = _rule(r"\n\.task-link")
    assert "min-height: var(--target)" in rule and "--target: 44px" in CSS


def test_the_inward_focus_offset_is_only_for_plain_links_and_summaries_in_a_scrolling_table_region():
    rule = _rule(r"\.table-scroll a:focus-visible, \.table-scroll summary:focus-visible")
    assert "outline-offset: -3px" in rule
    assert ".table-scroll :focus-visible" not in CSS                   # filled buttons keep the default +2px offset
    assert "outline-offset: 2px" in _rule(r"\n:focus-visible")


def test_the_account_tables_wrap_long_words_and_the_short_constants_keep_whole_words():
    assert "overflow-wrap: anywhere" in _rule(r"\.account-cliff-summary th, \.account-cliff-summary td")
    assert "overflow-wrap: anywhere" in _rule(r'\.account-cliff-constants td\[data-label="Source"\]')
    assert ".account-cliff-constants td {" not in CSS                  # not on every cell of the table


def test_the_table_figure_rules_cover_cells_and_headers():
    assert re.search(r"\.num, td\.amount, th\.amount \{ font-variant-numeric: tabular-nums; \}", CSS)
    wide = " ".join(body for cond, body in media_blocks(CSS) if cond == "(min-width: 640px)" and "th.amount" in body)
    assert "td.amount, th.amount { text-align: right; }" in wide


# ---------------------------------------------------------------------------------------------- error glyphs
def test_a_field_error_is_drawn_with_a_hidden_glyph_then_the_words():
    from copilot.web import routes_disputes
    html = routes_disputes.templates.env.from_string(
        "{% from '_components.html' import field_error %}{{ field_error('error-x', 'Enter a <date>.') }}").render()
    assert html == '<p class="field-error" id="error-x"><span aria-hidden="true">✕</span> Enter a &lt;date&gt;.</p>'


def test_the_form_field_macros_draw_their_messages_through_field_error():
    from copilot.web import routes_disputes
    env = routes_disputes.templates.env
    html = env.from_string("{% from '_form_fields.html' import field, carrier_field, date_fields %}"
                           "{{ field('a', 'A', error='Bad A') }}{{ carrier_field(error='Bad carrier', other_error='Bad other') }}"
                           "{{ date_fields('d', 'D', errors={'d': 'Bad date'}) }}").render()
    messages = re.findall(r'<p class="field-error" id="(error-[a-z_]+)"><span aria-hidden="true">✕</span> ([^<]+)</p>', html)
    assert messages == [("error-a", "Bad A"), ("error-carrier", "Bad carrier"), ("error-carrier_other_name", "Bad other"),
                        ("error-d", "Bad date")]
    assert '<p class="field-error" id="error-a">Bad' not in html


@pytest.mark.parametrize("kind, glyph, words", [("error", "✕", "Error"), ("warn", "!", "Warning"), ("info", "i", "Note"),
                                                ("ok", "✓", "Done")])
def test_a_banner_without_a_title_still_carries_its_glyph_tag(kind, glyph, words):
    from copilot.web import routes_disputes
    html = routes_disputes.templates.env.from_string(
        "{% from '_components.html' import banner_mark %}{{ banner_mark(kind) }}").render(kind=kind)
    assert f'<span aria-hidden="true">{glyph}</span><span class="visually-hidden"> {words}</span>' in html
    assert 'class="tag tag--' in html


def _error_page(state, kind, title="Error: Something went wrong", message="Nothing was changed."):
    from copilot.web import routes_disputes
    return routes_disputes.templates.env.get_template("error.html").render(
        request=None, title=title, state=state, kind=kind, message=message, items=[], conflict_form=None, last_read=None,
        show_last_read=False, retry_url=None, back_url="/disputes", back_label="Go back", feature_enabled=lambda name: False)


@pytest.mark.parametrize("kind, tag_class", [("error", "tag--bad"), ("warn", "tag--warn"), ("info", "tag--info")])
def test_the_error_page_banner_carries_the_glyph_tag_when_its_title_repeats_the_heading(kind, tag_class):
    html = _error_page("refused", kind)                           # not the not-found state: the banner title is left out
    banner = html[html.index('class="banner banner-' + kind + '"'):]
    assert f'class="tag {tag_class}"' in banner.split("Nothing was changed.")[0]
    assert '<span class="visually-hidden">' in banner.split("Nothing was changed.")[0]


def test_the_not_found_error_page_keeps_its_own_titled_banner():
    html = _error_page("not-found", "error", title="Error: That dispute was not found")
    banner = html[html.index('class="banner banner-error"'):]
    assert "Check the dispute" in banner.split("Nothing was changed.")[0]


# ---------------------------------------------------------------------------------------------- one status vocabulary
def test_paypal_dispute_statuses_use_the_closed_status_table_everywhere():
    for value, code in status._PAYPAL_STATUS.items():
        kind, label = status.DISPUTE_TABLE[code]
        assert (presentation.state_kind(value), presentation.state_label(value)) == (kind, label), value
    assert presentation.state_kind("RESOLVED") == "na" == status.DISPUTE_TABLE["resolved"][0]


# ---------------------------------------------------------------------------------------------- dates inside sentences
@pytest.mark.parametrize("text, expected", [
    ("effective 2025-01-13: the checks", "effective 13 January 2025: the checks"),
    ("(2025-01-13)", "(13 January 2025)"),
    ("since 2025-06-01.", "since 1 June 2025."),
    ("(read 2026-10-05) and (read 2026-10-06)", "(read 5 October 2026) and (read 6 October 2026)"),
    ("not a date 2026-13-45 stays", "not a date 2026-13-45 stays"),
    ("tracking 2026-10-07-AB stays", "tracking 2026-10-07-AB stays"),
    ("AB-2026-10-07 stays", "AB-2026-10-07 stays"),
    ("2026-10-07T10:00:00Z stays", "2026-10-07T10:00:00Z stays"),
    ("path x/2026-10-07/y stays", "path x/2026-10-07/y stays"),
    ("file 2026-10-07.pdf stays", "file 2026-10-07.pdf stays"),
    ("12026-10-07 stays", "12026-10-07 stays"),
    ("no date here", "no date here"), ("", ""), (None, None), (7, 7),
])
def test_dates_in_words(text, expected):
    assert presentation.dates_in_words(text) == expected


def test_dates_in_words_is_a_filter_in_both_environments():
    from copilot.web import routes_disputes, routes_package
    for env in (routes_disputes.templates.env, routes_package.templates.env):
        assert env.from_string("{{ 'since 2025-06-01.'|dates_in_words }}").render() == "since 1 June 2025."


# every sentence of the hosted note and of its ephemeral variant is under 25 words
def _sentences(text):
    return re.split(r"(?<=[.!?]) +(?=[A-Z0-9])", text)


def test_every_sentence_of_the_hosted_note_and_its_ephemeral_variant_is_under_25_words():
    from copilot import config, hosted_ephemeral
    disk = "The demo keeps its database and a log of its PayPal calls on the host's disk; a host restart may erase them."
    assert disk in config.HOSTED_DEMO_SENTENCE                       # the sentence hosted_ephemeral swaps for its notice
    ephemeral = config.HOSTED_DEMO_SENTENCE.replace(disk, hosted_ephemeral.NOTICE, 1)
    assert ephemeral != config.HOSTED_DEMO_SENTENCE
    for text, count in ((config.HOSTED_DEMO_SENTENCE, 14), (ephemeral, 15)):
        sentences = _sentences(text)
        assert len(sentences) == count
        assert all(len(sentence.split()) < 25 for sentence in sentences), [s for s in sentences if len(s.split()) >= 25]
