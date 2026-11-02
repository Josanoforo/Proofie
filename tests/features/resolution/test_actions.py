"""Request bodies, the buyer message and the hash canon."""
import hashlib
import json

import pytest

from copilot.core.send import sha256_json
from copilot.features.resolution import actions

NORM = {"create_time": "2026-10-01T15:00:00Z", "amount_cents": 4000, "currency": "USD"}
TXN = {"payment_date": "2026-09-28T10:00:00Z", "total_amount_cents": 4000, "total_currency": "USD",
       "shipping_address": {"line1": "Domicilio Demo 123", "city": "Ciudad de Mexico", "state": "CDMX",
                            "postal_code": "06600", "country_code": "MX"}}
TRACK = {"tracking_number": "DEMO-FDX-000001", "carrier": "FEDEX", "tracking_status": "SHIPPED"}


def test_offer_body_equals_the_sandbox_body_byte_for_byte_for_1000_cents_usd():
    body = actions.offer_body(1000, "USD", "Partial refund while we check the shipment")
    expected = {"offer_type": "REFUND", "offer_amount": {"currency_code": "USD", "value": "10.00"},
                "note": "Partial refund while we check the shipment"}
    assert body == expected
    assert json.dumps(body) == json.dumps(expected)
    assert list(body) == ["offer_type", "offer_amount", "note"]


@pytest.mark.parametrize("cents", [0, -5, 4001])
def test_offer_amount_must_be_positive_and_not_above_the_dispute_amount(cents):
    with pytest.raises(ValueError):
        actions.offer_body(cents, "USD", "n", max_cents=4000)


@pytest.mark.parametrize("cents", [10.0, "1000", True, None])
def test_offer_amount_must_be_an_integer(cents):
    with pytest.raises(ValueError):
        actions.offer_body(cents, "USD", "n")


def test_the_dispute_amount_itself_is_allowed():
    assert actions.offer_body(4000, "USD", "n", max_cents=4000)["offer_amount"]["value"] == "40.00"


@pytest.mark.parametrize("note", ["", "  ", "x" * 2001])
def test_bad_notes_are_refused(note):
    with pytest.raises(ValueError):
        actions.offer_body(1000, "USD", note)


def test_message_body_shape_and_refusals():
    assert actions.message_body("  Hello  ") == {"message": "Hello"}
    for bad in ("", " ", "x" * 2001):
        with pytest.raises(ValueError):
            actions.message_body(bad)


def test_cents_to_str_is_integer_arithmetic():
    assert [actions.cents_to_str(c) for c in (5, 100, 1000, 123456)] == ["0.05", "1.00", "10.00", "1234.56"]


def test_message_text_has_the_literals_of_the_fixture():
    text = actions.message_text(NORM, TXN, TRACK)
    for literal in ("28 September 2026", "40.00 USD", "Domicilio Demo 123", "06600", "DEMO-FDX-000001", "with FedEx"):
        assert literal in text
    assert "SHIPPED" not in text and "tracking page" not in text and "2026-09-28" not in text
    assert text.endswith("Tracking number: DEMO-FDX-000001 with FedEx. If it has not arrived, reply here and I will look into it right away.")
    assert "{" not in text and "None" not in text and "<" not in text


def test_message_text_uses_the_delivery_estimate_when_a_tracker_has_one():
    text = actions.message_text(NORM, TXN, {**TRACK, "delivery_estimate": "2026-10-10"})
    assert "has not arrived by 2026-10-10, reply here" in text


def test_message_text_without_transaction_or_tracker_leaves_unknowns_out_not_placeholders():
    text = actions.message_text(NORM, {}, None)
    assert text == ("Hello, this is about your order of 1 October 2026 for 40.00 USD. If it has not arrived, "
                    "reply here and I will look into it right away.")


def test_sha256_equals_the_send_canon_written_out_independently():
    body = actions.offer_body(1000, "USD", "note")
    canon = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert actions.request_sha256(body) == hashlib.sha256(canon).hexdigest() == sha256_json(body)
    reordered = {"note": "note", "offer_amount": {"value": "10.00", "currency_code": "USD"}, "offer_type": "REFUND"}
    assert actions.request_sha256(reordered) == actions.request_sha256(body)
