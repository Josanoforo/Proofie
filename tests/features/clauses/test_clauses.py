"""W1-3: the clause behind each check. The US rule file quotes PayPal's page for its six published_policy checks,
verbatim from the kept 2026-10-06 page text; Proofie's own checks and PayPal's case request carry a plain label.
Offline: rule files and the kept page extract only, no network."""
from pathlib import Path

import pytest

from copilot.core.checklist import load_rules
from copilot.features.clauses.service import CASE_REQUEST, CLAUSE_KEYS, OWN_RULE, clause_for, clauses_for

ROOT = Path(__file__).resolve().parents[3]
US = load_rules(country="US")
MX = load_rules(country="MX")
PUBLISHED = ("tracking_number", "delivery_status", "delivery_date", "destination", "full_address", "signature")
OWN = ("carrier", "receiver_name", "capture_date", "dates_coherent", "branch_pickup", "document_integrity")


def _us_page_text():
    path = ROOT / "evidence" / "manual" / "2026-10-06_paypal-seller-protection-us_page-text.txt"
    if not path.exists():
        pytest.skip("private evidence file, not published")
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_six_published_checks_are_the_ones_with_a_us_clause():
    published = {cid for cid, rule in US.checks.items() if rule["basis"] == "published_policy"}
    assert published == set(PUBLISHED)
    assert {cid for cid, rule in US.checks.items() if "clause" in rule} == set(PUBLISHED)


@pytest.mark.parametrize("cid", PUBLISHED)
def test_each_us_clause_has_the_keys_and_the_page_of_the_rule_set(cid):
    clause = US.checks[cid]["clause"]
    assert tuple(clause) == CLAUSE_KEYS
    assert clause["quote_lang"] == "en"
    assert clause["page_date"] == US.effective_date and clause["url"] == US.source_url
    assert clause["page_title"] == "PayPal's Seller Protection Program" and clause["read_date"] == "2026-10-06"


def test_every_us_quote_and_section_is_in_the_kept_page_text_byte_for_byte():
    body = _us_page_text()
    for cid in PUBLISHED:
        clause = US.checks[cid]["clause"]
        assert clause["quote"] in body, cid
        assert clause["section"] in body, cid


def test_the_us_signature_clause_is_the_list_heading_that_names_no_signature():
    body = _us_page_text()
    quote = US.checks["signature"]["clause"]["quote"]
    assert quote.endswith(":")
    start = body.index(quote)
    proof_list = body[start:body.index("IMPORTANT:", start)]
    assert "signature" not in proof_list.lower() and "signed" not in proof_list.lower()


def test_clause_for_quotes_the_page_for_a_published_check():
    clause = clause_for(US, "tracking_number")
    assert clause == {"kind": "published", "quote": "An online and verifiable tracking number.", "quote_lang": "en",
                      "citation_text": "PayPal's Seller Protection Program, last updated 2026-01-26, section "
                                       "“Establishing Proof of Shipment or Proof of Delivery” (read 2026-10-06)",
                      "url": "https://www.paypal.com/us/legalhub/paypal/seller-protection"}


@pytest.mark.parametrize("rules", [US, MX], ids=["US", "MX"])
def test_own_checks_and_the_case_request_carry_their_plain_label(rules):
    for cid in OWN:
        assert rules.checks[cid]["basis"] == "app_quality_rule"
        assert clause_for(rules, cid) == {"kind": "own_rule", "text": OWN_RULE}
    assert clause_for(rules, "case_request") == {"kind": "case_request", "text": CASE_REQUEST}
    assert OWN_RULE == "Proofie's own rule" and CASE_REQUEST == "PayPal asked for this"


# ---- MX: from the kept 2026-10-06 extract of the MX legal hub page (same version as the PDF), not a re-read of the PDF
MX_HUB = "https://www.paypal.com/mx/legalhub/paypal/seller-protection"
MX_SECTION = "Comprobante de entrega o comprobante de envío"


def _mx_page_text():
    path = ROOT / "evidence" / "manual" / "2026-10-06_paypal-seller-protection-mx_page-text.txt"
    if not path.exists():
        pytest.skip("private evidence file, not published")
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _fold(text):
    """The MX file's comparison rule (verification_note, FIX-RONDA3): U+00A0 folded to a space, whitespace ignored."""
    import re
    return re.sub(r"\s+", "", text.replace(" ", " "))


def test_the_six_published_mx_checks_each_have_a_clause():
    assert {cid for cid, rule in MX.checks.items() if rule["basis"] == "published_policy"} == set(PUBLISHED)
    assert {cid for cid, rule in MX.checks.items() if "clause" in rule} == set(PUBLISHED)


@pytest.mark.parametrize("cid", PUBLISHED)
def test_each_mx_clause_has_the_keys_in_spanish_and_cites_the_hub_page(cid):
    clause = MX.checks[cid]["clause"]
    assert tuple(clause) == CLAUSE_KEYS
    assert clause["quote_lang"] == "es" and clause["section"] == MX_SECTION
    assert clause["page_title"] == "Programa de Protección al Vendedor de PayPal"
    assert clause["page_date"] == MX.effective_date == "2025-01-13"
    assert clause["url"] == MX_HUB and clause["read_date"] == "2026-10-06"
    assert " " not in clause["quote"]


def test_every_mx_quote_and_section_is_in_the_kept_page_text():
    body = _mx_page_text()
    assert MX_SECTION in body
    for cid in PUBLISHED:
        quote = MX.checks[cid]["clause"]["quote"]
        assert _fold(quote) in _fold(body), cid                     # the file's rule
        assert quote in body, cid                                   # and, for these six, byte for byte


def test_the_mx_signature_clause_is_the_signed_receipt_item_with_its_limit_table():
    quote = MX.checks["signature"]["clause"]["quote"]
    assert quote.startswith("acuse de recibo firmado si el importe total del pago")
    assert "tabla de límites del acuse de recibo firmado" in quote and quote.endswith("mediante una firma)")


def test_clause_for_quotes_the_mx_page_in_spanish():
    clause = clause_for(MX, "tracking_number")
    assert clause["kind"] == "published" and clause["quote_lang"] == "es"
    assert clause["quote"] == "un número de rastreo en línea verificable"
    assert clause["url"] == MX_HUB and MX_SECTION in clause["citation_text"]


def test_the_mx_note_gains_the_dated_clause_line_and_names_its_source():
    lines = MX.raw["verification_note"].splitlines()
    assert lines[-1].startswith("2026-10-20 (RC43-PK W1-3 MX): a `clause` object added to the six published_policy checks")
    assert "2026-10-06 page text extract" in lines[-1] and "not from a re-read of the PDF" in lines[-1]
    assert lines[-2].startswith("2026-10-06 (FIX-RONDA3)")


def test_nothing_to_show_for_an_unknown_check_or_no_rule_set():
    assert clause_for(US, "no_such_check") is None
    assert clause_for(None, "tracking_number") is None


def test_clauses_for_maps_only_the_checks_with_something_to_show():
    class C:
        def __init__(self, cid):
            self.id = cid
    import dataclasses
    bare = dataclasses.replace(US, checks={k: {kk: vv for kk, vv in v.items() if kk != "clause"}
                                           for k, v in US.checks.items()})
    shown = clauses_for(bare, [C("tracking_number"), C("carrier"), C("case_request")])
    assert set(shown) == {"carrier", "case_request"}                # a published check without a clause shows nothing
    assert set(clauses_for(US, [C(cid) for cid in US.checks])) == set(US.checks)
