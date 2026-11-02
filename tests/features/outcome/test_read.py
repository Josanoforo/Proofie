"""read_outcome on synthetic disputes: decided and undecided, labels by code, refund cents, appeal link, failures."""
import httpx
import pytest

from copilot.features.outcome import read
from outcome_fakes import DECIDED, DISPUTE_ID, FakePayPal, dispute_raw, paypal_error


def test_decided_fixture_gives_the_code_the_label_the_refund_in_cents_and_the_appeal_link():
    r = read.read_outcome(FakePayPal(dispute_raw(**DECIDED)), DISPUTE_ID)
    assert (r.stage, r.status, r.outcome_code, r.decided) == ("CHARGEBACK", "RESOLVED", "RESOLVED_BUYER_FAVOUR", True)
    assert r.amount_refunded_cents == 1050 and r.appeal_link_present and r.links == ("appeal",)
    assert r.label == "Resolved in the buyer's favor" and r.seller_response_due_date == "2026-10-17T05:59:59.000Z"
    assert r.limitations == () and r.norm["dispute_id"] == DISPUTE_ID


def test_undecided_fixture_has_no_code_and_is_not_decided():
    r = read.read_outcome(FakePayPal(dispute_raw()), DISPUTE_ID)
    assert (r.decided, r.outcome_code, r.amount_refunded_cents, r.label, r.appeal_link_present) == \
        (False, None, None, None, False)
    assert r.links == ("provide-evidence",)


def test_the_read_is_one_get_with_the_outcome_op():
    fake = FakePayPal()
    read.read_outcome(fake, DISPUTE_ID)
    assert [(c[0], c[1], c[2]) for c in fake.calls] == [("GET", f"/v1/customer/disputes/{DISPUTE_ID}", "outcome_read")]


@pytest.mark.parametrize("code,label", [
    ("RESOLVED_BUYER_FAVOUR", "Resolved in the buyer's favor"), ("RESOLVED_SELLER_FAVOUR", "Resolved in the seller's favor"),
    ("RESOLVED_WITH_PAYOUT", "Resolved with a payout"), ("CANCELED_BY_BUYER", "Canceled by the buyer"),
    ("ACCEPTED", "Accepted"), ("DENIED", "Denied"), ("NONE", "No outcome"), ("OTHER", "Other outcome"),
    ("SOMETHING_NEW", "Outcome code SOMETHING_NEW")])
def test_outcome_codes_to_labels(code, label):
    assert read.outcome_label(code) == label
    r = read.read_outcome(FakePayPal(dispute_raw(status="UNDER_REVIEW", rels=(), outcome={"outcome_code": code})), DISPUTE_ID)
    assert r.outcome_code == code and r.label == label
    assert r.decided is (code != "NONE")


def test_a_resolved_status_without_an_outcome_block_is_decided():
    r = read.read_outcome(FakePayPal(dispute_raw(status="RESOLVED", rels=())), DISPUTE_ID)
    assert r.decided and r.outcome_code is None and r.label is None


@pytest.mark.parametrize("value,cents", [("10.50", 1050), ("10", 1000), ("0.05", 5), ("1234.56", 123456)])
def test_the_refund_amount_converts_to_integer_cents_by_code(value, cents):
    outcome = {"outcome_code": "RESOLVED_WITH_PAYOUT", "amount_refunded": {"currency_code": "USD", "value": value}}
    assert read.read_outcome(FakePayPal(dispute_raw(status="RESOLVED", rels=(), outcome=outcome)),
                             DISPUTE_ID).amount_refunded_cents == cents


def test_an_unreadable_refund_amount_is_none_with_a_limitation():
    outcome = {"outcome_code": "ACCEPTED", "amount_refunded": {"currency_code": "USD", "value": "1.005"}}
    r = read.read_outcome(FakePayPal(dispute_raw(status="RESOLVED", rels=(), outcome=outcome)), DISPUTE_ID)
    assert r.amount_refunded_cents is None and len(r.limitations) == 1


def test_appeal_link_detection_needs_the_appeal_rel_only():
    assert read.read_outcome(FakePayPal(dispute_raw(rels=("appeal", "send_message"))), DISPUTE_ID).appeal_link_present
    assert not read.read_outcome(FakePayPal(dispute_raw(rels=("send_message",))), DISPUTE_ID).appeal_link_present


@pytest.mark.parametrize("error", [paypal_error(500), paypal_error(403), httpx.ReadTimeout("slow")])
def test_a_paypal_failure_is_a_limitation_not_an_exception(error):
    r = read.read_outcome(FakePayPal(get_raises=error), DISPUTE_ID)
    assert r.decided is False and r.norm is None and len(r.limitations) == 1 and "dbg-1" not in r.limitations[0].message


def test_a_programming_error_still_raises():
    with pytest.raises(KeyError):
        read.read_outcome(FakePayPal(get_raises=KeyError("bug")), DISPUTE_ID)
