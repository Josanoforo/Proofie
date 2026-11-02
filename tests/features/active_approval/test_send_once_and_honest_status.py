"""A second click after a confirmed evidence send is blocked and
says so; PayPal's own effect of our send (it registers the tracking we sent as a tracker of the transaction and moves
the dispute to UNDER_REVIEW) is not reported as a foreign change; a real concurrent change is a secondary note, never
the headline. Fake transport only; the shapes follow the recorded evidence rows (copilot_web.jsonl rows 90, 94)."""
import copy
import re

import pytest
from _hosted import authenticated_client, set_test_csrf

from copilot.core.send_service import send_package
from copilot.core.send_recovery import RECEIVED_MESSAGE
from copilot.db import repo
from copilot.features.active_approval import service, store
from copilot.web.routes_disputes import get_client

from .test_guarantees import DID, SendFake, decisions, ready

OWN_NUMBER = "DEMO-FDX-001"          # the tracking number of the synthetic document in the `prepared` fixture
TXN = "DEMOTXN0000000001"


def _tracker(number, status="SHIPPED"):
    return {"transaction_id": TXN, "tracking_number": number, "tracking_number_type": "CARRIER_PROVIDED",
            "status": status, "carrier": "FEDEX", "shipment_uploader": "MERCHANT", "links": []}


class PayPalAfterSend(SendFake):
    """As PayPal behaved in the sandbox rehearsal: after provide-evidence the dispute is UNDER_REVIEW with only `self`, and the tracking
    in the evidence is registered as a tracker of the transaction (the tracker list was empty before the send)."""

    def __init__(self, fake, review, trackers_after=(OWN_NUMBER,), lands=True):
        super().__init__(fake, review)
        self.trackers_after, self.trackers, self.lands = trackers_after, [], lands

    def call(self, method, path, op, **kwargs):
        if method == "GET" and path.startswith("/v1/shipping/trackers"):
            return {"trackers": copy.deepcopy(self.trackers), "links": []}
        if method == "POST":
            self.posts += 1
            if self.lands:
                self.body["evidences"] = [{**e, "source": "SUBMITTED_BY_SELLER"}
                                          for e in self.review.request_input["evidences"]]
            self.body["status"], self.body["dispute_state"] = "UNDER_REVIEW", "UNDER_PAYPAL_REVIEW"
            self.body["links"] = [link for link in self.body.get("links", []) if link["method"] == "GET"]
            self.trackers = [_tracker(n) for n in self.trackers_after]
            return {}
        return super().call(method, path, op, **kwargs)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """The post-send reconciliation sleeps 5, 15, 30 s between GETs; the fakes answer at once."""
    monkeypatch.setattr("copilot.core.send_recovery.time.sleep", lambda seconds: None)


def _approved(prepared):
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    approval_id = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    return fake, db, package, review, approval_id


def _audit(db, action):
    return [row for row in repo.list_audit(db, DID) if row["action"] == action]


# ---- the send's own effect is not a foreign change -----------------------------------------------------------
def test_r2_paypals_own_tracker_registration_does_not_invalidate_the_approval(prepared):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1
    assert result.http_status == 200 and result.title == RECEIVED_MESSAGE and result.sent
    assert getattr(result, "note", None) is None
    assert repo.get_latest_valid_approval(db, package)["id"] == approval_id
    assert not _audit(db, "active_send_sources_changed") and not _audit(db, "invalidate_active_approval")
    assert store.reservation_state(db, approval_id) == "finished"


def test_r2_a_later_review_read_does_not_clear_the_approval_either(prepared):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    after = service.read(sender, db, DID, package)
    assert after.approval and after.approval["id"] == approval_id and not after.cleared
    assert not _audit(db, "invalidate_active_approval")


def test_r2_a_real_concurrent_tracking_change_is_a_secondary_note_and_invalidates_without_retry(prepared):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1, "no automatic retry"
    assert result.http_status == 200 and result.title == RECEIVED_MESSAGE and result.sent
    assert result.note.startswith("Changed after sending:") and result.note.endswith("nothing was re-sent.")
    assert "shipment checks or PayPal transaction facts" in result.note
    assert repo.get_latest_valid_approval(db, package) is None
    assert _audit(db, "active_send_sources_changed")[0]["detail"]["automatic_retry"] is False


def test_r2_our_own_tracker_next_to_a_foreign_one_still_counts_as_a_change(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=(OWN_NUMBER, "SOMEONE-ELSE-77"))
    result = send_package(sender, db, DID, package)
    assert result.sent and result.note and "Changed after sending" in result.note
    assert repo.get_latest_valid_approval(db, package) is None and sender.posts == 1


def test_r2_a_tracker_that_already_existed_before_the_send_is_compared_normally(prepared):
    fake, db, package, review, _ = _approved(prepared)

    class AlreadyThere(PayPalAfterSend):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.trackers = [_tracker(OWN_NUMBER, "SHIPPED")]

        def call(self, method, path, op, **kwargs):
            result = super().call(method, path, op, **kwargs)
            if method == "POST":
                self.trackers = [_tracker(OWN_NUMBER, "DELIVERED")]       # the existing tracker changed: not our effect
            return result

    # the approval was made while no tracker existed, so re-approve with the tracker present first
    sender = AlreadyThere(fake, review)
    review2 = service.read(sender, db, DID, package)
    service.approve(sender, db, DID, package, review2.review_sha256, decisions(review2), "", "")
    sender.review = review2
    result = send_package(sender, db, DID, package)
    assert result.sent and result.note and "Changed after sending" in result.note


def test_r2_an_unconfirmed_send_with_a_real_change_keeps_the_honest_review_required_result(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",), lands=False)
    result = send_package(sender, db, DID, package)
    assert result.http_status == 202 and not result.sent
    assert result.title == "Sent, review required" and "source changed while sending" in result.message
    assert sender.posts == 1 and repo.get_latest_valid_approval(db, package) is None


def test_r2_result_page_has_the_sent_headline_and_the_change_as_a_separate_secondary_note():
    from copilot.web.routes_send import _page
    html = _page(RECEIVED_MESSAGE, "PayPal now shows this evidence as received.", DID, 1, 200,
                 note="Changed after sending: shipment checks or PayPal transaction facts; nothing was re-sent."
                 ).body.decode()
    assert "<h1>Evidence sent to PayPal</h1>" in html
    assert 'data-state="changed-after-sending"' in html
    assert "Changed after sending: shipment checks or PayPal transaction facts; nothing was re-sent." in html
    assert "Sent, review required" not in html
    assert html.index("<h1>") < html.index("changed-after-sending")


def test_r2_route_headline_and_readback_time_after_a_confirmed_send_with_the_trackers_effect(prepared):
    web, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    web.app.dependency_overrides[get_client] = lambda: sender
    web = authenticated_client(web.app)
    set_test_csrf(web, "R2-CSRF")
    r = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": "R2-CSRF"})
    assert r.status_code == 200 and "<h1>Evidence sent to PayPal</h1>" in r.text
    assert "Confirmed by reading the dispute back from PayPal at" in r.text
    assert "review required" not in r.text.lower() and "changed after sending" not in r.text.lower()


def test_r2_dispute_page_after_a_confirmed_send_agrees_with_paypal(prepared):
    web, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    web.app.dependency_overrides[get_client] = lambda: sender
    web = authenticated_client(web.app)
    page = web.get(f"/disputes/{DID}").text
    row = re.search(r'<td data-label="Status">(.*?)</td>', web.get(f"/disputes/{DID}/workspace/prepare").text, re.S).group(1)
    assert "Sent to PayPal" in row and "Needs attention" not in row
    from _dom import parse
    dom = parse(page)
    approve, send = dom.find("tr", data_task="approve").text(), dom.find("tr", data_task="send").text()
    assert "Done: approval used for the send" in approve and "Not started" not in approve
    assert "Done: package v1 confirmed in PayPal's record" in send and "tag--ok" in page


def test_r2_dispute_page_keeps_sent_even_when_a_real_change_invalidated_the_approval(prepared):
    web, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    assert send_package(sender, db, DID, package).sent
    assert repo.get_latest_valid_approval(db, package) is None
    web.app.dependency_overrides[get_client] = lambda: sender
    web = authenticated_client(web.app)
    page = web.get(f"/disputes/{DID}").text
    assert "Sent to PayPal" in re.search(r'<td data-label="Status">(.*?)</td>', web.get(f"/disputes/{DID}/workspace/prepare").text, re.S).group(1)
    from _dom import parse
    assert "Not started" not in parse(page).find("tr", data_task="approve").text()


# ---- a second click after a confirmed send is blocked and says so ----------------------------
def test_r1_second_send_click_after_confirmed_makes_no_post_and_says_package_already_received(prepared):
    web, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")

    sender = PayPalAfterSend(fake, review)
    web.app.dependency_overrides[get_client] = lambda: sender
    web = authenticated_client(web.app)
    set_test_csrf(web, "R1-CSRF")
    first = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": "R1-CSRF"})
    assert first.status_code == 200 and sender.posts == 1
    sender.body["evidences"] = []                      # a later GET that no longer lists the evidence
    set_test_csrf(web, "R1-CSRF")
    second = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": "R1-CSRF"})
    assert sender.posts == 1, "the second click must never POST"
    assert second.status_code in (200, 409)
    assert "already" in second.text.lower() and "nothing new was sent" in second.text.lower()


def test_r1_second_send_click_while_evidence_is_still_listed_also_makes_no_post(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    repeat = send_package(sender, db, DID, package)
    assert sender.posts == 1 and "nothing new was sent" in repeat.message.lower()
