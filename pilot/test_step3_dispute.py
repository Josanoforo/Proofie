"""Offline tests for the dispute normaliser. No network, no secrets.

Expected values are written from the PayPal schema (customer_disputes_v1) and from arithmetic done by hand,
not copied from the code under test.
"""
from datetime import datetime, timedelta, timezone

import pytest

from step3_dispute import MX_TZ, minor_to_str, money_to_minor, normalize, parse_utc, remaining

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
CAPTURE = "DEMOTXN0000000001"


def base_dispute(**over):
    d = {
        "dispute_id": "PP-R-TEST-0001",
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY",
        "dispute_amount": {"currency_code": "USD", "value": "25.00"},
        "disputed_transactions": [{"buyer_transaction_id": "BUYER-TX-1", "seller_transaction_id": CAPTURE}],
        "create_time": "2026-10-04T23:50:00.000Z",
        "update_time": "2026-10-04T23:55:00.000Z",
        "seller_response_due_date": "2026-10-14T12:00:00.000Z",
        "links": [{"href": "https://x/self", "rel": "self", "method": "GET"},
                  {"href": "https://x/pe", "rel": "provide-evidence", "method": "POST"},
                  {"href": "https://x/sm", "rel": "send-message", "method": "POST"}],
        "evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}],
    }
    d.update(over)
    return d


# ---- money: Decimal, never float ------------------------------------------------------------------
@pytest.mark.parametrize("money,expected", [
    ({"currency_code": "USD", "value": "25.00"}, (2500, "USD")),
    ({"currency_code": "USD", "value": "25"}, (2500, "USD")),
    ({"currency_code": "USD", "value": "0.10"}, (10, "USD")),
    ({"currency_code": "USD", "value": "0.29"}, (29, "USD")),      # 0.29*100 is 28.999... with floats
    ({"currency_code": "MXN", "value": "435.77"}, (43577, "MXN")),
    ({"currency_code": "JPY", "value": "1500"}, (1500, "JPY")),    # no minor units
    (None, (None, None)),
    ({}, (None, None)),
])
def test_money_to_minor(money, expected):
    assert money_to_minor(money) == expected


@pytest.mark.parametrize("money", [
    {"currency_code": "USD", "value": "10.005"},   # would require rounding
    {"currency_code": "JPY", "value": "10.5"},
    {"currency_code": "USD", "value": "abc"},
])
def test_money_rejects_lossy_or_invalid(money):
    with pytest.raises(ValueError):
        money_to_minor(money)


@pytest.mark.parametrize("cents,cur,text", [
    (2500, "USD", "25.00"), (10, "USD", "0.10"), (5, "USD", "0.05"), (43577, "MXN", "435.77"),
    (1500, "JPY", "1500"), (None, "USD", None), (2500, None, None),
])
def test_minor_to_str(cents, cur, text):
    assert minor_to_str(cents, cur) == text


@pytest.mark.parametrize("value", ["25.00", "0.05", "435.77", "1234567.89"])
def test_money_roundtrip_is_lossless(value):
    cents, cur = money_to_minor({"currency_code": "USD", "value": value})
    assert minor_to_str(cents, cur) == value


# ---- dates ----------------------------------------------------------------------------------------
def test_parse_utc_accepts_z_and_offset():
    assert parse_utc("2026-10-14T12:00:00.000Z") == datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    assert parse_utc("2026-10-14T06:00:00-06:00") == datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", [None, "", "not a date", "2026-10-14T12:00:00"])  # last one is naive
def test_parse_utc_rejects_missing_malformed_naive(bad):
    assert parse_utc(bad) is None


def test_remaining_exact_ten_days():
    r = remaining(NOW + timedelta(days=10), NOW)
    assert (r["remaining_days"], r["remaining_hours"], r["overdue"]) == (10, 240, False)
    assert r["remaining_text"] == "10d 0h"


def test_remaining_partial_day_floors():
    r = remaining(NOW + timedelta(days=2, hours=5, minutes=59), NOW)
    assert (r["remaining_days"], r["remaining_hours"]) == (2, 53)
    assert r["remaining_text"] == "2d 5h"


def test_remaining_overdue():
    r = remaining(NOW - timedelta(hours=30), NOW)
    assert r["overdue"] is True
    assert r["remaining_hours"] == -30 and r["remaining_days"] == -1
    assert r["remaining_text"] == "overdue by 1d 6h"


def test_remaining_deadline_rendered_in_mexico_city():
    # 15:00Z is 09:00 in Mexico City (UTC-6, no DST in October 2026)
    r = remaining(datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc), NOW)
    assert r["deadline_cdmx"].startswith("2026-10-14 09:00")
    assert MX_TZ.utcoffset(datetime(2026, 10, 14)) == timedelta(hours=-6)


def test_remaining_without_deadline():
    r = remaining(None, NOW)
    assert r["deadline_cdmx"] is None and r["remaining_hours"] is None and r["overdue"] is None
    assert r["remaining_text"] == "no deadline in response"


# ---- normalize ------------------------------------------------------------------------------------
def test_normalize_full_dispute():
    n = normalize(base_dispute(), NOW, CAPTURE)
    assert n["dispute_id"] == "PP-R-TEST-0001"
    assert n["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert n["status"] == "WAITING_FOR_SELLER_RESPONSE" and n["stage"] == "INQUIRY"
    assert (n["amount_cents"], n["currency"], n["amount_display"]) == (2500, "USD", "25.00")
    assert n["seller_transaction_ids"] == [CAPTURE] and n["capture_matches_expected"] is True
    assert n["remaining_days"] == 10 and n["remaining_hours"] == 240
    assert n["can_provide_evidence"] is True
    assert n["available_actions"] == ["provide-evidence", "send-message"]
    assert n["evidence_requested_from_seller"] == ["PROOF_OF_FULFILLMENT"]
    assert n["evidence_submitted_by_seller"] == []


def test_normalize_other_capture_is_flagged_not_assumed():
    d = base_dispute(disputed_transactions=[{"seller_transaction_id": "OTHERCAPTURE9"}])
    assert normalize(d, NOW, CAPTURE)["capture_matches_expected"] is False


def test_normalize_capture_unknown_gives_none():
    d = base_dispute(disputed_transactions=[{"buyer_transaction_id": "BUYER-TX-1"}])
    assert normalize(d, NOW, CAPTURE)["capture_matches_expected"] is None


def test_normalize_dispute_without_deadline():
    d = base_dispute()
    del d["seller_response_due_date"]
    n = normalize(d, NOW, CAPTURE)
    assert n["seller_response_due_date"] is None and n["deadline_parse_ok"] is None
    assert n["remaining_hours"] is None and n["remaining_text"] == "no deadline in response"


def test_normalize_malformed_deadline_is_reported():
    n = normalize(base_dispute(seller_response_due_date="soon"), NOW, CAPTURE)
    assert n["deadline_parse_ok"] is False and n["remaining_hours"] is None


def test_normalize_amount_in_other_currency():
    d = base_dispute(dispute_amount={"currency_code": "MXN", "value": "435.77"})
    n = normalize(d, NOW, CAPTURE)
    assert (n["amount_cents"], n["currency"]) == (43577, "MXN")


def test_normalize_no_provide_evidence_link_means_cannot_provide():
    d = base_dispute(links=[{"rel": "self", "href": "x", "method": "GET"},
                            {"rel": "escalate", "href": "y", "method": "POST"}])
    n = normalize(d, NOW, CAPTURE)
    assert n["can_provide_evidence"] is False and n["available_actions"] == ["escalate"]


def test_normalize_sparse_list_item_does_not_crash():
    n = normalize({"dispute_id": "PP-R-TEST-0002", "status": "OPEN"}, NOW, CAPTURE)
    assert n["dispute_id"] == "PP-R-TEST-0002" and n["amount_cents"] is None
    assert n["can_provide_evidence"] is False and n["capture_matches_expected"] is None
