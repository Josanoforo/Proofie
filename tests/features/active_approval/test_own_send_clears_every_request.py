"""PayPal answers a provide-evidence by closing the whole set of seller requests, not only the type that was sent
(sandbox take-T2, copilot_web.jsonl rows 216/224: requested PROOF_OF_FULFILLMENT + PROOF_OF_REFUND + OTHER before the send,
none after, one SUBMITTED_BY_SELLER entry, status UNDER_REVIEW). That is the send's own effect: no note, the approval stands.
Anything else that differs is still reported. Fake transport only."""
import copy

import pytest

from copilot.core.send_recovery import RECEIVED_MESSAGE
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service, store

from .test_guarantees import DID, SendFake, decisions
from .test_send_once_and_honest_status import OWN_NUMBER, PayPalAfterSend, _audit, _no_waiting  # noqa: F401

REQUESTED = ("PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND", "OTHER")


def _requests(types=REQUESTED):
    return [{"evidence_type": t, "source": "REQUESTED_FROM_SELLER", "date": "2026-11-03T02:05:23.880Z"} for t in types]


class ClosesRequests(PayPalAfterSend):
    """The sandbox shape: three requests before the send; after it only what the seller submitted. `keep` lists request
    types PayPal leaves open (empty in the sandbox facts)."""

    def __init__(self, fake, review, keep=(), **kwargs):
        super().__init__(fake, review, **kwargs)
        self.body["evidences"] = _requests()
        self.keep = keep

    def call(self, method, path, op, **kwargs):
        result = super().call(method, path, op, **kwargs)
        if method == "POST":
            # lands=False: PayPal closed the requests but our submission is not among the entries
            self.body["evidences"] = [e for e in self.body["evidences"] if e["source"] == "SUBMITTED_BY_SELLER"]
            self.body["evidences"] += _requests(self.keep)
        return result


def _approved_with_three_requests(prepared):
    _, fake, db, package, _ = prepared
    fake.raw["evidences"] = _requests()
    review = service.read(fake, db, DID, package)
    approval_id = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    return fake, db, package, review, approval_id


def test_a_clean_send_that_closes_every_request_has_no_note_and_keeps_the_approval(prepared):
    fake, db, package, review, approval_id = _approved_with_three_requests(prepared)
    sender = ClosesRequests(fake, review)
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1
    assert result.http_status == 200 and result.title == RECEIVED_MESSAGE and result.sent
    assert getattr(result, "note", None) is None
    assert repo.get_latest_valid_approval(db, package)["id"] == approval_id
    assert not _audit(db, "active_send_sources_changed") and not _audit(db, "invalidate_active_approval")
    assert store.reservation_state(db, approval_id) == "finished"


def test_a_later_read_after_that_send_does_not_clear_the_approval(prepared):
    fake, db, package, review, approval_id = _approved_with_three_requests(prepared)
    sender = ClosesRequests(fake, review)
    assert send_package(sender, db, DID, package).sent
    after = service.read(sender, db, DID, package)
    assert after.approval and after.approval["id"] == approval_id and not after.cleared
    assert not _audit(db, "invalidate_active_approval")


@pytest.mark.parametrize("keep", [("PROOF_OF_REFUND",), ("OTHER",)], ids=["refund-left-open", "other-left-open"])
def test_a_request_that_stays_open_next_to_the_closed_ones_is_still_a_change(prepared, keep):
    fake, db, package, review, _ = _approved_with_three_requests(prepared)
    sender = ClosesRequests(fake, review, keep=keep)
    result = send_package(sender, db, DID, package)
    assert result.sent and result.note and result.note.startswith("Changed after sending:")
    assert repo.get_latest_valid_approval(db, package) is None


def test_a_foreign_tracker_next_to_the_closed_requests_is_still_a_change(prepared):
    fake, db, package, review, _ = _approved_with_three_requests(prepared)
    sender = ClosesRequests(fake, review, trackers_after=(OWN_NUMBER, "SOMEONE-ELSE-77"))
    result = send_package(sender, db, DID, package)
    assert result.sent and result.note and "shipment checks or PayPal transaction facts" in result.note
    assert repo.get_latest_valid_approval(db, package) is None


def test_requests_gone_without_our_submission_landing_is_still_reported(prepared):
    fake, db, package, review, _ = _approved_with_three_requests(prepared)
    sender = ClosesRequests(fake, review, lands=False)
    result = send_package(sender, db, DID, package)
    assert not result.sent and result.http_status == 202
    assert result.title == "Sent, review required" and "source changed while sending" in result.message
    assert repo.get_latest_valid_approval(db, package) is None
