"""Offline tests for tools/sandbox_orders.py body construction."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_orders as so  # noqa: E402


@pytest.mark.parametrize("cents,expected", [(2500, "25.00"), (80000, "800.00"), (4000, "40.00"), (5, "0.05")])
def test_amount_string(cents, expected):
    body = so.build_order_body(cents, "x")
    amount = body["purchase_units"][0]["amount"]
    assert amount == {"currency_code": "USD", "value": expected}
    assert isinstance(amount["value"], str)


def test_shipping_preference_and_structure():
    body = so.build_order_body(2500, "d1-main")
    ctx = body["payment_source"]["paypal"]["experience_context"]
    assert body["intent"] == "CAPTURE"
    # The shipping address is set DIRECTLY on the order, never left for the buyer to
    # pick (SET_PROVIDED_ADDRESS, not GET_FROM_FILE) - a judge's real dispute then has a real address for
    # copilot/core/context.py's lookups (Transaction Search/Trackers) to find.
    assert ctx["shipping_preference"] == "SET_PROVIDED_ADDRESS"
    assert ctx["user_action"] == "PAY_NOW"
    assert ctx["return_url"].startswith("https://example.com/")
    assert ctx["cancel_url"].startswith("https://example.com/")
    assert body["purchase_units"][0]["reference_id"] == "d1-main"


def test_dr1_order_brand_is_the_public_proofie_demo_name():
    body = so.build_order_body(2500, "DR1-DEMO")
    assert body["payment_source"]["paypal"]["experience_context"]["brand_name"] == "Proofie (DEMO)"


def test_shipping_address_is_included_on_the_order():
    body = so.build_order_body(2500, "d1-main")
    shipping = body["purchase_units"][0]["shipping"]
    assert shipping["name"]["full_name"] == so.SHIPPING_NAME
    assert shipping["address"] == so.SHIPPING_ADDRESS
    assert shipping["address"]["country_code"] == "MX"
    assert shipping["address"]["postal_code"]


def test_no_float_in_body():
    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        else:
            assert not isinstance(v, float)
    walk(so.build_order_body(80000, "d2"))
    json.dumps(so.build_order_body(4000, "d3"))


@pytest.mark.parametrize("bad", [25.0, 0, -5, True, "2500"])
def test_rejects_non_integer_or_nonpositive(bad):
    with pytest.raises(ValueError):
        so.cents_to_str(bad)


def test_approve_url_prefers_payer_action():
    order = {"links": [{"rel": "self", "href": "a"}, {"rel": "payer-action", "href": "https://x/approve"}]}
    assert so.approve_url(order) == "https://x/approve"


def test_buyer_click_lines_name_the_sale_and_the_single_path():
    lines = so.buyer_click_lines("45.00", "USD", "CAP123")
    text = "\n".join(lines)
    assert "45.00 USD" in text and "CAP123" in text
    assert "Resolution Center" in text and "Item not received" in text and "final confirmation" in text
    assert "password" not in text.lower()


def test_batch_creates_n_labelled_sales_and_refuses_bad_counts(monkeypatch, capsys):
    made = []
    monkeypatch.setattr(so, "create", lambda cents, label: made.append((cents, label)))
    so.batch(3, 1500, "pool")
    assert made == [(1500, "pool-1"), (1500, "pool-2"), (1500, "pool-3")]
    assert "3 sale(s) created" in capsys.readouterr().out
    for bad in (0, 11, True, "2"):
        with pytest.raises(ValueError):
            so.batch(bad, 1500, "pool")
    assert len(made) == 3


def test_main_accepts_batch_and_several_capture_ids_and_keeps_going_after_one_failure(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(so, "batch", lambda n, cents, label: seen.append(("batch", n, cents, label)))

    def fake_capture(order_id):
        seen.append(("capture", order_id))
        if order_id == "BAD":
            raise RuntimeError("not approved")

    monkeypatch.setattr(so, "capture", fake_capture)
    assert so.main(["x", "batch", "2", "1500", "pool"]) == 0
    assert so.main(["x", "capture", "A", "BAD", "C"]) == 1
    assert seen == [("batch", 2, 1500, "pool"), ("capture", "A"), ("capture", "BAD"), ("capture", "C")]
    assert "ERROR: BAD" in capsys.readouterr().out
    assert so.main(["x", "capture"]) == 2
