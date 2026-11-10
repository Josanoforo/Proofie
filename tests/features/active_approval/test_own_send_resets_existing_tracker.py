"""v1.1.2, sandbox fact read from take-902 (copilot_web.jsonl rows 14 and 261, dispute PP-R-WPV-10197701): the tracker of the
number we send already existed and read DELIVERED; at the second of our POST PayPal set it back to SHIPPED. That reset is
the send's own effect: no note, the approval of what was sent stands, and the pages keep reading the status PayPal showed
when she approved. Any other change of an existing tracker is still a change. Fake transport only."""
from copilot.core import own_tracker
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service, store

from .test_guarantees import DID, decisions
from .test_own_tracker_delivery import delivered  # noqa: F401 - the package whose document says delivered
from .test_send_once_and_honest_status import OWN_NUMBER, PayPalAfterSend, _audit, _no_waiting, _tracker  # noqa: F401


class ResetsTheExistingTracker(PayPalAfterSend):
    def __init__(self, *a, after="SHIPPED", **k):
        super().__init__(*a, **k)
        self.trackers, self.after = [_tracker(OWN_NUMBER, "DELIVERED")], after

    def call(self, method, path, op, **kwargs):
        result = super().call(method, path, op, **kwargs)
        if method == "POST":
            self.trackers = [_tracker(OWN_NUMBER, self.after)]
        return result


def _approved_with_a_delivered_tracker(prepared, **kwargs):
    _, fake, db, package, _ = prepared
    fake.raw["evidences"] = [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}]
    sender = ResetsTheExistingTracker(fake, None, **kwargs)
    review = service.read(sender, db, DID, package)
    approval_id = service.approve(sender, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender.review = review
    return sender, db, package, approval_id


def test_paypal_setting_the_existing_tracker_back_to_shipped_on_our_send_is_not_a_change(delivered):  # noqa: F811
    sender, db, package, approval_id = _approved_with_a_delivered_tracker(delivered)
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1 and result.sent and getattr(result, "note", None) is None
    assert repo.get_latest_valid_approval(db, package)["id"] == approval_id
    assert not _audit(db, "active_send_sources_changed") and not _audit(db, "invalidate_active_approval")
    assert store.reservation_state(db, approval_id) == "finished"


def test_a_later_read_after_that_send_keeps_the_approval_and_the_pages_read_the_approved_status(delivered):  # noqa: F811
    sender, db, package, approval_id = _approved_with_a_delivered_tracker(delivered)
    assert send_package(sender, db, DID, package).sent
    after = service.read(sender, db, DID, package)
    assert after.approval and after.approval["id"] == approval_id and not after.cleared
    answer = own_tracker.own_tracker_client(sender, db, DID).call("GET", "/v1/shipping/trackers?transaction_id=x", "context_tracking")
    assert [t["status"] for t in answer["trackers"]] == ["DELIVERED"]


def test_another_status_on_the_existing_tracker_after_the_send_is_still_a_change(delivered):  # noqa: F811
    sender, db, package, _ = _approved_with_a_delivered_tracker(delivered, after="RETURNED")
    result = send_package(sender, db, DID, package)
    assert result.sent and result.note and "Changed after sending" in result.note
    assert repo.get_latest_valid_approval(db, package) is None
