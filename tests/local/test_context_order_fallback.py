"""v1.1.2 O: when PayPal's transaction report has no row for the sale yet, the amount, the payment date and the shipping address
come from the sale itself (capture, then order), with their own source. Read-only GETs; the report still wins when it has the sale."""
import pytest

from copilot.core.checklist import ExtractedDoc, evaluate, load_rules
from copilot.core.context import SRC_DISPUTE, SRC_ORDER, SRC_SEARCH, DisallowedCall, _guard, fetch_context

TXN, ORDER = "CAP123", "ORD456"
NORM = {"dispute_id": "PP-R-AAA-10000001", "seller_transaction_ids": [TXN], "create_time": "2026-11-10T02:00:00Z",
        "amount_cents": 79900, "currency": "MXN", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "status": "WAITING_FOR_SELLER_RESPONSE"}
CAPTURE = {"id": TXN, "status": "COMPLETED", "amount": {"currency_code": "MXN", "value": "799.00"},
           "create_time": "2026-11-10T01:41:44Z", "supplementary_data": {"related_ids": {"order_id": ORDER}}}
ORDER_BODY = {"id": ORDER, "purchase_units": [{"payments": {"captures": [{"id": TXN}]}, "shipping": {
    "name": {"full_name": "Ana Demo"},
    "address": {"address_line_1": "Demo address line", "admin_area_2": "Ciudad de Mexico", "admin_area_1": "CDMX",
                "postal_code": "06600", "country_code": "MX"}}}]}
REPORT_ROW = {"transaction_details": [{"transaction_info": {"transaction_id": TXN, "transaction_initiation_date": "2026-11-10T01:41:44+0000",
                                                             "transaction_amount": {"currency_code": "MXN", "value": "799.00"}},
                                        "shipping_info": {"name": "Ana Demo", "address": {"line1": "Demo address line", "city": "Ciudad de Mexico",
                                                                                          "state": "CDMX", "postal_code": "06600", "country_code": "MX"}}}]}


class Fake:
    def __init__(self, report=None, capture=CAPTURE, order=ORDER_BODY):
        self.report, self.capture, self.order, self.calls = report or {"transaction_details": []}, capture, order, []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path.split("?")[0], op))
        if path.startswith("/v1/reporting/transactions"):
            return self.report
        if path.startswith("/v2/payments/captures/"):
            return self.capture
        if path.startswith("/v2/checkout/orders/"):
            return self.order
        return {}


def test_an_empty_report_is_filled_from_the_capture_and_the_order_with_their_own_source():
    fake = Fake()
    ctx = fetch_context(fake, NORM)
    assert ctx.get("shipping_address")["postal_code"] == "06600" and ctx.source_of("shipping_address") == SRC_ORDER
    assert ctx.get("total_amount_cents") == 79900 and ctx.source_of("total_amount_cents") == SRC_ORDER
    assert ctx.get("payment_date") and ctx.source_of("payment_date") == SRC_ORDER and ctx.get("recipient_name") == "Ana Demo"
    assert not any(lim.message.startswith("PayPal returned no transaction details") for lim in ctx.limitations)
    assert all(m == "GET" for m, _p, _o in fake.calls)
    assert [op for _m, _p, op in fake.calls if op in ("context_capture", "context_order")] == ["context_capture", "context_order"]


def test_when_the_report_has_the_sale_the_order_is_never_read():
    fake = Fake(report=REPORT_ROW)
    ctx = fetch_context(fake, NORM)
    assert ctx.source_of("shipping_address") == SRC_SEARCH and ctx.source_of("total_amount_cents") == SRC_SEARCH
    assert not any(op in ("context_capture", "context_order") for _m, _p, op in fake.calls)


@pytest.mark.parametrize("capture, order", [({}, ORDER_BODY), ({**CAPTURE, "id": "OTHER"}, ORDER_BODY),
                                            ({**CAPTURE, "supplementary_data": {}}, ORDER_BODY), (CAPTURE, {})])
def test_a_capture_or_order_that_does_not_answer_changes_nothing_about_the_address(capture, order):
    ctx = fetch_context(Fake(capture=capture, order=order), NORM)
    assert ctx.get("shipping_address") is None
    assert any(lim.message.startswith("PayPal returned no transaction details") for lim in ctx.limitations)


def test_without_any_sale_amount_the_disputed_amount_stays_the_labelled_fallback():
    ctx = fetch_context(Fake(capture={}), NORM)
    assert ctx.get("total_amount_cents") == 79900 and ctx.source_of("total_amount_cents") == SRC_DISPUTE


def test_the_destination_check_can_stop_a_fresh_sale_on_the_order_address():
    ctx = fetch_context(Fake(), NORM)
    doc = ExtractedDoc("1", {"postal_code": {"value": "03100", "confidence": "read"}})
    checks = {c.id: c for c in evaluate(ctx, [doc], load_rules(country="MX"))}
    assert checks["destination"].state == "conflict"
    assert checks["destination"].seller_message.startswith("Your receipt says 03100. PayPal says 06600.")
    assert SRC_ORDER in checks["destination"].sources


def test_the_allowlist_takes_the_two_reads_and_nothing_else_new():
    _guard("GET", "/v2/payments/captures/CAP123")
    _guard("GET", "/v2/checkout/orders/ORD456")
    with pytest.raises(DisallowedCall):
        _guard("POST", "/v2/checkout/orders/ORD456")
    with pytest.raises(DisallowedCall):
        _guard("GET", "/v2/payments/refunds/X")


def test_a_failed_optional_read_leaves_one_log_line_without_the_message(caplog):
    class Refuses(Fake):
        def call(self, method, path, op, **kw):
            if path.startswith("/v2/payments/captures/"):
                raise RuntimeError("secret-looking body CAP123")
            return super().call(method, path, op, **kw)
    with caplog.at_level("WARNING", logger="copilot.context"):
        ctx = fetch_context(Refuses(), NORM)
    assert ctx.get("shipping_address") is None
    lines = [r.getMessage() for r in caplog.records if r.name == "copilot.context"]
    assert lines == ["optional read of the sale's capture failed: RuntimeError status=None"]
