"""The draft page, contract checks, notices page, error and access pages.

Every assertion works on the rendered HTML (the DOM the browser receives) or on the request actually made; fake clients only,
no PayPal, no Ollama, no network. Behaviour items were written first."""
import json
import re
import sys
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "local"))

from _dom import parse                                   # noqa: E402
from _hosted import authenticated_client, set_test_csrf  # noqa: E402
from _ux_lint import back_links, breadcrumb_items, main_nav_links, primaries, required_sentence_count, template_lint  # noqa: E402

from copilot import config                                # noqa: E402
from copilot.app import create_app                        # noqa: E402
from copilot.features.draft import check, model, service, store  # noqa: E402
from copilot.web.routes_disputes import get_client        # noqa: E402
from draft_fakes import (DISPUTE_ID, EN_NOEST, ES_NOEST, FakePayPal, model_call, paypal_error, sheet,  # noqa: E402
                         TXN, TRACK, NORM)
from draft_fakes import _no_httpx_calls  # noqa: E402,F401  (autouse: no httpx call in these tests)
from test_draft_routes import GOOD, MODEL_POST, PAGE, TOKEN, _reset, _web  # noqa: E402

pytestmark = [pytest.mark.usefixtures("web_mode")]

PRIMARY_LABEL = "Review this text on the resolution page (nothing is sent yet)"
MODEL_LABEL = "Ask the local assistant for a draft (English and Spanish)"
HOSTED = config.hosted_demo


def _order(dom):
    """Document-order index of every node (for 'comes after' assertions)."""
    return {id(node): i for i, node in enumerate(dom.walk())}


def _box(page_text):
    """The handoff or copy box text, unescaped."""
    dom = parse(page_text)
    area = dom.find("textarea", id="draft-message") or dom.find("textarea", data_state="copy-en")
    return unescape(area.text()) if area is not None else None


def _box_raw(page_text):
    area = re.search(r'<textarea name="text"[^>]*>(.*?)</textarea>', page_text, re.S)
    return unescape(area.group(1)).strip("\r\n") if area else None


def _with_draft(monkeypatch, answer=None, raw=None, fake=None):
    web, fake, call = _web(monkeypatch, fake=fake, call=model_call(answer if answer is not None else GOOD, raw=raw))
    web.post(MODEL_POST, data={"csrf_token": TOKEN})
    _reset(web)
    return web, fake, call


# --- the validator refuses delivery and signature claims (tests first) ------
def _ok(en=EN_NOEST, es=ES_NOEST, s=None):
    return check.validate_draft(s or sheet(), en, es)


def test_p0_31_a_signature_claim_is_always_refused():
    v = _ok(en=EN_NOEST + " It was signed by the buyer.")
    assert not v.ok and any(p.startswith("English:") and "delivered or signed for" in p for p in v.problems)
    v = _ok(es=ES_NOEST + " Fue firmado por usted.")
    assert not v.ok and any(p.startswith("Spanish:") and "delivered or signed for" in p for p in v.problems)
    delivered = sheet(track={**TRACK, "tracking_status": "DELIVERED"})
    assert not _ok(en=EN_NOEST + " It was signed for.", s=delivered).ok          # signature: refused even when delivered
    assert not _ok(en=EN_NOEST + " Could you confirm it is signed?", s=sheet()).ok


@pytest.mark.parametrize("en,es", [
    (EN_NOEST + " The order was delivered.", ES_NOEST),
    (EN_NOEST + " The order has been delivered.", ES_NOEST),
    (EN_NOEST, ES_NOEST + " El pedido fue entregado."),
    (EN_NOEST, ES_NOEST + " El pedido ha sido entregado."),
    (EN_NOEST, ES_NOEST + " La entrega fue entregada."),
])
def test_p0_31_an_assertive_delivery_phrase_is_refused_unless_the_tracking_shows_delivered(en, es):
    v = _ok(en=en, es=es)
    assert not v.ok and any("delivered or signed for" in p for p in v.problems)
    delivered = sheet(track={**TRACK, "tracking_status": "DELIVERED"})
    assert _ok(en=en, es=es, s=delivered).ok


@pytest.mark.parametrize("en,es", [
    (EN_NOEST + " It is in transit.", ES_NOEST + " Si no ha recibido su pedido, responda aquí."),
    (EN_NOEST + " Was the order delivered?", ES_NOEST + " ¿Ha recibido su pedido?"),
    (EN_NOEST + " If it was delivered, tell us.", ES_NOEST + " Si fue entregado, avíseme."),
    (EN_NOEST + " It was shipped by FEDEX.", ES_NOEST),
])
def test_p0_31_questions_conditionals_shipped_and_in_transit_pass(en, es):
    v = _ok(en=en, es=es)
    assert v.ok, v.problems


def test_p0_31_existing_problem_strings_are_unchanged():
    v = _ok(en=EN_NOEST + " Your reference is 98765.")
    assert "English: the number 98765 is not in the facts" in v.problems
    v = _ok(es=ES_NOEST + " Se lo garantizo.")
    assert "Spanish: contains the promise word 'garantiz'" in v.problems
    assert _ok().ok and _ok().problems == ()


@pytest.mark.local_spec
def test_p0_31_the_rejected_fixture_is_refused_for_the_signature_claim_on_the_page(monkeypatch):
    web, _fake, _call = _with_draft(monkeypatch, {"en": EN_NOEST + " It was delivered and signed by the buyer.", "es": ES_NOEST})
    page = web.get(PAGE).text
    assert 'data-state="draft" data-valid="no"' in page
    assert "It says delivered or signed for. PayPal's tracking does not show a delivery, so Proofie did not use it." in unescape(page)


# --- structured issues, recomputed at render ------
def test_p1_70_issues_are_structured_and_the_string_tuple_is_unchanged():
    en = EN_NOEST + " Ref 98765. Also DEMO-FDX-000002 and 2026-10-02."
    v = _ok(en=en)
    found = check.issues(sheet(), en, ES_NOEST)
    kinds = {i.kind for i in found}
    assert {"number", "date", "tracking_code"} <= kinds
    values = {(i.kind, i.value) for i in found}
    assert ("tracking_code", "DEMO-FDX-000002") in values and ("date", "2026-10-02") in values
    assert not any(i.kind == "number" and i.value == "02" for i in found)     # inside the reported date
    assert all(isinstance(p, str) for p in v.problems)


@pytest.mark.local_spec
def test_p1_70_the_rejected_state_has_one_sentence_per_kind_and_no_raw_validator_strings(monkeypatch):
    en = EN_NOEST + " Ref 98765. Also DEMO-FDX-000002 and 2026-10-02."
    es = ES_NOEST + " Ref 98765."
    web, _fake, _call = _with_draft(monkeypatch, {"en": en, "es": es})
    page = unescape(web.get(PAGE).text)
    dom = parse(page)
    draft = dom.find(data_state="draft")
    assert draft.attrs["data-valid"] == "no"
    assert "The AI text was not used" in draft.text() and "Your message still holds the fixed template." in draft.text()
    problems = dom.find("ul", data_state="problems")
    items = [li.text() for li in problems.find_all("li")]
    assert "It has numbers that are not in the facts: 98765. (English and Spanish text)" in items
    assert any(i.startswith("It has a date that is not in the facts: 2026-10-02") for i in items)
    assert any(i.startswith("It has a tracking-like code that is not in the facts: DEMO-FDX-000002") for i in items)
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert "is not in the facts" in outside and "English: the number" not in outside and "DEMOFDX000002" not in outside
    details = dom.find("details", data_state="rejected-text")
    assert "Show the rejected text (not usable)" in details.find("summary").text()
    assert "Ref 98765" in details.text()


def test_p1_70_fails_closed_when_the_recomputed_verdict_differs(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    # a refused row whose texts actually validate against its own sheet: the page must not invent per-kind sentences
    store.save_draft(DISPUTE_ID, None, sheet(), EN_NOEST, ES_NOEST, "m", 7, None, False, ["English: the number 7 is not in the facts"])
    page = unescape(web.get(PAGE).text)
    dom = parse(page)
    draft = dom.find(data_state="draft")
    assert draft is not None and "The AI text was not used" in draft.text()
    assert "the number 7 is not in the facts" not in re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert dom.find("ul", data_state="problems") is None


# --- on the draft page ------
def test_a2_7_the_draft_page_has_no_required_sentence(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    assert required_sentence_count(web.get(PAGE).text) == 0


def test_p0_28_one_filled_primary_under_the_textarea_and_the_model_button_is_secondary(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    page = web.get(PAGE).text
    dom = parse(page)
    assert [n.text() for n in primaries(page)] == [PRIMARY_LABEL]
    found = dom.find_all(class_="btn-primary")
    order = _order(dom)
    area = dom.find("textarea", id="draft-message")
    assert area is not None and "maxlength" not in area.attrs and area.attrs["name"] == "text"
    assert order[id(found[0])] > order[id(area)]
    form = dom.find("form", data_state="handoff-form")
    assert form.attrs["action"] == f"/disputes/{DISPUTE_ID}/resolution/preview#preview" and "novalidate" in form.attrs
    assert found[0].inside("form") and "Nothing is sent yet. You check the exact text on the next page." in form.text()
    assert dom.find("input", name="action").attrs["value"] == "message" and dom.find("input", name="text") is None
    if HOSTED():
        assert dom.find("form", data_state="model-form") is None and MODEL_LABEL not in page
    else:
        model_button = [b for b in dom.find_all("button") if b.text() == MODEL_LABEL]
        assert len(model_button) == 1 and "btn-secondary" in model_button[0].attrs["class"]
        assert "btn-primary" not in model_button[0].attrs["class"]


def test_p0_29_a_failed_paypal_read_says_so_and_invents_no_paypal_state(monkeypatch):
    fake = FakePayPal(get_raises=paypal_error(500))
    web, fake, call = _web(monkeypatch, fake=fake)
    r = web.get(PAGE)
    page = unescape(r.text)
    dom = parse(page)
    assert "PayPal offers no send-message option" not in page and "/draft/model" not in page
    assert "Proofie could not read this dispute from PayPal" in dom.text()
    banner = dom.find(data_state="read-failed")
    assert banner is not None and "banner-warn" in banner.attrs["class"]
    assert "PayPal could not return the dispute right now. Try again in a few minutes." in banner.text()
    assert "Proofie cannot show the facts or say whether PayPal accepts a message here. Nothing was sent." in banner.text()
    reload_link = banner.find("a")
    assert reload_link.text() == "Reload page" and reload_link.attrs["href"] == PAGE
    assert dom.find("textarea") is None and dom.find("table", data_state="facts") is None and dom.find("form") is None
    assert dom.find("pre", data_state="template") is None and "copy this text" not in page
    assert call.calls == [] and all(m == "GET" for m, _p, _o in fake.calls)
    # polish: Reload page is the one forward action of the page, so it is the primary
    assert [p.text() for p in primaries(page)] == ["Reload page"] and reload_link.attrs["class"].split()[:2] == ["btn", "btn-primary"]


@pytest.mark.local_spec
def test_p0_29_the_model_is_never_called_on_an_empty_fact_sheet(monkeypatch):
    fake = FakePayPal(get_raises=paypal_error(500))
    web, fake, call = _web(monkeypatch, fake=fake)
    r = web.post(MODEL_POST, data={"csrf_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == PAGE
    assert call.calls == [] and store.latest_draft(DISPUTE_ID) is None


def test_p2_27_title_h1_and_dispute_line(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    page = web.get(PAGE).text
    dom = parse(page)
    assert f"<title>Draft a message to the buyer: {DISPUTE_ID} - Proofie</title>" in page
    h1 = dom.find("h1")
    assert h1.text().startswith("Draft a message to the buyer")
    assert h1.find("span", class_="h1-id").text() == f"Dispute {DISPUTE_ID}"
    assert breadcrumb_items(page) == [("All disputes", "/disputes"), (f"Dispute {DISPUTE_ID}", f"/disputes/{DISPUTE_ID}"),
                                      ("Draft a message to the buyer", None)] or \
        breadcrumb_items(page)[-1] == ("Draft a message to the buyer", None)
    assert back_links(page) == []


@pytest.mark.local_spec
def test_p1_71_the_box_starts_as_the_template_and_ai_text_only_with_start_ai(monkeypatch):
    web, _fake, _call = _with_draft(monkeypatch)
    template = service.draft_view(FakePayPal(), config.db_path(), DISPUTE_ID).template_text
    assert _box(web.get(PAGE).text) == template
    started = web.get(PAGE + "?start=ai")
    box = _box(started.text)
    assert box == GOOD["en"] and box != template
    assert "Read each sentence and compare it with the facts on this page." in started.text
    assert "Read each sentence" not in web.get(PAGE).text                           # The warning only when the box holds AI text
    page = parse(web.get(PAGE).text)
    use = [a for a in page.find_all("a") if a.text() == "Use this AI text as my starting point"]
    assert len(use) == 1 and use[0].attrs["href"].startswith(PAGE + "?start=ai")
    keep = [a for a in parse(started.text).find_all("a") if a.text() == "Keep the fixed template"]
    assert len(keep) == 1 and "start=" not in keep[0].attrs["href"]


@pytest.mark.local_spec
def test_p1_71_start_ai_is_refused_for_a_rejected_or_missing_draft(monkeypatch):
    web, _fake, _call = _with_draft(monkeypatch, {"en": EN_NOEST + " Ref 98765.", "es": ES_NOEST})
    template = service.draft_view(FakePayPal(), config.db_path(), DISPUTE_ID).template_text
    r = web.get(PAGE + "?start=ai")
    assert _box(r.text) == template and "Ref 98765" not in _box(r.text)
    assert 'data-state="start-ai-refused"' in r.text


@pytest.mark.local_spec
def test_p1_71_labels_hints_and_the_label_of_the_card(monkeypatch):
    web, _fake, _call = _with_draft(monkeypatch)
    page = unescape(web.get(PAGE).text)
    assert "Starts as the fixed template, written by code from PayPal's data." in page
    assert "Written by AI (local model)" in page and "For reference only. Not part of your message until you choose it." in page
    assert "Suggested by the local model" not in page


@pytest.mark.local_spec
def test_p1_72_spanish_blocks_are_marked_and_labelled(monkeypatch):
    web, _fake, _call = _with_draft(monkeypatch)
    dom = parse(web.get(PAGE).text)
    es = dom.find_all(data_lang="es")
    assert es and all(node.attrs.get("lang") == "es" for node in es)
    page = unescape(dom.text())
    assert "Escrito por IA (versión en español; revísala antes de copiar)" in page
    assert "Proofie does not send this Spanish text. Code checked its numbers and dates, not that it says the same as the English." in page
    assert dom.find(data_lang="en") is not None
    tracking = [n for n in dom.walk() if n.attrs.get("translate") == "no" and "DEMO-FDX-000001" in n.text()]
    assert tracking


@pytest.mark.local_spec
def test_p1_73_a_model_failure_redirects_303_to_a_fixed_sentence_and_a_refresh_does_not_rerun_it(monkeypatch):
    web, _fake, call = _web(monkeypatch, call=model_call(raises=RuntimeError("GPU busy")))
    r = web.post(MODEL_POST, data={"csrf_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == PAGE + "?model=unavailable#ai"
    shown = web.get(PAGE + "?model=unavailable")
    assert "The AI model did not answer. Your message still holds the fixed template. You can ask again in a minute." in shown.text
    banner = parse(shown.text).find(data_state="model-error")
    assert banner is not None and "banner-warn" in banner.attrs["class"]
    for _ in range(2):
        web.get(PAGE + "?model=unavailable")
    assert len(call.calls) == 1 and store.latest_draft(DISPUTE_ID) is None
    assert "model-error" not in web.get(PAGE + "?model=whatever").text
    assert "did not answer" not in web.get(PAGE + "?model=%3Cb%3E").text


@pytest.mark.local_spec
def test_p1_73_an_unreadable_answer_redirects_with_its_own_value(monkeypatch):
    web, _fake, _call = _web(monkeypatch, call=model_call(raw="not json"))
    r = web.post(MODEL_POST, data={"csrf_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == PAGE + "?model=unreadable#ai"
    assert "The AI model&#39;s answer could not be read. Your message still holds the fixed template." in web.get(PAGE + "?model=unreadable").text \
        or "The AI model's answer could not be read. Your message still holds the fixed template." in web.get(PAGE + "?model=unreadable").text


def test_p1_73_the_model_form_is_guarded_and_says_what_it_does(monkeypatch):
    if HOSTED():
        pytest.skip("hosted demo: no model form")
    web, _fake, _call = _web(monkeypatch)
    dom = parse(web.get(PAGE).text)
    form = dom.find("form", data_state="model-form")
    assert form is not None and form.attrs.get("data-guard-submit") != "off" and "data-sends" not in form.attrs
    button = form.find("button")
    assert button.attrs["data-sending-text"] == "Writing the draft..."
    text = unescape(dom.text())
    assert "The AI model runs on this computer. It can take a few minutes the first time. Nothing is sent to PayPal or the buyer." in text


@pytest.mark.local_spec
def test_p1_74_a_stale_draft_names_what_changed_and_withholds_use_this_text(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    old = sheet(track={**TRACK, "tracking_number": "OLD-TRACK-000009"})
    en = EN_NOEST.replace("DEMO-FDX-000001", "OLD-TRACK-000009")
    es = ES_NOEST.replace("DEMO-FDX-000001", "OLD-TRACK-000009")
    assert check.validate_draft(old, en, es).ok
    store.save_draft(DISPUTE_ID, None, old, en, es, "m", 7, None, True, [])
    r = web.get(PAGE)
    text = unescape(parse(r.text).text())
    assert re.search(r"The facts changed after this AI text was written \(tracking number[,)].*Ask the AI model again\.", text)
    assert "start=ai" not in r.text and "Use this AI text as my starting point" not in r.text
    assert "OLD-TRACK-000009" not in _box(r.text)
    assert _box(web.get(PAGE + "?start=ai").text) == service.draft_view(FakePayPal(), config.db_path(), DISPUTE_ID).template_text


@pytest.mark.local_spec
def test_p1_74_the_written_time_is_shown_with_the_shared_filter(monkeypatch):
    web, _fake, _call = _with_draft(monkeypatch)
    dom = parse(web.get(PAGE).text)
    label = dom.find(data_state="model-label")
    assert label is not None and "Written by AI (local model) at " in label.text() and label.find("time") is not None


@pytest.mark.parametrize("rels,has_sentence", [(("send_message", "make_offer"), True), (("send_message", "provide_evidence"), False)])
def test_p1_75_the_offer_sentence_only_when_an_offer_is_still_possible(monkeypatch, rels, has_sentence):
    web, _fake, _call = _web(monkeypatch, fake=FakePayPal(rels=rels))
    page = unescape(web.get(PAGE).text)
    dom = parse(page)
    sentence = "After a message, PayPal no longer shows the option to make an offer."
    assert ("PayPal accepts a message on this dispute. Nothing is sent from this page." in page)
    if has_sentence:
        assert sentence in page
        link = [a for a in dom.find_all("a") if a.text() == "Make an offer first"]
        assert len(link) == 1 and link[0].attrs["href"] == f"/disputes/{DISPUTE_ID}/resolution"
    else:
        assert sentence not in page and "make an offer" not in page.lower()


def test_p2_23_the_copy_only_box_is_editable_and_labelled(monkeypatch):
    web, _fake, _call = _web(monkeypatch, fake=FakePayPal(rels=("provide_evidence",)))
    page = web.get(PAGE).text
    dom = parse(page)
    area = dom.find("textarea", data_state="copy-en")
    assert area is not None and "readonly" not in area.attrs and area.attrs.get("id")
    label = dom.find("label", **{"for": area.attrs["id"]})
    assert label is not None and label.text().startswith("Message to copy (English)")
    text = unescape(dom.text())
    assert "Edit it if you want, then select it and copy it. PayPal offers no message option for this dispute, so you send it to the buyer yourself. Proofie does not save or send this text, and PayPal's record will not show it." in text
    assert len(primaries(page)) == 0 and dom.find("form", data_state="handoff-form") is None


def test_p2_25_the_template_text_appears_once(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    page = web.get(PAGE).text
    assert page.count("Hello, this is about your order of 28 September 2026 for 40.00 USD.") == 1   # template date in words (resolution)
    dom = parse(page)
    assert dom.find("pre", data_state="template") is None


def test_p2_26_the_facts_table_has_a_caption_and_display_labels(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    page = web.get(PAGE).text
    dom = parse(page)
    facts = dom.find("table", data_state="facts")
    assert facts.find("caption").text() == "Facts the message may use (from PayPal)"
    shown = {row.find("th").text(): row.find("td").text() for row in facts.find_all("tr") if row.find("th")}
    assert list(shown) == ["Order date", "Order amount", "Shipping address", "Tracking number", "Carrier", "Last tracking status",
                           "Delivery estimate" if "Delivery estimate" in shown else "Dispute stage", "Dispute stage"][:len(shown)] or \
        {"Order date", "Order amount", "Shipping address", "Tracking number", "Carrier", "Last tracking status", "Dispute stage"} <= set(shown)
    assert "Currency" not in shown and "Dispute id" not in shown
    details = dom.find("details", data_state="facts-details")
    if HOSTED():
        assert details is None
    else:
        assert "Facts as data (technical)" in details.find("summary").text()


def test_p2_28_the_no_draft_line_and_the_hosted_model_off_line(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    page = unescape(web.get(PAGE).text)
    if HOSTED():
        assert "No draft yet." not in page and 'data-state="no-draft"' not in page
        assert "The AI model is off in the hosted demo. Your message starts as the fixed template." in page
        assert 'data-state="model-off"' in page
    else:
        assert "No AI text yet. The AI model writes only when you ask." in page and 'data-state="no-draft"' in page
        assert "No draft yet." not in page


def test_p2_22_the_disclaimer_line_is_short_and_always_there(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    dom = parse(unescape(web.get(PAGE).text))
    line = dom.find(data_state="disclaimer")
    assert line.text() == "The AI model is an assistant only and can be wrong. You review every word."


def test_draft_template_lint_and_no_hosted_regressions():
    base = ROOT / "copilot" / "web" / "templates"
    paths = [base / "features" / "draft" / "draft.html", base / "features" / "conformance" / "checks.html",
             base / "features" / "webhooks" / "events.html", base / "error.html", base / "access.html"]
    assert template_lint(paths) == []


# --- conformance ------
def _conformance(monkeypatch, tmp_path, evidence=None, specs=None):
    monkeypatch.setenv("COPILOT_FEATURE_CONFORMANCE", "true")
    for key, value in (("PROOFIE_EVIDENCE_DIR", evidence), ("PROOFIE_SPECS_DIR", specs)):
        monkeypatch.delenv(key, raising=False) if value is None else monkeypatch.setenv(key, str(value))
    app = create_app()
    return authenticated_client(app)


def test_p1_76_synthetic_run_names_the_sample_responses_and_hides_variable_names(monkeypatch, tmp_path):
    web = _conformance(monkeypatch, tmp_path)
    page = unescape(web.get("/conformance").text)
    dom = parse(page)
    assert "Schema used: the short extracts of PayPal's published documents built into Proofie." in page
    assert "Responses checked: Proofie's own sample responses, not responses from PayPal." in page
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert "PROOFIE_" not in outside and "the rest differ" not in outside
    details = dom.find("details")
    assert "Technical details" in details.find("summary").text() and "PROOFIE_SPECS_DIR" in details.text()
    assert "pinned 2026-10-06" in details.text()
    assert breadcrumb_items(page)[-1] == ("Contract checks", None) and back_links(page) == []
    assert dom.find("h1").text() == "Contract checks"


def test_p1_76_zero_recorded_responses_never_says_the_rest_differ(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "empty.jsonl").write_text("", encoding="utf-8")
    web = _conformance(monkeypatch, tmp_path, evidence=evidence)
    page = unescape(web.get("/conformance").text)
    assert "Responses checked: none. No recorded PayPal responses were found on this computer." in page
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert "the rest differ" not in outside and "PROOFIE_" not in outside and "differ" not in outside.split("Responses checked")[1][:300]


def test_p1_76_the_differ_clause_appears_only_when_something_differs(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    good = {"method": "GET", "path": "/v1/customer/disputes/PP-R-X", "status": 200, "body": {"dispute_id": "PP-R-X"}}
    (evidence / "one.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")
    web = _conformance(monkeypatch, tmp_path, evidence=evidence)
    page = unescape(web.get("/conformance").text)
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    line = [p.text() for p in parse(outside).find_all("p") if p.text().startswith("Responses checked:")][0]
    assert line.startswith("Responses checked: 1 recorded PayPal response")
    from copilot.features.conformance.suite import configured_run
    result, _skipped = configured_run()
    differing = result["response_counts"]["different"]
    assert ("differ" in line) == (differing > 0)


# --- webhooks page ------
def _webhooks(monkeypatch, webhook_id="WH-REGISTERED-1"):
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS", "true")
    monkeypatch.delenv("COPILOT_WEBHOOK_ID", raising=False) if webhook_id is None else monkeypatch.setenv("COPILOT_WEBHOOK_ID", webhook_id)
    return authenticated_client(create_app())


def test_p2_29_the_notices_table_has_a_caption_labels_and_plain_columns(monkeypatch):
    from copilot.features.webhooks import store as wstore
    web = _webhooks(monkeypatch)
    wstore.save_event("WH-DEMO-1", "CUSTOMER.DISPUTE.UPDATED", "dispute", DISPUTE_ID, None, "a" * 64, "verified", "UNDER_REVIEW")
    page = web.get("/webhooks/events").text
    dom = parse(page)
    table = dom.find("table", data_state="events")
    assert table.find("caption").text() == "Notices PayPal sent to Proofie, newest first"
    headers = [th.text() for th in table.find("thead").find_all("th")]
    assert headers == ["Received (Mexico City time)", "What happened", "Dispute", "Signature check",
                       "Dispute status (Proofie asked PayPal)", "Details"]
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        assert len(cells) == 6 and [c.attrs.get("data-label") for c in cells] == headers
    assert dom.find("h1").text() == "PayPal notices"
    assert breadcrumb_items(page)[-1] == ("PayPal notices", None) and back_links(page) == []
    assert main_nav_links(page)
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert "COPILOT_WEBHOOK_ID" not in outside and "sha256" not in outside


def test_p2_29_not_configured_and_verification_off_sentences(monkeypatch):
    web = _webhooks(monkeypatch, webhook_id=None)
    page = unescape(web.get("/webhooks/events").text)
    dom = parse(page)
    assert "Proofie is not set up to receive PayPal notices." in dom.find(data_state="not-configured").text()
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert "COPILOT_WEBHOOK_ID" not in outside and "COPILOT_WEBHOOK_ID" in page
    assert "Signature checks are off, so these notices are not verified and Proofie did not ask PayPal about them." in page


# --- error and refusal pages ------
def _main_links(dom, href):
    main = dom.find("main")
    return [a for a in main.find_all("a") if a.attrs.get("href") == href]


def test_p1_77_session_expired_on_a_dispute_post_links_back_to_the_dispute(monkeypatch):
    web, _fake, call = _web(monkeypatch)
    r = web.post(MODEL_POST, data={"csrf_token": "forged"})
    assert r.status_code == 403 and call.calls == []
    page = unescape(r.text)
    dom = parse(page)
    assert re.search(r"<title>Error: Your session expired", page)
    assert dom.find("h1").text() == "Your session expired"
    found = primaries(page)
    assert len(found) == 1 and found[0].text() == "Go back to the dispute" and found[0].attrs["href"] == f"/disputes/{DISPUTE_ID}"
    assert "Your session expired before the form was sent. Nothing was sent. If it keeps happening, check that your browser allows cookies for this site." in page
    assert _main_links(dom, "/disputes") == [] and back_links(page) == []
    assert breadcrumb_items(page) == [("All disputes", "/disputes"), (f"Dispute {DISPUTE_ID}", None)] or \
        breadcrumb_items(page)[0] == ("All disputes", "/disputes")


def test_p1_77_session_expired_outside_a_dispute_has_no_back_link(monkeypatch):
    from copilot.web.routes_disputes import csrf_expired_page
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/fulfilment/anything", "headers": [], "query_string": b"", "app": create_app()}
    response = csrf_expired_page(Request(scope))
    assert response.status_code == 403
    assert primaries(response.body.decode("utf-8")) == []


def test_p1_77_a_get_error_keeps_one_try_again_and_no_duplicate_all_disputes_link(monkeypatch):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakePayPal(get_raises=paypal_error(503))
    web = authenticated_client(app)
    r = web.get(f"/disputes/{DISPUTE_ID}")
    assert r.status_code == 502
    page = unescape(r.text)
    dom = parse(page)
    assert dom.find("h1").text() == "PayPal could not be reached right now"
    banner = dom.find(data_state="paypal-unreachable")
    assert banner is not None and "PayPal could not be reached right now" not in banner.find("p", class_="banner-title").text() \
        if banner.find("p", class_="banner-title") else True
    found = primaries(page)
    assert len(found) == 1 and found[0].text() == "Try again" and found[0].attrs["href"] == f"/disputes/{DISPUTE_ID}"
    assert _main_links(dom, "/disputes") == []
    # The error title is the current (last) crumb, so the dispute stays a link
    assert breadcrumb_items(page) == [("All disputes", "/disputes"), (f"Dispute {DISPUTE_ID}", f"/disputes/{DISPUTE_ID}"),
                                      ("PayPal could not be reached right now", None)]
    assert back_links(page) == []


def test_p1_77_a_post_error_offers_no_retry_and_one_get_link_to_the_dispute(monkeypatch):
    from copilot.core.disputes import PayPalError
    app = create_app()

    @app.post(f"/disputes/{DISPUTE_ID}/boom")
    def boom():
        raise PayPalError(503, "X", "dbg", {})
    web = authenticated_client(app)
    r = web.post(f"/disputes/{DISPUTE_ID}/boom", data={})
    assert r.status_code == 502
    page = unescape(r.text)
    assert "Proofie could not confirm" in page or "could not read PayPal" in page
    assert not [a for a in parse(page).find_all("a") if a.text() == "Try again"]
    found = primaries(page)
    assert len(found) == 1 and found[0].attrs["href"] == f"/disputes/{DISPUTE_ID}"


def test_p1_77_security_invalid_id_page_has_no_all_disputes_button(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    r = web.get("/disputes/not-an-id/draft")
    assert r.status_code == 400
    assert _main_links(parse(r.text), "/disputes") == [] and len(primaries(r.text)) == 0


def _render_conflict():
    from copilot.web.routes_disputes import templates
    return templates.env.get_template("error.html").render(
        title="Error: This package cannot be approved yet", message="A document does not agree with your package.", kind="error",
        state="refused", items=["Tracking number differs."], show_last_read=False, retry_url=None, back_url=None,
        back_label="Back", last_lead=None, last_read=None,
        conflict_form={"dispute_id": DISPUTE_ID, "package_id": 7, "csrf_token": "t0k",
                       "documents": [{"id": 3, "filename": "receipt.pdf"}]})


def test_p1_78_the_conflict_page_offers_three_clear_ways_and_no_required_sentence():
    html = unescape(_render_conflict())
    dom = parse(html)
    assert required_sentence_count(html) == 0
    assert "A document does not agree with your package. Choose one way to fix it." in dom.text()
    hrefs = {a.text(): a.attrs["href"] for a in dom.find_all("a")}
    assert hrefs["Edit statements"] == f"/disputes/{DISPUTE_ID}/packages/7#statements"
    assert hrefs["Edit shipment details"] == f"/disputes/{DISPUTE_ID}/packages/7#shipment-details"
    area = dom.find("textarea", name="conflict_note") or dom.find("input", name="conflict_note")
    assert area is not None
    label = dom.find("label", **{"for": area.attrs["id"]})
    assert label.text().startswith("Note for PayPal about the difference")
    assert "This note is added to your statements and sent to PayPal with your evidence. Approving does not send anything." in dom.text()
    exclude = dom.find("form", action=f"/disputes/{DISPUTE_ID}/documents/3/exclude")
    assert exclude is not None and "btn-link" in exclude.find("button").attrs["class"]
    assert "A file you leave out of the checks is also taken out of your response: it is not sent to PayPal." in dom.text()
    assert 'Remove "receipt.pdf" from consideration' not in html and "Leave receipt.pdf out of the checks" in dom.text()
    approve = dom.find("form", action=f"/disputes/{DISPUTE_ID}/packages/7/approve")
    assert approve.find("button").text() == "Approve with this note" and "novalidate" in approve.attrs
    assert primaries(html) == []
    assert template_lint([ROOT / "copilot/web/templates/error.html"]) == []


# --- access page (hosted only) ------
@pytest.mark.hosted_spec
def test_p1_79_the_access_page_is_a_clean_single_field_form(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "test-hosted-access-code")
    web = TestClient(create_app(), base_url="https://localhost", client=("127.0.0.1", 12345))
    r = web.get("/access")
    assert r.status_code == 200
    page = unescape(r.text)
    dom = parse(page)
    assert required_sentence_count(page) == 0
    form = dom.find("form")
    assert "novalidate" in form.attrs
    field = dom.find("input", name="code")
    assert field.attrs["autocomplete"] == "current-password" and field.attrs.get("aria-required") == "true"
    assert "The code you were given to open this demo." in dom.text()
    found = primaries(page)
    assert [n.text() for n in found] == ["Open the demo"] and dom.find("h1").text() == "Open the demo"
    assert main_nav_links(page) == [] and dom.find("nav", aria_label="Main") is None
    bad = web.post("/access", data={"code": "wrong code!", "csrf_token": web.cookies.get("csrf_token"), "next": "/disputes"})
    assert bad.status_code == 401
    text = unescape(bad.text)
    assert "The access code is not correct. Check it and enter it again." in text
    summary = parse(text).find("section", class_="error-summary")
    assert summary is not None and summary.find("a").attrs["href"] == "#field-code"
    assert parse(text).find("input", name="code").attrs.get("aria-invalid") == "true"
