"""The published claim-level exclusions as cited rows of the rule files (`eligibility_notes`), matched by code
on the dispute's own fields, and the informational "Eligibility notes" block of the review page. Offline: synthetic
sandbox-shaped dispute bodies (pilot/fixtures/synthetic), a fake client, no network, nothing from .env."""
from _hosted import authenticated_client, reset_csrf
import copy
import dataclasses
import html
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from copilot import config
from copilot.core import checklist_input
from copilot.core.checklist import evaluate, load_rules
from copilot.core.eligibility import EFFECT_SENTENCE, TRIGGER_FIELDS, notes_for, rows
from copilot.db import repo
from conftest import in_git_checkout, is_private_checkout
from test_uic1_copy import DID, WAITING, Fake, _web

import importlib.util

_spec = importlib.util.spec_from_file_location("m1_loader", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)

US = load_rules(country="US")
MX = load_rules(country="MX")

# Excerpt of the pinned PayPal Customer Disputes API v1 document (schema.json, info.version 1.12, fetched 2026-10-06,
# sha256 fee5e04ab606f8961b95744e65c4189fc7c4613e79ba2f944d2deab79e834330): the `dispute` properties a trigger may
# name and their enums, copied from components.schemas.dispute_reason and components.schemas.dispute_channel. The
# The acceptance script re-reads the pinned file and checks this excerpt against it.
DISPUTE_PROPERTY_ENUMS = {
    "reason": ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "UNAUTHORISED",
               "CREDIT_NOT_PROCESSED", "DUPLICATE_TRANSACTION", "INCORRECT_AMOUNT", "PAYMENT_BY_OTHER_MEANS",
               "CANCELED_RECURRING_BILLING", "PROBLEM_WITH_REMITTANCE", "OTHER"),
    "dispute_channel": ("INTERNAL", "EXTERNAL", "ALERT"),
}
INR, SNAD, UNAUTH = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "UNAUTHORISED"
US_QUOTES = {
    "inr_card_issuer_chargeback": "Item Not Received claims that result from buyers filing chargeback claims with their "
                                  "issuers for card-funded transactions are not eligible for PayPal’s Seller "
                                  "Protection.",
    "snad_not_eligible": "Items where the buyer claims (either with us or their card issuer) that the item you sent "
                         "isn’t what was ordered (referred to as a Significantly Not as Described claim).",
    "unauthorized_shipment_within_two_days": "In the case of Unauthorized Transaction claim, you must provide valid proof "
                                             "of shipment or proof of delivery that demonstrates that the item was "
                                             "shipped or provided to the buyer no later than two days after PayPal "
                                             "notified you of the dispute or reversal.",
}


def dispute(reason, channel="INTERNAL"):
    """A document-shaped dispute object: the synthetic sandbox body with its own reason and channel set."""
    b = copy.deepcopy(WAITING)
    b["reason"] = reason
    b["dispute_channel"] = channel
    for t in b.get("disputed_transactions", []):
        t["reason"] = reason
    return b


# ---- rule-file schema -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rules", [US, MX], ids=["US", "MX"])
def test_every_row_has_the_fields_and_its_trigger_names_document_fields_and_values(rules):
    section = rules.raw["eligibility_notes"]
    assert "fee5e04ab606f8961b95744e65c4189fc7c4613e79ba2f944d2deab79e834330" in section["trigger_source"]
    ids = [r["id"] for r in rows(rules)]
    assert len(ids) == len(set(ids)) and ids
    for r in rows(rules):
        assert set(r) <= {"id", "trigger", "quote", "quote_lang", "lead_in", "citation", "basis", "effect"}, r["id"]
        assert r["basis"] == "published_policy" and r["effect"] == "informational", r["id"]
        assert isinstance(r["quote"], str) and r["quote"].strip() == r["quote"] and len(r["quote"]) > 20, r["id"]
        assert r["quote_lang"] in ("en", "es")
        assert r["trigger"], r["id"]
        for field, values in r["trigger"].items():
            assert field in TRIGGER_FIELDS and field in DISPUTE_PROPERTY_ENUMS, (r["id"], field)
            assert values and set(values) <= set(DISPUTE_PROPERTY_ENUMS[field]), (r["id"], field, values)
        c = r["citation"]
        assert set(c) == {"page_title", "page_date", "section", "url", "read_date"}, r["id"]
        assert c["page_date"] == rules.effective_date and c["read_date"] == "2026-10-06"
        assert c["url"] == f"https://www.paypal.com/{rules.country.lower()}/legalhub/paypal/seller-protection"
        assert c["page_title"] and c["section"]


def test_the_us_rows_are_the_three_page_sentences_verbatim():
    assert {r["id"]: r["quote"] for r in rows(US)} == US_QUOTES
    by_id = {r["id"]: r for r in rows(US)}
    assert by_id["inr_card_issuer_chargeback"]["citation"]["section"] == "What’s Eligible"
    assert by_id["snad_not_eligible"]["citation"]["section"] == "Ineligible Items and Transactions"
    assert by_id["snad_not_eligible"]["lead_in"] == ("The following items or transactions are not eligible for "
                                                     "PayPal’s Seller Protection program:")
    assert by_id["unauthorized_shipment_within_two_days"]["citation"]["section"] == "Basic Requirements"
    assert all(r["citation"]["page_title"] == "PayPal's Seller Protection Program" for r in rows(US))


def test_the_mx_rows_quote_the_spanish_page_for_the_same_three_situations():
    by_id = {r["id"]: r for r in rows(MX)}
    assert set(by_id) == set(US_QUOTES)
    assert all(r["quote_lang"] == "es" for r in by_id.values())
    assert "contracargos" in by_id["inr_card_issuer_chargeback"]["quote"]
    assert "distinto al descrito" in by_id["snad_not_eligible"]["quote"]
    assert "dos días" in by_id["unauthorized_shipment_within_two_days"]["quote"]


@pytest.mark.parametrize("rules", [US, MX], ids=["US", "MX"])
def test_verification_note_keeps_its_history_and_gains_the_dated_line(rules):
    note = rules.raw["verification_note"]
    assert "2026-10-04" in note                                   # the earlier reading stays
    assert [line for line in note.splitlines() if line.startswith("2026-10-06 (RULES-US1): eligibility_notes added")]
    last = {"US": "2026-10-19 (RC43-PK W1-3): a `clause` object added to the six published_policy checks",
            "MX": "2026-10-20 (RC43-PK W1-3 MX): a `clause` object added to the six published_policy checks"}
    before = {"US": "2026-10-07 (FIX-RONDA4): whitespace only, no quote edited.",
              "MX": "2026-10-06 (FIX-RONDA3): whitespace normalised in the quoted strings, nothing else."}
    assert note.splitlines()[-1].startswith(last[rules.country])
    assert note.splitlines()[-2].startswith(before[rules.country])  # the line before it stays the whitespace line


def test_the_mx_note_states_which_whitespace_was_normalised():
    """The page has U+00A0 in the snad row; the file keeps ordinary spaces
    and says so, naming the three places and the comparison rule."""
    line = MX.raw["verification_note"].splitlines()[-2]          # [-1] is the RC43-PK MX clause line
    for place in ("'transacciones' + U+00A0 + 'no'", "'requisitos' + U+00A0 + 'para'", "'por' + U+00A0 + 'artículo'",
                  "keeps ordinary spaces", "whitespace-insensitive", "'descrito )'"):
        assert place in line, place
    by_id = {r["id"]: r for r in rows(MX)}
    for r in by_id.values():
        for key in ("quote", "lead_in"):
            assert " " not in r.get(key, ""), (r["id"], key)


MX_INR_LEAD_IN = ("Es posible que el programa de Protección al Vendedor de PayPal se aplique cuando un comprador "
                  "reclama que: no autorizó los fondos que se enviaron desde su cuenta de PayPal ni se benefició de "
                  "ellos (lo que se denomina “reclamación por transacción no autorizada”) y que la transacción no "
                  "autorizada ocurrió en un entorno alojado por PayPal; o que no recibió el artículo que le envió (lo "
                  "que se denomina “reclamación por artículo no recibido”) siempre y cuando,")


def test_the_mx_card_issuer_row_has_the_page_sentence_start_as_its_lead_in():
    """The quote is a lowercase mid-sentence fragment; its lead_in is the start of the
    same page sentence, so lead_in + quote read as the page's whole sentence and no clause dangles."""
    row = {r["id"]: r for r in rows(MX)}["inr_card_issuer_chargeback"]
    assert row["lead_in"] == MX_INR_LEAD_IN
    assert row["lead_in"][0].isupper() and row["lead_in"].endswith("siempre y cuando,")
    assert row["quote"][0].islower() and row["quote"].endswith(".")
    assert [r["id"] for r in rows(MX) if r.get("lead_in")] == ["inr_card_issuer_chargeback", "snad_not_eligible"]
    assert [r["id"] for r in rows(US) if r.get("lead_in")] == ["snad_not_eligible"]          # no US row edited


# The plain-text extracts of the two 2026-10-06 page downloads, kept as private evidence (force-added; never exported).
def _page_text(country):
    path = ROOT / "evidence" / "manual" / f"2026-10-06_paypal-seller-protection-{country.lower()}_page-text.txt"
    if not path.exists() and not is_private_checkout(ROOT):
        pytest.skip("private evidence file, not published")       # in a private checkout an absent file still fails
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _fold(text):
    """U+00A0 folded to a space, then every whitespace run removed (the extract adds spaces at tag boundaries)."""
    return re.sub(r"\s+", "", text.replace(" ", " "))


# Which strings are found in the extract byte for byte; every other one only after _fold (named in the MX note).
EXACT_IN_EXTRACT = {("US", "inr_card_issuer_chargeback", "quote"), ("US", "snad_not_eligible", "lead_in"),
                    ("US", "snad_not_eligible", "quote"), ("US", "unauthorized_shipment_within_two_days", "quote"),
                    ("MX", "inr_card_issuer_chargeback", "quote"), ("MX", "snad_not_eligible", "lead_in"),
                    ("MX", "unauthorized_shipment_within_two_days", "quote")}
# Not exact in the extract: MX snad_not_eligible quote (the extract has 'descrito )') and MX inr_card_issuer_chargeback
# lead_in (the extract has spaces inside the curly quotes). Against the page HTML the MX snad lead_in and quote differ
# only by U+00A0 (the note names the three places).


def test_every_quote_and_lead_in_is_in_the_kept_page_text_with_u00a0_folded_to_space_and_whitespace_ignored():
    exact = set()
    for rules in (US, MX):
        body = _page_text(rules.country)
        assert rules.raw["eligibility_notes"]["rows"]
        for r in rows(rules):
            assert r["citation"]["section"] in body, (rules.country, r["id"])
            for key in ("lead_in", "quote"):
                if key not in r:
                    continue
                assert _fold(r[key]) in _fold(body), (rules.country, r["id"], key)
                if r[key] in body:
                    exact.add((rules.country, r["id"], key))
    assert exact == EXACT_IN_EXTRACT


# Where the US page HTML has U+00A0 (two per string; the US note names them), the file keeps ordinary spaces.
US_NBSP_PLACES = {("snad_not_eligible", "lead_in"): (("transactions", "are"), ("not", "eligible")),
                  ("snad_not_eligible", "quote"): (("a", "Significantly"), ("Described", "claim).")),
                  ("unauthorized_shipment_within_two_days", "quote"): (("of", "Unauthorized"), ("Transaction", "claim,"))}


def test_the_us_note_states_the_u00a0_places_and_the_fold_matches_the_page_form():
    """The US note's dated line names the six U+00A0 places; the page form of each
    string (with U+00A0 there) folds to the file's string and is found in the kept text after the same fold."""
    line = US.raw["verification_note"].splitlines()[-2]          # [-1] is the RC43-PK clause line
    assert line.startswith("2026-10-07 (FIX-RONDA4): whitespace only, no quote edited.")
    for (before, after) in (p for places in US_NBSP_PLACES.values() for p in places):
        assert f"'{before}' + U+00A0 + '{after}'" in line, (before, after)
    assert "keeps ordinary spaces" in line and "fold U+00A0 to a space" in line
    assert "inr_card_issuer_chargeback quote, is exact" in line
    body = _page_text("US")
    by_id = {r["id"]: r for r in rows(US)}
    for (row_id, key), places in US_NBSP_PLACES.items():
        s = by_id[row_id][key]
        assert " " not in s, (row_id, key)
        page_form = s
        for before, after in places:
            assert page_form.count(f"{before} {after}") == 1, (row_id, key, before)
            page_form = page_form.replace(f"{before} {after}", f"{before} {after}")
        assert page_form.count(" ") == 2 and page_form != s
        assert _fold(page_form) == _fold(s) and _fold(page_form) in _fold(body), (row_id, key)
    for rules in (US, MX):
        for r in rows(rules):
            for key in ("quote", "lead_in"):
                assert " " not in r.get(key, ""), (rules.country, r["id"], key)


# ---- evaluation on document-shaped disputes -----------------------------------------------------------------------
def test_card_issuer_channel_with_item_not_received_shows_the_us_row():
    notes = notes_for(dispute(INR, "EXTERNAL"), US)
    assert [n.id for n in notes] == ["inr_card_issuer_chargeback"]
    assert notes[0].quote == US_QUOTES["inr_card_issuer_chargeback"]
    assert notes[0].matched_on == ("Buyer says they did not receive the item or service",
                                   "The buyer filed it with their card issuer or bank")


def test_not_as_described_shows_its_row_on_any_channel():
    for channel in ("INTERNAL", "EXTERNAL"):
        assert [n.id for n in notes_for(dispute(SNAD, channel), US)] == ["snad_not_eligible"]


def test_unauthorised_shows_its_row():
    notes = notes_for(dispute(UNAUTH), US)
    assert [n.id for n in notes] == ["unauthorized_shipment_within_two_days"]
    assert "Basic Requirements" in notes[0].citation_text and "2026-01-26" in notes[0].citation_text


def test_plain_item_not_received_with_the_internal_channel_shows_nothing():
    assert notes_for(dispute(INR, "INTERNAL"), US) == []
    assert notes_for(dispute(INR, "INTERNAL"), MX) == []
    assert notes_for(dispute(INR, "ALERT"), US) == []                 # a pre-chargeback alert is not a filed chargeback


def test_missing_fields_or_no_rules_show_nothing():
    assert notes_for({}, US) == [] and notes_for(None, US) == [] and notes_for(dispute(SNAD), None) == []
    no_reason = dispute(SNAD)
    del no_reason["reason"]
    assert notes_for(no_reason, US) == []


def test_the_mx_file_gives_its_own_rows():
    assert [n.id for n in notes_for(dispute(INR, "EXTERNAL"), MX)] == ["inr_card_issuer_chargeback"]
    assert [n.quote_lang for n in notes_for(dispute(UNAUTH), MX)] == ["es"]
    assert "Programa de Protección al Vendedor de PayPal" in notes_for(dispute(SNAD), MX)[0].citation_text


def test_a_rule_set_without_the_section_has_no_notes():
    bare = dataclasses.replace(US, raw={k: v for k, v in US.raw.items() if k != "eligibility_notes"})
    assert rows(bare) == [] and notes_for(dispute(SNAD), bare) == []


@pytest.mark.parametrize("name", m1.case_names())
def test_checklist_output_is_byte_identical_with_and_without_the_notes(name):
    case = m1.load_case(name)
    for rules in (US, MX):
        bare = dataclasses.replace(rules, raw={k: v for k, v in rules.raw.items() if k != "eligibility_notes"})
        a = evaluate(m1.build_context(case), m1.build_docs(case), rules, today=m1.today_of(case))
        b = evaluate(m1.build_context(case), m1.build_docs(case), bare, today=m1.today_of(case))
        assert repr(a).encode("utf-8") == repr(b).encode("utf-8")


# ---- review page ----------------------------------------------------------------------------------------------------
def _use(monkeypatch, rules):
    monkeypatch.setattr(checklist_input, "_RULES_CACHE", [rules])


def _package():
    db = config.db_path()
    repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    return repo.insert_package(db, DID, source="human_edit", validation_ok=False, review_flags=[])


def _page(body, pid):
    c, _ = _web(Fake(detail=body))
    return c.get(f"/disputes/{DID}/packages/{pid}")


def _block(text):
    m = re.search(r'<section class="panel" id="eligibility-notes" data-state="eligibility-note">(.*?)</section>', text,
                  re.S)
    return m.group(1) if m else None


def _checks_table(text):
    start = text.index('<h2 id="checks">Checks</h2>')
    table = text[start:text.index("</table>", start)]
    return re.sub(r'name="csrf_token" value="[^"]*"', "", table)


def test_page_shows_the_block_with_the_hook_and_the_quote_once_when_a_row_matches(monkeypatch):
    _use(monkeypatch, US)
    r = _page(dispute(INR, "EXTERNAL"), _package())
    assert r.status_code == 200
    block = _block(r.text)
    assert block is not None and r.text.count('data-state="eligibility-note"') == 1
    assert "<h2>Eligibility notes</h2>" in block
    quote = US_QUOTES["inr_card_issuer_chargeback"]
    assert html.unescape(r.text).count(quote) == 1 and quote in html.unescape(block)
    assert EFFECT_SENTENCE in html.unescape(block)
    assert "What’s Eligible" in html.unescape(block) and "last updated 2026-01-26" in block
    assert 'data-eligibility-note="inr_card_issuer_chargeback"' in block and '<blockquote lang="en">' in block
    assert r.text.index('data-state="eligibility-note"') > r.text.index('<h2 id="checks">Checks</h2>')   # After the checks


def test_the_block_is_informational_only(monkeypatch):
    _use(monkeypatch, US)
    block = _block(_page(dispute(SNAD), _package()).text)
    assert block is not None
    for forbidden in ("<form", "<button", "<input"):
        assert forbidden not in block, forbidden
    # The one link allowed is the source page beside each quote (no action, it opens PayPal's page)
    assert block.count("<a ") == block.count("href=") == len(re.findall(r"data-eligibility-note=", block)) == 1
    own = html.unescape(re.sub(r"<a [^>]*>[^<]*</a>", "", block))                      # the app's own words: everything except PayPal's quoted text
    for quoted in (US_QUOTES["snad_not_eligible"], rows(US)[1]["lead_in"], "Ineligible Items and Transactions"):
        own = own.replace(quoted, "")
    for verdict in ("you will lose", "you will win", "not covered", "ineligible", "not eligible", "denied",
                    "approved", "you must"):
        assert verdict not in own.lower(), verdict
    assert html.unescape(block).count(US_QUOTES["snad_not_eligible"]) == 1


def test_the_block_is_absent_not_empty_without_a_match(monkeypatch):
    _use(monkeypatch, US)
    r = _page(dispute(INR, "INTERNAL"), _package())
    assert r.status_code == 200
    assert "eligibility-note" not in r.text and "Eligibility notes" not in r.text and "eligibility-notes" not in r.text


def test_the_block_is_absent_on_the_package_not_found_page(monkeypatch):
    _use(monkeypatch, US)
    r = _page(dispute(SNAD), 999999)
    assert r.status_code == 404 and "eligibility-note" not in r.text


def test_the_checks_table_is_byte_identical_with_and_without_a_matching_row(monkeypatch):
    _use(monkeypatch, US)
    pid = _package()
    matched = _page(dispute(INR, "EXTERNAL"), pid)
    plain = _page(dispute(INR, "INTERNAL"), pid)
    assert _block(matched.text) is not None and _block(plain.text) is None
    assert _checks_table(matched.text).encode("utf-8") == _checks_table(plain.text).encode("utf-8")


def test_quote_and_citation_are_html_escaped(monkeypatch):
    raw = copy.deepcopy(US.raw)
    row = raw["eligibility_notes"]["rows"][1]
    row["quote"] = 'A <script>alert(1)</script> & "quoted" text'
    row["lead_in"] = "<b>lead</b>"
    row["citation"] = {**row["citation"], "section": "<i>Section</i>"}
    _use(monkeypatch, dataclasses.replace(US, raw=raw))
    block = _block(_page(dispute(SNAD), _package()).text)
    assert block is not None
    assert "<script>" not in block and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;quoted&#34; text" in block
    assert "<b>lead</b>" not in block and "&lt;b&gt;lead&lt;/b&gt;" in block
    assert "<i>Section</i>" not in block and "&lt;i&gt;Section&lt;/i&gt;" in block


@pytest.mark.parametrize("rules", [US, MX], ids=["US", "MX"])
def test_each_note_shows_title_date_section_and_the_source_link_once(monkeypatch, rules):
    """Beside each quote, the page title, its date, the section and one link, opened
    with rel="noopener" and no tracking parameters. The href is the row's citation.url, the page the quote
    was read from (for MX the legal-hub page, not the PDF that is the rule file's source_url)."""
    _use(monkeypatch, rules)
    for body, ids in ((dispute(INR, "EXTERNAL"), ["inr_card_issuer_chargeback"]), (dispute(SNAD), ["snad_not_eligible"]),
                      (dispute(UNAUTH), ["unauthorized_shipment_within_two_days"])):
        block = _block(_page(body, _package()).text)
        assert block is not None
        notes = re.findall(r'<div data-eligibility-note="([^"]+)">(.*?)</div>', block, re.S)
        assert [i for i, _ in notes] == ids
        for note_id, note in notes:
            row = {r["id"]: r for r in rows(rules)}[note_id]
            source = re.search(r"<small>Source: (.*?)</small>", note, re.S).group(1)
            text = html.unescape(source)
            for part in (row["citation"]["page_title"], row["citation"]["page_date"], row["citation"]["section"]):
                assert part in text, part
            links = re.findall(r'<a href="([^"]*)" rel="noopener">', source)
            assert links == [row["citation"]["url"]] and note.count("<a ") == 1
            assert links == [f"https://www.paypal.com/{rules.country.lower()}/legalhub/paypal/seller-protection"]
            assert "?" not in links[0] and "utm_" not in links[0] and not links[0].endswith(".pdf")


def test_the_note_carries_the_row_citation_url():
    for rules, cc in ((US, "us"), (MX, "mx")):
        for body in (dispute(INR, "EXTERNAL"), dispute(SNAD), dispute(UNAUTH)):
            for n in notes_for(body, rules):
                assert n.citation_url == {r["id"]: r for r in rows(rules)}[n.id]["citation"]["url"]
                assert n.citation_url == f"https://www.paypal.com/{cc}/legalhub/paypal/seller-protection"
    assert MX.source_url.endswith(".pdf") and notes_for(dispute(SNAD), MX)[0].citation_url != MX.source_url


def test_the_link_falls_back_to_source_url_only_when_the_row_has_no_url(monkeypatch):
    raw = copy.deepcopy(MX.raw)
    row = raw["eligibility_notes"]["rows"][1]
    row["citation"] = {k: v for k, v in row["citation"].items() if k != "url"}
    _use(monkeypatch, dataclasses.replace(MX, raw=raw))
    block = _block(_page(dispute(SNAD), _package()).text)
    assert re.findall(r'<a href="([^"]*)" rel="noopener">', block) == [MX.source_url]


def test_the_mx_card_issuer_block_shows_the_lead_in_before_the_quote(monkeypatch):
    """Probe: the rendered MX card-issuer block opens with the sentence start, not a lowercase fragment."""
    _use(monkeypatch, MX)
    block = _block(_page(dispute(INR, "EXTERNAL"), _package()).text)
    quoted = re.search(r'<blockquote lang="es">(.*?)</blockquote>', block, re.S).group(1)
    paragraphs = [html.unescape(p) for p in re.findall(r"<p>(.*?)</p>", quoted, re.S)]
    assert paragraphs == [MX_INR_LEAD_IN, rows(MX)[0]["quote"]]
    assert paragraphs[0].startswith("Es posible que")


def test_the_mx_rule_set_shows_its_spanish_quote_marked_as_spanish(monkeypatch):
    _use(monkeypatch, MX)
    block = _block(_page(dispute(UNAUTH), _package()).text)
    assert block is not None and '<blockquote lang="es">' in block
    assert "dos días" in html.unescape(block)


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
