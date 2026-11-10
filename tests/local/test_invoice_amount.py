"""Offline tests for the invoice_amount check (Proofie's own rule): the invoice total equals the PayPal transaction amount."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot.core.checklist import ExtractedDoc, evaluate, load_rules
from copilot.core.field_extraction import extract_fields

_spec = importlib.util.spec_from_file_location("m1_loader", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)

CASE = m1.load_case("c01_complete")          # PayPal transaction amount 4500.00 MXN, from transaction search
RULES = {"MX": load_rules(country="MX"), "US": load_rules(country="US")}


def run(fields, country="MX", ctx=None):
    docs = m1.build_docs(CASE) + [ExtractedDoc("inv", fields)]
    out = evaluate(ctx or m1.build_context(CASE), docs, RULES[country], today=m1.today_of(CASE))
    return {c.id: c for c in out}["invoice_amount"]


def f(cents, cur=None):
    d = {"invoice_total_cents": {"value": cents, "confidence": "read"}}
    if cur:
        d["invoice_currency"] = {"value": cur, "confidence": "read"}
    return d


@pytest.mark.parametrize("text,cents,cur", [
    ("Invoice total: 4,500.00 MXN", 450000, "MXN"), ("Total de la factura: MXN 4500", 450000, "MXN"),
    ("Invoice total: $1,234.5", 123450, None), ("Total factura: 750.00 USD", 75000, "USD")])
def test_extraction_reads_integer_cents_without_floats(text, cents, cur):
    got = extract_fields(text)
    assert got["invoice_total_cents"]["value"] == cents
    assert got.get("invoice_currency", {}).get("value") == cur


def test_extraction_unreadable_total_is_illegible_and_no_label_is_absent():
    assert extract_fields("Invoice total: --")["invoice_total_cents"]["confidence"] == "illegible"
    assert "invoice_total_cents" not in extract_fields("Estatus: Entregado")


@pytest.mark.parametrize("country", ["MX", "US"])
def test_equal_amount_is_verified(country):
    assert run(f(450000, "MXN"), country).state == "verified"


def test_different_amount_is_a_conflict_that_blocks():
    c = run(f(420000, "MXN"))
    assert c.state == "conflict" and c.action == "review_difference" and c.basis == "app_quality_rule"
    assert "4,200.00" in c.seller_message and "4,500.00" in c.seller_message


def test_currency_mismatch_is_a_conflict_never_converted():
    assert run(f(450000, "USD")).state == "conflict"


def test_no_invoice_total_is_not_applicable_so_existing_cases_are_unchanged():
    for name in m1.case_names():
        case = m1.load_case(name)
        out = {c.id: c for c in evaluate(m1.build_context(case), m1.build_docs(case), RULES["MX"], today=m1.today_of(case))}
        assert out["invoice_amount"].state == "not_applicable", name


def test_two_different_totals_conflict():
    docs = m1.build_docs(CASE) + [ExtractedDoc("a", f(450000)), ExtractedDoc("b", f(1))]
    out = {c.id: c for c in evaluate(m1.build_context(CASE), docs, RULES["MX"], today=m1.today_of(CASE))}
    assert out["invoice_amount"].state == "conflict"


def test_illegible_total_is_unverifiable_not_a_conflict():
    c = run({"invoice_total_cents": {"value": None, "confidence": "illegible"}})
    assert c.state == "present_unverifiable" and c.action == "fix_value"


def test_against_the_disputed_amount_a_difference_is_a_caution_that_never_blocks():
    ctx = m1.build_context(CASE)
    from copilot.core.context import Field, SRC_DISPUTE
    ctx.fields["total_amount_cents"] = Field(450000, SRC_DISPUTE)
    wrong, right = run(f(1, "MXN"), ctx=ctx), run(f(450000, "MXN"), ctx=ctx)
    assert wrong.state == "present_unverifiable" and wrong.risk and wrong.action == "none"
    assert "amount in dispute at PayPal 4,500.00 MXN" in wrong.seller_message and "could not be compared with the sale itself" in wrong.seller_message
    assert run(f(450000, "USD"), ctx=ctx).state == "present_unverifiable"          # another currency: still only a caution here
    assert right.state == "verified" and "amount in dispute at PayPal" in right.seller_message


def test_no_amount_at_all_is_unverifiable():
    ctx = m1.build_context(CASE)
    ctx.fields.pop("total_amount_cents", None)
    assert run(f(1, "MXN"), ctx=ctx).state == "present_unverifiable"
