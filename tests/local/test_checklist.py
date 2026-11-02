"""Offline tests for copilot/core/checklist.py: one per interpretation rule plus every case of the copilot/fixtures/m1 set."""
import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot.core.checklist import ACTIONS, BASES, STATES, Check, ExtractedDoc, evaluate, load_rules

_spec = importlib.util.spec_from_file_location("m1_loader", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)

RULES = load_rules()


def run(case, docs=None, ctx=None):
    return {c.id: c for c in evaluate(ctx or m1.build_context(case), docs if docs is not None
                                      else m1.build_docs(case), RULES, today=m1.today_of(case))}


def case_run(name):
    case = m1.load_case(name)
    return case, run(case)


@pytest.mark.parametrize("name", [n for n in m1.case_names()])
def test_fixture_expected_states(name):
    case, checks = case_run(name)
    for cid, state in case["expected"].items():
        assert checks[cid].state == state, (cid, checks[cid].seller_message)
    for cid in case["expected_risk"]:
        assert checks[cid].risk, cid


@pytest.mark.parametrize("name", m1.case_names())
def test_every_check_is_well_formed_and_actionable(name):
    _, checks = case_run(name)
    for c in checks.values():
        assert isinstance(c, Check)
        assert c.state in STATES and c.basis in BASES and c.action in ACTIONS
        assert c.label_for_seller and c.seller_message and c.basis_ref
        if c.basis == "published_policy":
            assert "2025-01-13" in c.basis_ref and "paypalobjects.com" in c.basis_ref
        if c.state in ("missing", "conflict") and c.basis != "app_quality_rule" and c.id != "receiver_name":
            assert c.action != "none", c.id


def test_fixtures_are_marked_synthetic():
    for n in m1.case_names():
        assert "SYNTHETIC" in m1.load_case(n)["_synthetic"]


# ---- interpretation rules
def test_tracking_not_registered_in_paypal_is_not_a_contradiction():
    _, c = case_run("v_tracking_not_in_paypal")
    assert c["tracking_number"].state == "present_unverifiable"
    assert c["tracking_number"].state != "conflict"


def test_tracking_that_differs_from_paypals_is_a_conflict():
    _, c = case_run("c03_other_order")
    assert c["tracking_number"].state == "conflict"


def test_absent_illegible_unverifiable_and_conflict_are_distinct_states():
    _, absent = case_run("c02_missing_carrier_proof")
    _, illegible = case_run("c06_illegible")
    _, conflict = case_run("c03_other_order")
    assert absent["tracking_number"].state == "missing"
    assert illegible["tracking_number"].state == "present_unverifiable"
    assert conflict["tracking_number"].state == "conflict"
    assert "legible" in illegible["tracking_number"].seller_message


def test_seller_declared_address_is_labelled_and_never_silently_replaces_paypals():
    case, c = case_run("v_seller_declared_address")
    d = c["destination"]
    assert d.state == "present_unverifiable" and "seller_statement" in d.sources
    assert "not from PayPal" in d.seller_message
    # and when PayPal does have an address, a conflicting declared one is ignored for the comparison
    case2 = m1.load_case("c01_complete")
    ctx = m1.build_context(case2).with_seller_statement("shipping_address", {"postal_code": "99999"})
    out = run(case2, ctx=ctx)
    assert out["destination"].state == "verified" and "seller_statement" not in out["destination"].sources


def test_document_without_street_is_present_unverifiable_not_failure():
    _, c = case_run("v_no_street")
    assert c["full_address"].state == "present_unverifiable"
    assert c["destination"].state == "verified"
    assert c["full_address"].action == "none" and not c["full_address"].risk


def test_city_and_state_are_enough_without_postal_code():
    case = m1.load_case("c01_complete")
    docs = m1.build_docs(case)
    docs[0].fields["postal_code"] = {"value": None, "confidence": "absent"}
    docs[0].fields["state"] = {"value": "Ciudad de México", "confidence": "read"}
    assert run(case, docs)["destination"].state == "verified"


def test_invoice_dated_after_delivery_is_not_a_conflict():
    _, c = case_run("c04b_invoice_after_delivery")
    assert [x.id for x in c.values() if x.state == "conflict"] == []


def test_delivery_before_shipping_is_a_conflict():
    _, c = case_run("c04a_contradictory_dates")
    assert c["dates_coherent"].state == "conflict" and c["dates_coherent"].action == "review_difference"


def test_delivery_before_payment_is_a_conflict():
    case = m1.load_case("c01_complete")
    docs = m1.build_docs(case)
    docs[0].fields["delivery_date"] = {"value": "2026-09-01", "confidence": "read"}
    docs[0].fields["ship_date"] = {"value": "2026-08-30", "confidence": "read"}
    assert run(case, docs)["delivery_date"].state == "conflict"


def test_unknown_carrier_is_other_with_risk_and_does_not_invalidate():
    _, c = case_run("v_carrier_other")
    assert c["carrier"].state == "verified" and c["carrier"].risk
    assert "OTHER" in c["carrier"].seller_message
    assert c["delivery_status"].state == "verified"


def test_aggregator_carrier_notes_to_use_the_underlying_carrier():
    _, c = case_run("c10_branch_pickup")
    assert c["carrier"].risk and "underlying carrier" in c["carrier"].seller_message


def test_no_verified_deadline_text_comes_from_disputes_layer():
    from copilot.core.disputes import humanize_deadline
    assert humanize_deadline({}) == "No verified deadline available"


# ---- signature rule
def _set_total(case, currency, value):
    ti = case["paypal"]["transactions"]["transaction_details"][0]["transaction_info"]
    ti["transaction_amount"] = {"currency_code": currency, "value": value}


@pytest.mark.parametrize("currency,value,expected", [
    ("MXN", "10000.00", "not_applicable"),      # exactly the limit: "supera" = strictly greater
    ("MXN", "10000.01", "present_unverifiable"),  # paper_only signature in the fixture -> required, unverifiable
    ("USD", "750.00", "not_applicable"),
    ("USD", "750.01", "present_unverifiable"),
    ("JPY", "77000", "not_applicable"),         # zero-decimal currency: whole units
    ("JPY", "77001", "present_unverifiable"),
    ("EUR", "550.00", "not_applicable"),
    ("EUR", "550.01", "present_unverifiable"),
])
def test_signature_limit_is_per_currency_and_strictly_greater(currency, value, expected):
    case = m1.load_case("c11_paper_signature_12500")
    _set_total(case, currency, value)
    c = run(case)["signature"]
    assert c.state == expected, (currency, value, c.seller_message)
    if expected == "present_unverifiable":
        assert c.risk and c.action == "add_document"   # required, found only on paper


def test_amount_above_limit_without_any_signature_is_missing():
    case = m1.load_case("c11_paper_signature_12500")
    _set_total(case, "USD", "750.01")
    case["docs"][0]["fields"]["signature"] = {"value": None, "confidence": "absent"}
    assert run(case)["signature"].state == "missing"


def test_unknown_currency_signature_is_present_unverifiable_and_not_converted():
    case = m1.load_case("c11_paper_signature_12500")
    _set_total(case, "XYZ", "999999.00")
    c = run(case)["signature"]
    assert c.state == "present_unverifiable" and c.action == "none" and not c.risk
    assert "XYZ" in c.seller_message and "never converted" in c.seller_message


def test_unknown_amount_signature_is_present_unverifiable():
    case = m1.load_case("c11_paper_signature_12500")
    ctx = m1.build_context(case)
    ctx = dataclasses.replace(ctx, fields={k: v for k, v in ctx.fields.items()
                                           if k not in ("total_amount_cents", "total_currency")})
    c = run(case, ctx=ctx)["signature"]
    assert c.state == "present_unverifiable" and "unknown" in c.seller_message


def test_usd_25_does_not_need_a_signature():
    _, c = case_run("v_non_mxn")
    assert c["signature"].state == "not_applicable"


def test_usd_800_requires_a_signature():
    _, c = case_run("v_usd_800_signature")
    assert c["signature"].state == "present_unverifiable" and c["signature"].risk
    assert c["signature"].action == "add_document"


def test_signature_limits_table_matches_the_pdf_and_money_convention():
    pdf = {"AUD": 850, "NZD": 950, "BRL": 1750, "NOK": 4600, "CAD": 850, "PHP": 34000, "CZK": 15000,
           "PLN": 2300, "DKK": 4100, "RUB": 48000, "EUR": 550, "SGD": 950, "HKD": 6000, "SEK": 4950,
           "HUF": 170000, "CHF": 700, "ILS": 2700, "TWD": 23000, "JPY": 77000, "THB": 24500, "MYR": 3100,
           "GBP": 450, "MXN": 10000, "USD": 750}
    assert set(RULES.signature_limits) == set(pdf)
    from step3_dispute import _ZERO_DECIMAL
    for cur, whole in pdf.items():
        entry = RULES.signature_limits[cur]
        assert entry["minor_units"] == (0 if cur in _ZERO_DECIMAL else 2), cur
        assert entry["limit_minor"] == whole * 10 ** entry["minor_units"], cur


def test_city_and_country_match_is_accepted():
    case = m1.load_case("c01_complete")
    docs = m1.build_docs(case)
    for k in ("postal_code", "state"):
        docs[0].fields[k] = {"value": None, "confidence": "absent"}
    docs[0].fields["country"] = {"value": "México", "confidence": "read"}
    c = run(case, docs=docs)["destination"]
    assert c.state == "verified" and "city/country" in c.seller_message


def test_city_and_country_mismatch_is_a_conflict():
    case = m1.load_case("c01_complete")
    docs = m1.build_docs(case)
    for k in ("postal_code", "state"):
        docs[0].fields[k] = {"value": None, "confidence": "absent"}
    docs[0].fields["country"] = {"value": "United States", "confidence": "read"}
    assert run(case, docs=docs)["destination"].state == "conflict"

def test_paper_only_signature_above_threshold_is_flagged_risk():
    _, c = case_run("c11_paper_signature_12500")
    assert c["signature"].state == "present_unverifiable" and c["signature"].risk


def test_online_signature_above_threshold_verifies():
    case = m1.load_case("c11_paper_signature_12500")
    case["docs"][0]["fields"]["signature"] = {"value": "online", "confidence": "read"}
    assert run(case)["signature"].state == "verified"


def test_no_signature_above_threshold_is_missing():
    case = m1.load_case("c11_paper_signature_12500")
    case["docs"][0]["fields"]["signature"] = {"value": None, "confidence": "absent"}
    assert run(case)["signature"].state == "missing"


# ---- delivery state
def test_in_transit_is_missing_with_check_status_not_a_conflict():
    _, c = case_run("c05_in_transit")
    assert c["delivery_status"].state == "missing" and c["delivery_status"].action == "check_status"
    assert "do not state it was delivered" in c["delivery_status"].seller_message


def test_expired_tracking_asks_for_a_new_document():
    _, c = case_run("c09_expired_tracking")
    assert c["delivery_status"].state == "missing" and c["delivery_status"].action == "add_document"


def test_branch_pickup_with_branch_postal_code_is_not_a_failure_but_a_risk():
    _, c = case_run("c10_branch_pickup")
    assert c["destination"].state == "present_unverifiable" and c["destination"].risk
    assert c["branch_pickup"].risk


def test_injected_instruction_is_not_obeyed_and_is_flagged():
    _, c = case_run("c07_injection")
    assert c["delivery_status"].state == "missing"          # the document did not turn it into verified
    assert c["document_integrity"].risk and c["document_integrity"].action == "review_difference"


def test_old_capture_date_raises_our_own_caution_not_a_paypal_requirement():
    case = m1.load_case("c01_complete")
    case["today"] = "2026-12-01"
    c = run(case)["capture_date"]
    assert c.risk and c.basis == "app_quality_rule" and "not a PayPal requirement" in c.seller_message


# ---- basis
def test_basis_is_declared_and_quality_rules_are_not_presented_as_policy():
    _, c = case_run("c01_complete")
    assert c["tracking_number"].basis == "published_policy"
    assert c["capture_date"].basis == "app_quality_rule" and c["dates_coherent"].basis == "app_quality_rule"
    assert c["case_request:PROOF_OF_FULFILLMENT"].basis == "case_request"
    assert "dispute evidences" in c["case_request:PROOF_OF_FULFILLMENT"].basis_ref


def test_case_request_is_verified_once_submitted():
    case = m1.load_case("c01_complete")
    case["dispute_norm"]["evidence_submitted_by_seller"] = ["PROOF_OF_FULFILLMENT"]
    assert run(case)["case_request:PROOF_OF_FULFILLMENT"].state == "verified"


def test_rules_file_declares_version_country_and_source():
    assert RULES.country == "MX" and RULES.effective_date == "2025-01-13"
    assert RULES.source_url.startswith("https://www.paypalobjects.com/")


def test_evaluate_is_deterministic():
    case = m1.load_case("c10_branch_pickup")
    assert run(case) == run(case)


def test_rules_can_switch_a_check_off():
    import dataclasses
    checks = {k: v for k, v in RULES.checks.items() if k != "receiver_name"}
    rules = dataclasses.replace(RULES, checks=checks)
    case = m1.load_case("c01_complete")
    ids = [c.id for c in evaluate(m1.build_context(case), m1.build_docs(case), rules, today=m1.today_of(case))]
    assert "receiver_name" not in ids and "tracking_number" in ids


def test_checks_the_pdf_does_not_state_are_app_quality_rules():
    _, c = case_run("c10_branch_pickup")
    for cid in ("carrier", "receiver_name", "capture_date", "branch_pickup", "dates_coherent",
                "document_integrity"):
        assert c[cid].basis == "app_quality_rule", cid
        assert "PayPal Seller Protection" not in c[cid].basis_ref, cid
    for cid in ("tracking_number", "delivery_status", "delivery_date", "destination", "full_address",
                "signature"):
        assert c[cid].basis == "published_policy", cid
        assert "(2025-01-13), p." in c[cid].basis_ref, cid


def test_rules_file_records_the_reading_and_hash():
    note = RULES.raw["verification_note"]
    assert "2026-10-04" in note
    assert "2F27175B3C0928D79E782D4683BC078C6C347A8ED4800633D8D7911DA65CDEAE" in note


# ---- per-country rule sets (MX case study + US)
from copilot.core.checklist import UnknownCountryError, supported_countries

US_RULES = load_rules(country="US")


def test_us_rule_set_loads_and_default_stays_mx():
    assert US_RULES.country == "US" and US_RULES.effective_date == "2026-01-26"
    assert US_RULES.rule_set_id == "us_seller_protection_2026-01-26"
    assert load_rules().country == "MX" and load_rules(country="mx").country == "MX"
    assert set(US_RULES.checks) == set(RULES.checks)
    assert supported_countries() == ["MX", "US"]


def test_unknown_country_is_an_explicit_error_not_a_default():
    with pytest.raises(UnknownCountryError) as e:
        load_rules(country="ZZ")
    assert "ZZ" in str(e.value) and "MX" in str(e.value) and "US" in str(e.value)
    with pytest.raises(UnknownCountryError):
        load_rules(country="")


def test_us_published_policy_checks_have_section_citations():
    for cid, rule in US_RULES.checks.items():
        assert rule["basis"] in BASES, cid
        if rule["basis"] == "published_policy":
            assert "2026-01-26" in rule["citation"] and "'" in rule["citation"], cid   # quoted section heading
            assert "Basic Requirements" in rule["citation"] or "Establishing Proof" in rule["citation"] \
                or "Item Not Received" in rule["citation"], cid
    assert US_RULES.source_url.startswith("https://www.paypal.com/us/legalhub/")


def _usd800(case):
    _set_total(case, "USD", "800.00")
    return case


def test_signature_differs_between_mx_and_us_on_800_usd():
    case = _usd800(m1.load_case("c11_paper_signature_12500"))
    mx = {c.id: c for c in evaluate(m1.build_context(case), m1.build_docs(case), RULES, today=m1.today_of(case))}
    us = {c.id: c for c in evaluate(m1.build_context(case), m1.build_docs(case), US_RULES, today=m1.today_of(case))}
    assert mx["signature"].state == "present_unverifiable"      # above MX's 750 USD limit, paper only
    assert us["signature"].state == "not_applicable" and us["signature"].basis == "published_policy"
    assert "2026-01-26" in us["signature"].basis_ref
    # exactly the signature check differs in state; everything else is identical for this fixture
    diff = {cid for cid in mx if (mx[cid].state, mx[cid].risk) != (us[cid].state, us[cid].risk)}
    assert diff == {"signature"}
