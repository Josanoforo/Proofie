"""Copy and presentation fixes found by the accessibility reading pass: language of parts, plain failures, counters.

Offline only: fake PayPal client, a temp database per test (conftest.py), DOM checks through _dom."""
import secrets

import pytest
from _dom import parse
from _hosted import authenticated_client, set_test_csrf
from _lang import spanish_outside_lang
from test_a11y_pins import DID, FakeClient

from copilot import config
from copilot.app import create_app
from copilot.core import checklist
from copilot.db import repo
from copilot.web.routes_disputes import get_client

pytestmark = [pytest.mark.usefixtures("web_mode")]


def _web(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_TRIAGE", "true")
    monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF", "false")       # the fake client answers no account search (C-D turns it on)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, secrets.token_urlsafe(32))
    return web


def _package_page(web):
    package = repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    response = web.get(f"/disputes/{DID}/packages/{package}")
    assert response.status_code == 200
    return response.text


# ---------------------------------------------------------------------------------------------------- language of parts
def test_the_spanish_rule_title_on_the_package_page_sits_inside_an_element_marked_es(web_mode, monkeypatch):
    html = _package_page(_web(monkeypatch))
    title = checklist.load_rules(country="MX").source_title
    assert "Protección al Vendedor" in title
    dom = parse(html)
    link = next(n for n in dom.find_all("a") if "Protección al Vendedor" in n.text())      # the rule-source link
    # the element that carries the words, or the one inside it, says Spanish
    marked = [n for n in [link, *link.walk()] if (n.attrs.get("lang") or "").startswith("es") and title.split(" - ")[0] in n.text()]
    assert marked, "the rule title is not inside lang=es"
    assert spanish_outside_lang(html) == []


def test_no_spanish_rule_text_sits_outside_lang_es_on_the_triage_page(web_mode, monkeypatch):
    response = _web(monkeypatch).get(f"/disputes/{DID}/triage")
    assert response.status_code == 200
    assert "Comprobante de entrega" in response.text                      # the page really prints the Spanish citation
    assert spanish_outside_lang(response.text) == []


def test_every_spanish_phrase_of_every_shipped_rule_set_is_marked_by_the_filter(web_mode):
    """A new Spanish rule title, section or citation word that the filter does not know fails here, not on a screen."""
    from copilot.web.presentation import mark_spanish
    for country in checklist.supported_countries():
        rules = checklist.load_rules(country=country)
        texts = [rules.source_title] + [rule["citation"] for rule in rules.checks.values()]
        for row in ((rules.raw or {}).get("eligibility_notes") or {}).get("rows") or []:
            citation = row["citation"]
            texts.append(f"{citation['page_title']}, last updated {citation['page_date']}, section “{citation['section']}”")
        for text in texts:
            assert spanish_outside_lang(str(mark_spanish(text))) == [], (country, text)


# ---------------------------------------------------------------------------------------------- a refused send
def _stored_result(web, technical, state="rejected"):
    from copilot.web import send_results
    package = repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    rid = send_results.save(config.db_path(), DID, package, {
        "http_status": 400, "title": "Not sent", "message": "PayPal rejected this evidence. Review the technical details below, "
        "fix the content if needed, and try again.", "technical": technical, "kind": "error", "state": state})
    response = web.get(f"/disputes/{DID}/packages/{package}/result/{rid}")
    assert response.status_code == 400                       # the page answers with the stored HTTP status
    return response.text


def _outside_details(html):
    import re
    return re.sub(r"<details.*?</details>", "", html, flags=re.S)


def test_a_refused_send_gives_paypals_reason_in_words_outside_technical_details(web_mode, monkeypatch):
    html = _stored_result(_web(monkeypatch), "status=400 issue=INVALID_STRING_LENGTH debug_id=dbg-400")
    visible = _outside_details(html)
    banner = parse(html).find("div", data_state="rejected").text()
    assert "PayPal says a text in this request is too short or too long." in banner
    assert "INVALID_STRING_LENGTH" not in visible and "dbg-400" not in visible
    assert "status=400 issue=INVALID_STRING_LENGTH" in html                                  # still there, under Technical details


def test_a_refused_send_with_an_unknown_issue_says_paypal_did_not_say_why(web_mode, monkeypatch):
    html = _stored_result(_web(monkeypatch), "status=400 issue=SOMETHING_NEW debug_id=d")
    banner = parse(html).find("div", data_state="rejected").text()
    assert "PayPal did not say why; the technical details below show its answer." in banner
    assert "SOMETHING_NEW" not in _outside_details(html)


# ---------------------------------------------------------------------------------------------- Spanish hint words inline
def test_the_spanish_words_of_the_delivery_hint_flow_inline_with_normal_commas():
    from pathlib import Path
    from copilot.web.presentation import DELIVERY_WORDS_HINT_HTML
    css = (Path(__file__).resolve().parents[2] / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    # only the Spanish line of a form hint is its own block; the three words of the delivery hint are not
    assert '.field-hint span[lang="es"] {' not in css and ".field-hint .hint-es { display: block; }" in css
    dom = parse(f"<p class='field-hint'>{DELIVERY_WORDS_HINT_HTML}</p>")
    spans = dom.find_all("span")
    assert [s.text() for s in spans] == ["entregado", "firmado", "recibido"]
    assert all("hint-es" not in s.attrs.get("class", "") and s.attrs["lang"] == "es" for s in spans)
    assert "entregado</span>, <span" in str(DELIVERY_WORDS_HINT_HTML)


def test_the_spanish_line_of_a_form_hint_is_still_its_own_block(web_mode):
    from pathlib import Path
    template = (Path(__file__).resolve().parents[2] / "copilot" / "web" / "templates" / "_form_fields.html").read_text(encoding="utf-8")
    assert template.count('<span lang="es" class="hint-es">') == 2


# ---------------------------------------------------------------------------------------------- minors
def _dispute_page(web):
    response = web.get(f"/disputes/{DID}/workspace/prepare")
    assert response.status_code == 200
    return response.text


def test_the_upload_button_names_its_object_and_the_dispute_page_says_disputes_may_have_changed(web_mode, monkeypatch):
    html = _dispute_page(_web(monkeypatch))
    dom = parse(html)
    assert [b.text() for b in dom.find_all("button") if "Upload" in b.text()] == ["Upload the document"]
    assert "Cases may have changed since." not in html


def test_a_settling_result_offers_check_again_and_names_the_state_instead_of_warning(web_mode, monkeypatch):
    web = _web(monkeypatch)
    html = _stored_result(web, "", state="settling")
    dom = parse(html)
    assert any(b.text() == "Check again" for b in dom.find_all("button")), "no Check again control"
    banner = dom.find("div", data_state="settling")
    assert "Warning" not in banner.text() and "Warning" not in html.split("<main")[1]


def test_the_disclosure_marker_is_kept_out_of_the_accessible_name():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    block = css[css.index("summary::before"):].split("}")[0]
    assert 'content: "▸" / ""' in block                    # the marker has empty alternative text


def test_plain_words_replace_the_internal_ones_on_the_ledger_and_account_pages():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "copilot" / "features"
    text = " ".join((root / part).read_text(encoding="utf-8") for part in ("ledger/service.py", "account_cliff/rules.py"))
    for word in ("payloads", "digests", "attributed", "is implemented here"):
        assert word not in text, word


def test_the_field_mark_legend_names_the_rules_not_the_loaded_policy(web_mode, monkeypatch):
    html = _package_page(_web(monkeypatch))
    from html import unescape
    text = unescape(html)
    assert "Required by PayPal's Seller Protection rules" in text and "loaded policy" not in text


def test_one_spelling_the_us_one_in_the_visible_words():
    """PayPal's US-facing words: favor, recognized, program, fulfillment, canceled. Identifiers and PayPal's own codes
    (RESOLVED_BUYER_FAVOUR) are not words a seller reads and are not touched."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "copilot"
    for relative in ("core/checklist.py", "web/inbox_view.py", "features/outcome/read.py", "features/ledger/service.py",
                     "features/account_cliff/network_context.py", "web/presentation.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        words = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                         and not n.value.isupper() and n.value.upper() != n.value.replace(" ", "_").upper())
        for word in ("recognised", "favour", "programme", "Fulfilment", "Cancelled"):
            assert word not in words.replace("RESOLVED_BUYER_FAVOUR", "").replace("RESOLVED_SELLER_FAVOUR", ""), (relative, word)
