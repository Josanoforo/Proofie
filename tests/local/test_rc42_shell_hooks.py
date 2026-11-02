"""The shell hooks the package and dispute pages build on (copilot/web/links.py, field_words.py, the done_banner undo
argument, the stage table, the label filter). Offline, no PayPal, no model. Every hook is
backward compatible: no existing page changes its output (the byte-identical and unchanged-label tests below)."""
import re

import pytest
from _dom import parse

from copilot.core import checklist_input
from copilot.web import field_words, links, presentation, routes_disputes, routes_package

# PayPal's dispute_lifecycle_stage enum as pinned in the conformance schema; the parity with that file is asserted in
# tests/features/conformance/test_rc42_stage_table.py (the conformance feature is excluded from the flags-off export, so this
# file must not read its fixtures).
SCHEMA = ["INQUIRY", "CHARGEBACK", "PRE_ARBITRATION", "ARBITRATION"]
ENVS = [routes_disputes.templates.env, routes_package.templates.env]


def macro(env, names, call, **context):
    return env.from_string("{% from '_components.html' import " + names + " %}" + call).render(**context)


# ------------------------------------------------------------------------------------------------ links.py
def test_the_resolution_center_link_is_the_sandbox_host_with_noopener_and_a_new_tab_note():
    anchor = parse(str(links.resolution_center_link())).find("a")
    assert anchor.attrs["href"] == links.RESOLUTION_CENTER_URL
    assert anchor.attrs["rel"] == "noopener" and anchor.attrs["target"] == "_blank"
    assert anchor.text() == "Open PayPal's Resolution Center (sandbox) (opens in a new tab)"
    hidden = anchor.find("span", class_="visually-hidden")
    assert hidden is not None and hidden.text() == "(opens in a new tab)"


def test_the_only_link_target_is_the_sandbox_host_never_the_live_or_the_www_paypal_resolution_center():
    host = re.match(r"https://([^/]+)/", links.RESOLUTION_CENTER_URL).group(1)
    assert host == "www.sandbox.paypal.com"
    assert "api-m" not in links.RESOLUTION_CENTER_URL and "//www.paypal.com" not in links.RESOLUTION_CENTER_URL


def test_the_link_words_can_replace_a_phrase_and_are_escaped():
    anchor = parse(str(links.resolution_center_link(links.RESOLUTION_CENTER_NAME))).find("a")
    assert anchor.children[0] == "PayPal's Resolution Center"
    evil = str(links.resolution_center_link("<b>x</b>"))
    assert "<b>" not in evil and "&lt;b&gt;" in evil


@pytest.mark.parametrize("env", ENVS)
def test_both_template_environments_expose_the_link_as_a_global(env):
    html = env.from_string("{{ resolution_center_link() }}|{{ RESOLUTION_CENTER_URL }}").render()
    assert html.startswith('<a href="https://www.sandbox.paypal.com/') and html.endswith("|" + links.RESOLUTION_CENTER_URL)


def test_the_sign_in_sentence_is_the_one_in_the_item():
    assert links.SIGN_IN_SENTENCE == ("To see this dispute as PayPal shows it, open PayPal's Resolution Center (sandbox) "
                                      "and sign in with the seller account.")


# ------------------------------------------------------------------------------------------ field_words.py
def test_every_manual_field_has_the_words_the_shipment_form_already_uses():
    assert set(checklist_input.MANUAL_FIELD_NAMES) <= set(field_words.FIELD_WORDS)
    for name, label, _ in routes_package.SHIPMENT_FIELDS:
        assert field_words.field_word(name) == label, name


def test_delivery_facts_keys_render_the_words_the_page_shows_today():
    # features/delivery_facts/page.html field_labels: lower case in the sentence, capitalised in the <dt>.
    today = {"tracking": "tracking number", "carrier": "carrier", "status": "status", "shipment": "ship date",
             "line1": "address line 1", "line2": "address line 2", "city": "city", "state": "state",
             "postal_code": "postal code", "country_code": "country"}
    for key, old in today.items():
        assert field_words.field_word(key).lower() == old, key
        assert field_words.field_word(key) == old.capitalize(), key


@pytest.mark.parametrize("key", ["", "nope", "some_new_field", None, 7, ("a",)])
def test_an_unknown_key_never_prints_as_itself(key):
    assert field_words.field_word(key) == "A field Proofie does not name" == field_words.UNKNOWN_FIELD_WORD
    assert "_" not in field_words.field_word(key)


@pytest.mark.parametrize("env", ENVS)
def test_the_label_is_a_filter_and_a_global_in_both_environments(env):
    html = env.from_string("{{ 'capture_date'|field_word }}|{{ field_word('postal_code') }}|{{ 'x_y'|field_word }}").render()
    assert html == "Date the proof was captured|Postal code|A field Proofie does not name"


def test_no_visible_word_in_the_table_is_a_raw_key():
    for key, words in field_words.FIELD_WORDS.items():
        title_cased = words[0].isupper() if not words.startswith("$") else key in {"25_to_100", "100_to_500"}
        assert title_cased and "_" not in words, key


def test_delivery_and_topic_tracking_words_are_context_specific():
    assert field_words.field_word("tracking") == "Tracking number"
    assert field_words.field_word("topic_tracking") == "Tracking"
    assert [field_words.field_word("topic_" + key) for key in
            ("delivery", "tracking", "packaging", "item_detail", "other")] == [
                "Delivery", "Tracking", "Packaging", "Item detail", "Other"]


# ------------------------------------------------------------------------------------------- stage table
def test_the_stage_table_is_exactly_the_four_pinned_schema_values_in_paypals_order():
    assert [value for value, _ in presentation.STAGE_TABLE] == SCHEMA == ["INQUIRY", "CHARGEBACK", "PRE_ARBITRATION", "ARBITRATION"]
    assert [name for _, name in presentation.STAGE_TABLE] == ["Inquiry", "Chargeback", "Pre-arbitration", "Arbitration"]
    assert "CLAIM" not in dict(presentation.STAGE_TABLE)


@pytest.mark.parametrize("value, name", [("INQUIRY", "Inquiry"), ("CHARGEBACK", "Chargeback"),
                                         ("PRE_ARBITRATION", "Pre-arbitration"), ("ARBITRATION", "Arbitration"),
                                         ("CLAIM", "Claim")])
def test_stage_name_uses_the_table_and_keeps_claim_only_as_a_legacy_alias(value, name):
    assert presentation.stage_name(value) == name


@pytest.mark.parametrize("value", [None, "", "  "])
def test_a_stage_paypal_did_not_return_reads_not_returned_by_paypal(value):
    assert presentation.stage_name(value) == presentation.STAGE_NOT_RETURNED == "Not returned by PayPal"


@pytest.mark.parametrize("value", ["SOMETHING_NEW", "inquiry", 3, ("INQUIRY",)])
def test_a_present_stage_that_paypal_does_not_define_reads_stage_not_recognized_never_the_enum(value):
    assert presentation.stage_name(value) == presentation.STAGE_UNKNOWN == "Stage not recognized"
    assert "SOMETHING" not in presentation.stage_name(value)


def test_the_shared_stage_macro_of_the_dispute_pages_keeps_its_words():
    """The macro reads stage_name only for the four stages and the legacy CLAIM (it prints its own sentence for anything else), so
    changing what stage_name returns for other values cannot change its output."""
    env = routes_disputes.templates.env
    macro = ("{% macro stage_words(value) -%}{%- if stage_strip(value) or value == 'CLAIM' %}{{ stage_name(value) }}"
             "{% else %}Stage not recognized{% endif -%}{%- endmacro %}")
    for value, words in (("INQUIRY", "Inquiry"), ("PRE_ARBITRATION", "Pre-arbitration"), ("CLAIM", "Claim"),
                         ("SOMETHING_NEW", "Stage not recognized"), (None, "Stage not recognized")):
        assert env.from_string(macro + "{{ stage_words(v) }}").render(v=value) == words


@pytest.mark.parametrize("current", SCHEMA)
def test_the_strip_lists_the_four_stages_with_exactly_one_current(current):
    strip = presentation.stage_strip(current)
    assert [item["value"] for item in strip] == SCHEMA
    assert [item["value"] for item in strip if item["current"]] == [current]
    assert all(set(item) == {"value", "name", "current"} for item in strip)       # no done/to-come state: nothing predicted


@pytest.mark.parametrize("value", [None, "", "CLAIM", "SOMETHING_NEW"])
def test_there_is_no_strip_for_a_missing_unknown_or_legacy_stage(value):
    assert presentation.stage_strip(value) is None


def test_the_strip_words_are_the_items_strings():
    assert presentation.STAGE_STRIP_LABEL == "Where this dispute is in PayPal's process"
    assert presentation.STAGE_CURRENT_WORDS == "current stage"


@pytest.mark.parametrize("env", ENVS)
def test_the_stage_helpers_are_globals_in_both_environments(env):
    html = env.from_string("{{ stage_name('ARBITRATION') }}|{{ stage_strip('INQUIRY')|length }}|{{ STAGE_CURRENT_WORDS }}").render()
    assert html == "Arbitration|4|current stage"


# ----------------------------------------------------------------------------------------- done_banner undo
PLAIN = ('<div class="banner banner-ok" data-state="done" data-done="detached" role="status"><p class="banner-title">'
         '<strong>✓ a.pdf was removed from the response.</strong></p></div>')


@pytest.mark.parametrize("env", ENVS)
def test_without_undo_the_banner_is_byte_identical_to_the_old_one(env):
    assert macro(env, "done_banner", "{{ done_banner('detached', 'a.pdf was removed from the response.') }}") == PLAIN
    for call in ("{{ done_banner(key, text, None) }}", "{{ done_banner(key, text, undo=None, csrf_token='t') }}",
                 "{{ done_banner(key, text, csrf_token='t') }}"):
        assert macro(env, "done_banner", call, key="detached", text="a.pdf was removed from the response.") == PLAIN


def test_done_undo_exists_only_for_the_two_undoable_keys():
    undo = presentation.done_undo("detached", "/disputes/D/packages/5/attach/9", "a.pdf")
    assert undo == {"post": "/disputes/D/packages/5/attach/9", "hidden": "removing a.pdf from the response", "fields": ()}
    undo = presentation.done_undo("document_excluded", "/disputes/D/documents/9/include", "a.pdf",
                                  fields=[("return_to", "package:5")])
    assert undo == {"post": "/disputes/D/documents/9/include", "hidden": "leaving out a.pdf",
                    "fields": (("return_to", "package:5"),)}
    for key in ("attached", "document_included", "statement_removed", "approved", "document_added", "unknown", None):
        assert presentation.done_undo(key, "/x", "a.pdf") is None, key
    assert presentation.done_undo("detached", "", "a.pdf") is None
    assert presentation.done_undo("detached", "/x", None) is None and presentation.done_undo("detached", "/x", " ") is None


@pytest.mark.parametrize("env", ENVS)
def test_with_undo_the_banner_holds_one_guarded_post_form_with_the_accessible_name(env):
    undo = presentation.done_undo("document_excluded", "/disputes/D/documents/9/include", "my <scan>.pdf",
                                  fields=(("return_to", "package:5"),))
    html = macro(env, "done_banner", "{{ done_banner('document_excluded', 'text', undo=undo, csrf_token='tok') }}", undo=undo)
    banner = parse(html).find("div", data_state="done")
    forms = banner.find_all("form")
    assert len(forms) == 1 and forms[0].attrs["method"] == "post"
    assert forms[0].attrs["action"] == "/disputes/D/documents/9/include"
    fields = {n.attrs["name"]: n.attrs["value"] for n in forms[0].find_all("input")}
    assert fields == {"csrf_token": "tok", "return_to": "package:5"}
    button = forms[0].find("button")
    assert button.attrs["class"] == "btn-link"
    assert button.text() == "Undo leaving out my <scan>.pdf"
    assert button.children[0] == "Undo" and button.find("span", class_="visually-hidden").text() == "leaving out my <scan>.pdf"
    assert "<scan>" not in html                                                      # escaped, not injected as markup
    assert banner.find("p", class_="banner-title").text() == "✓ text"           # the sentence is unchanged


def test_undo_without_a_csrf_token_draws_no_control_rather_than_a_dead_one():
    env = routes_disputes.templates.env
    undo = presentation.done_undo("detached", "/x", "a.pdf")
    assert "<form" not in macro(env, "done_banner", "{{ done_banner('detached', 't', undo=undo) }}", undo=undo)


def test_a_page_call_that_reads_done_undo_renders_the_old_banner_while_nothing_passes_it():
    env = routes_disputes.templates.env
    done = {"key": "detached", "text": "a.pdf was removed from the response."}
    html = macro(env, "done_banner", "{{ done_banner(done.key, done.text, undo=done.undo, csrf_token=csrf_token) }}",
                 done=done, csrf_token="tok")
    assert html == PLAIN              # done.undo is undefined: P and D may switch the call before they pass an undo


# -------------------------------------------------------------------------------------------- idempotence
def test_install_stays_idempotent_with_the_new_globals():
    env = routes_package.templates.env
    before, filters = dict(env.globals), dict(env.filters)
    presentation.install(env)
    assert set(env.globals) == set(before) and set(env.filters) == set(filters)
