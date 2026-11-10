"""v1.1.2: PayPal's transaction report lags a new sale. When the report gains the sale's row between the approval and the
read right after the send, the same facts arrive from another source. That is not a change by the seller and not a change
of the facts: no note, the approval of what was sent stands. Fake transport only."""
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service, store

from .test_guarantees import DID, decisions, ready
from .test_send_once_and_honest_status import PayPalAfterSend, _audit, _no_waiting  # noqa: F401

TXN, ORDER = "DEMOTXN0000000001", "DEMOORDER00000001"
CAPTURE = {"id": TXN, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "25.00"},
           "create_time": "2026-09-20T01:41:44Z", "supplementary_data": {"related_ids": {"order_id": ORDER}}}
ORDER_BODY = {"id": ORDER, "purchase_units": [{"payments": {"captures": [{"id": TXN}]}, "shipping": {
    "name": {"full_name": "Ana Demo"},
    "address": {"address_line_1": "Demo address line", "admin_area_2": "Ciudad de Mexico", "admin_area_1": "CDMX",
                "postal_code": "06600", "country_code": "MX"}}}]}
ROW = {"transaction_details": [{"transaction_info": {"transaction_id": TXN, "transaction_initiation_date": "2026-09-20T01:41:44+0000",
                                                      "transaction_amount": {"currency_code": "USD", "value": "25.00"}},
                                "shipping_info": {"name": "Ana Demo", "address": {"line1": "Demo address line", "city": "Ciudad de Mexico",
                                                                                  "state": "CDMX", "postal_code": "06600",
                                                                                  "country_code": "MX"}}}]}


class SaleFromTheOrder:
    """The approval-time client: the report has no row, the capture and the order answer."""
    def __init__(self, fake):
        self.fake, self.raw = fake, fake.raw

    def call(self, method, path, op, **kwargs):
        if path.startswith("/v2/payments/captures/"):
            return CAPTURE
        if path.startswith("/v2/checkout/orders/"):
            return ORDER_BODY
        return self.fake.call(method, path, op, **kwargs)


class ReportCatchesUp(PayPalAfterSend):
    """Before the POST as above; after it the report holds the sale's row (same amount, date and address)."""
    def call(self, method, path, op, **kwargs):
        if path.startswith("/v2/payments/captures/"):
            return CAPTURE
        if path.startswith("/v2/checkout/orders/"):
            return ORDER_BODY
        if method == "GET" and path.startswith("/v1/reporting/transactions") and self.posts:
            return ROW
        return super().call(method, path, op, **kwargs)


def _approved(prepared):
    _, fake, db, package, _ = prepared
    client = SaleFromTheOrder(fake)
    review = ready(client, db, package)
    approval_id = service.approve(client, db, DID, package, review.review_sha256, decisions(review), "", "")
    return fake, db, package, review, approval_id


def test_the_report_gaining_the_sale_after_the_send_is_not_a_change(prepared):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = ReportCatchesUp(fake, review)
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1 and result.sent
    assert getattr(result, "note", None) is None
    assert repo.get_latest_valid_approval(db, package)["id"] == approval_id
    assert not _audit(db, "active_send_sources_changed") and not _audit(db, "invalidate_active_approval")
    assert store.reservation_state(db, approval_id) == "finished"


def test_a_later_read_with_the_report_row_does_not_clear_the_approval_of_what_was_sent(prepared):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = ReportCatchesUp(fake, review)
    assert send_package(sender, db, DID, package).sent
    after = service.read(sender, db, DID, package)
    assert after.approval and after.approval["id"] == approval_id and not after.cleared
