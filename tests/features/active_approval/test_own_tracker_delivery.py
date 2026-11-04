"""SELF-TRACKER: PayPal registers our own tracking number as a MERCHANT tracker with status SHIPPED after a confirmed send
(sandbox take-N: GET trackers [] before, one SHIPPED entry after). A document that says delivered must not then read as
"Does not match" against that tracker on the package page, and the sidebar must not say "a difference is listed under Checks".
Only an own-sent number registered by our own confirmed send is excluded; foreign trackers and trackers present before the
send still count, and the pre-send checks are unchanged."""
import pytest
from _hosted import authenticated_client

from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.web.routes_disputes import get_client

from .test_guarantees import DID, decisions, ready
from .test_send_once_and_honest_status import OWN_NUMBER, PayPalAfterSend, _tracker
from .test_ux_rc41_approval_send import _raw_checks
from copilot.features.active_approval import service

DELIVERED = "DEMO DATA\nTracking number: DEMO-FDX-001\nCarrier: FedEx\nStatus: Delivered\nDelivery date: 2026-09-28\nPostal code: 06600"


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr("copilot.core.send_recovery.time.sleep", lambda seconds: None)


@pytest.fixture
def delivered(prepared):
    """The `prepared` package, but its document says delivered (the fixture's own says in transit)."""
    web, fake, db, package, doc = prepared
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE documents SET extracted_text=? WHERE id=?", (DELIVERED, doc))
    return prepared


def _state(checks, cid):
    return next(c.state for c in checks if c.id == cid)


def _approve(sender, db, package):
    review = service.read(sender, db, DID, package)
    service.approve(sender, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender.review = review
    return review


def test_the_fixture_states_a_delivered_document_with_no_tracker_before_the_send(delivered):
    _, fake, db, package, _ = delivered
    assert _state(_raw_checks(fake, db), "delivery_status") == "verified"


def test_own_tracker_after_a_confirmed_send_is_not_a_not_delivered_source(delivered):
    """The sandbox facts: document says delivered, the send is confirmed, our tracker appears as SHIPPED."""
    _, fake, db, package, _ = delivered
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)                 # trackers [] before, [OWN_NUMBER SHIPPED] after the POST
    assert send_package(sender, db, DID, package).sent
    assert sender.trackers and sender.trackers[0]["status"] == "SHIPPED"
    checks = _raw_checks(sender, db)
    assert _state(checks, "delivery_status") == "verified"
    assert _state(checks, "tracking_number") == "verified"          # the registered number still compares, it is only not a delivery report
    assert not [c.id for c in checks if c.state == "conflict"]


def test_the_package_page_after_the_send_has_no_bad_row_and_no_difference_line(delivered):
    web, fake, db, package, _ = delivered
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    web.app.dependency_overrides[get_client] = lambda: sender
    page = authenticated_client(web.app).get(f"/disputes/{DID}/packages/{package}")
    assert page.status_code == 200
    assert 'data-tag-state="bad"' not in page.text
    assert "a difference is listed under Checks" not in page.text
    assert 'id="check-delivery_status" data-check="delivery_status" data-check-state="verified"' in page.text


def test_the_case_overview_after_the_send_has_no_difference_lines(delivered):
    """The overview reads the same checks as the package page: no 'PayPal facts changed after you sent: 1 difference' (seen in the
    sandbox take after a clean send) and no 'Checked after sending: 1 difference'."""
    web, fake, db, package, _ = delivered
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    web.app.dependency_overrides[get_client] = lambda: sender
    overview = authenticated_client(web.app).get(f"/disputes/{DID}")
    assert overview.status_code == 200
    assert "PayPal facts changed after you sent" not in overview.text
    assert "Checked after sending" not in overview.text
    assert "No differences found in the fields checked just now." in overview.text


def test_control_a_foreign_shipped_tracker_after_the_send_still_conflicts(delivered):
    _, fake, db, package, _ = delivered
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    assert send_package(sender, db, DID, package).sent
    assert _state(_raw_checks(sender, db), "delivery_status") == "conflict"


def test_control_b_a_tracker_with_our_number_that_existed_before_the_send_still_conflicts(delivered):
    _, fake, db, package, _ = delivered
    fake.raw["evidences"] = [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}]
    review = service.read(fake, db, DID, package)

    class AlreadyThere(PayPalAfterSend):
        def call(self, method, path, op, **kwargs):
            result = super().call(method, path, op, **kwargs)
            if method == "POST":
                self.trackers = [_tracker(OWN_NUMBER, "SHIPPED")]      # the tracker that was already there changes: not our effect
            return result

    sender = AlreadyThere(fake, review)
    sender.trackers = [_tracker(OWN_NUMBER, "DELIVERED")]   # registered when the seller approves (a SHIPPED one would block approval)
    _approve(sender, db, package)
    assert _state(_raw_checks(sender, db), "delivery_status") == "verified"
    assert send_package(sender, db, DID, package).sent
    assert _state(_raw_checks(sender, db), "delivery_status") == "conflict"


def test_control_c_before_any_send_the_checks_are_unchanged(delivered):
    _, fake, db, package, _ = delivered
    review = ready(fake, db, package)
    sender = PayPalAfterSend(fake, review)
    assert _state(_raw_checks(sender, db), "delivery_status") == "verified"          # no tracker yet
    sender.trackers = [_tracker(OWN_NUMBER)]                # a SHIPPED tracker with the number we would send, before any send
    assert _state(_raw_checks(sender, db), "delivery_status") == "conflict"          # not loosened: no confirmed send exists
    assert sender.posts == 0
