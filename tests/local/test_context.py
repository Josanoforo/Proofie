"""Offline tests for copilot/core/context.py with a fake client (no network)."""
import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot.core.context import (DisallowedCall, _guard, fetch_context, SRC_DISPUTE, SRC_SEARCH, SRC_SELLER,
                                  SRC_TRACKING)

_spec = importlib.util.spec_from_file_location("m1_loader", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)


def ctx_of(name):
    case = m1.load_case(name)
    return case, m1.fake_client(case), m1.build_context(case)


def test_every_field_carries_its_source():
    _, _, ctx = ctx_of("c01_complete")
    assert ctx.source_of("seller_transaction_id") == SRC_DISPUTE
    assert ctx.source_of("shipping_address") == SRC_SEARCH
    assert ctx.source_of("payment_date") == SRC_SEARCH
    assert ctx.source_of("tracking_number") == SRC_TRACKING
    assert ctx.get("shipping_address")["postal_code"] == "06600"
    assert ctx.get("total_amount_cents") == 450000 and ctx.get("total_currency") == "MXN"
    assert ctx.limitations == ()


def test_only_get_calls_on_allowed_paths_with_window_up_to_31_days():
    case = m1.load_case("c01_complete")
    client = m1.fake_client(case)
    fetch_context(client, case["dispute_norm"])
    assert {m for m, _ in client.calls} == {"GET"}
    search = next(p for _, p in client.calls if p.startswith("/v1/reporting/transactions"))
    q = parse_qs(urlparse(search).query)
    assert q["transaction_id"] == ["DEMOTXN0000000001"] and q["fields"] == ["all"]
    from datetime import datetime
    s, e = (datetime.strptime(q[k][0], "%Y-%m-%dT%H:%M:%SZ") for k in ("start_date", "end_date"))
    assert 0 < (e - s).days <= 31
    assert any(p.startswith("/v1/shipping/trackers?transaction_id=DEMOTXN0000000001") for _, p in client.calls)


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/reporting/transactions"),
    ("GET", "/v1/customer/disputes/PP-1"),
    ("GET", "https://api-m.paypal.com/v1/reporting/transactions"),          # live host
    ("GET", "https://evil.example/v1/shipping/trackers"),
])
def test_allowlist_rejects_other_methods_hosts_and_paths(method, path):
    with pytest.raises(DisallowedCall):
        _guard(method, path)


def test_sandbox_host_with_allowed_prefix_passes():
    _guard("GET", "https://api-m.sandbox.paypal.com/v1/shipping/trackers?transaction_id=X")


@pytest.mark.parametrize("status", [401, 403, 404, 400, 500])
def test_http_errors_become_limitations_never_exceptions(status):
    case = m1.load_case("c01_complete")
    case["paypal"] = {"transactions": {"__error": status}, "trackers": {"__error": status}}
    ctx = fetch_context(m1.fake_client(case), case["dispute_norm"])
    assert ctx.limitation_for(SRC_SEARCH) and ctx.limitation_for(SRC_TRACKING)
    assert ctx.get("shipping_address") is None and ctx.get("tracking_number") is None
    assert ctx.get("seller_transaction_id")          # dispute facts still there


def test_missing_scope_message_is_plain_english():
    case = m1.load_case("c01_complete")
    case["paypal"]["transactions"] = {"__error": 403}
    ctx = fetch_context(m1.fake_client(case), case["dispute_norm"])
    msg = ctx.limitation_for(SRC_SEARCH)
    assert "permission" in msg and "403" not in msg


def test_timeout_becomes_limitation():
    case = m1.load_case("c01_complete")
    case["paypal"] = {"transactions": {"__timeout": True}, "trackers": {"__timeout": True}}
    ctx = fetch_context(m1.fake_client(case), case["dispute_norm"])
    assert "in time" in ctx.limitation_for(SRC_SEARCH) and "in time" in ctx.limitation_for(SRC_TRACKING)


def test_empty_results_become_limitations():
    case = m1.load_case("c01_complete")
    case["paypal"] = {"transactions": {"transaction_details": []}, "trackers": {"trackers": []}}
    ctx = fetch_context(m1.fake_client(case), case["dispute_norm"])
    assert "no transaction details" in ctx.limitation_for(SRC_SEARCH)
    assert "No tracking number" in ctx.limitation_for(SRC_TRACKING)


def test_no_shipping_address_message():
    _, _, ctx = ctx_of("v_seller_declared_address")
    assert ctx.limitation_for(SRC_SEARCH) == "PayPal returned no shipping address for this transaction."
    assert ctx.get("shipping_address") is None


def test_seller_statement_is_labelled_and_never_replaces_paypal_address():
    _, _, ctx = ctx_of("c01_complete")
    ctx2 = ctx.with_seller_statement("shipping_address", {"postal_code": "99999"})
    assert ctx2.source_of("seller_declared_shipping_address") == SRC_SELLER
    assert ctx2.get("shipping_address")["postal_code"] == "06600"
    assert ctx2.source_of("shipping_address") == SRC_SEARCH


def test_programming_errors_still_raise():
    class Broken:
        def call(self, *a, **k):
            raise KeyError("bug")
    with pytest.raises(KeyError):
        fetch_context(Broken(), m1.load_case("c01_complete")["dispute_norm"])


def test_dispute_without_transaction_id_degrades_gracefully():
    norm = dict(m1.load_case("c01_complete")["dispute_norm"], seller_transaction_ids=[])
    client = m1.FakeClient()
    ctx = fetch_context(client, norm)
    assert client.calls == [] and ctx.limitation_for(SRC_SEARCH) and ctx.limitation_for(SRC_TRACKING)


def test_total_falls_back_to_dispute_amount_with_its_true_source():
    case = m1.load_case("c01_complete")
    case["paypal"]["transactions"] = {"__error": 403}
    ctx = fetch_context(m1.fake_client(case), case["dispute_norm"])
    assert ctx.get("total_amount_cents") == 450000 and ctx.source_of("total_amount_cents") == SRC_DISPUTE
