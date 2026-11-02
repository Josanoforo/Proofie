"""prepare_* and register on the fake PayPal: the hash, the fresh-link rule, one POST, polls, one GET, 422 not retried."""
import json
from datetime import datetime, timedelta

import httpx
import pytest

from copilot import config
from copilot.db import repo
from copilot.features.resolution import actions as act
from copilot.features.resolution import service, store
from resolution_fakes import BASE, DISPUTE_ID, FakePayPal, paypal_error



def _offer(fake, cents=1000, note="Partial refund while we check the shipment"):
    return service.prepare_offer(fake, DISPUTE_ID, cents, note)


@pytest.mark.parametrize("record", [None, "confirmed_offer", "pending_offer", "unconfirmed_offer",
                                  "failed_offer", "confirmed_message", "other_dispute_offer"])
def test_waiting_buyer_text_requires_confirmed_local_offer_for_same_dispute(record):
    fake = FakePayPal(phase="after_message", status="WAITING_FOR_BUYER_RESPONSE")
    if record is not None:
        did = "PP-R-TST-0000002" if record == "other_dispute_offer" else DISPUTE_ID
        action = "message" if record == "confirmed_message" else "offer"
        status = {"pending_offer": "sent_pending_settle", "unconfirmed_offer": "sent_unconfirmed",
                  "failed_offer": "failed"}.get(record, "sent_confirmed")
        store.save_action(did, action, {}, "a" * 64, "a" * 64, {}, status == "sent_confirmed", status)
    page = service.page_state(fake, DISPUTE_ID)
    expected = ("Offer confirmed by reading the dispute back: waiting for the buyer's reply" if record == "confirmed_offer" else
                "Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from this app, and PayPal no longer offers the option to make one.")
    assert page.next_step_text == expected
    assert page.actions.offer is False
    assert fake.posts() == []


def test_prepare_offer_gives_the_body_its_hash_and_the_economics_line():
    p = _offer(FakePayPal())
    assert p.allowed and p.reason == "" and p.action == "offer"
    assert p.body == {"offer_type": "REFUND", "offer_amount": {"currency_code": "USD", "value": "10.00"},
                      "note": "Partial refund while we check the shipment"}
    assert p.sha256 == act.request_sha256(p.body)
    assert p.next_step_text == "Proofie has seen PayPal remove the refund option after a seller message. If you want to offer a refund, do it first."
    assert p.economics.concede_cents == 4000 and p.economics.fight_cents == (1500, 5500)   # INQUIRY: no fee on concede
    assert "eligibility code 01" in p.economics.text


def test_prepare_offer_at_claim_stage_adds_the_fee_to_conceding(monkeypatch):
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    p = service.prepare_offer(FakePayPal(stage="CHARGEBACK"), DISPUTE_ID, 1000, "n")
    assert p.economics.concede_cents == 5500 and "account cliff feature off" in p.economics.text


@pytest.mark.parametrize("cents", [0, 4001])
def test_prepare_offer_refuses_a_bad_amount_without_a_body(cents):
    p = _offer(FakePayPal(), cents)
    assert not p.allowed and p.body is None and p.sha256 is None and p.reason


def test_prepare_offer_when_the_link_is_gone_says_why():
    p = _offer(FakePayPal(phase="after_offer", status="WAITING_FOR_BUYER_RESPONSE"))
    assert not p.allowed and p.body is None and p.reason == "Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from this app, and PayPal no longer offers the option to make one."


def test_prepare_message_uses_the_template_with_paypals_literals():
    p = service.prepare_message(FakePayPal(), DISPUTE_ID)
    text = p.body["message"]
    assert p.allowed and p.sha256 == act.request_sha256(p.body)
    for literal in ("28 September 2026", "40.00 USD", "DEMO-FDX-000001", "FedEx", "Domicilio Demo 123"):
        assert literal in text


def test_prepare_with_a_failing_read_is_not_allowed_with_a_limitation():
    p = service.prepare_offer(FakePayPal(get_raises=paypal_error(500, None)), DISPUTE_ID, 1000, "n")
    assert not p.allowed and p.limitations


# ---- register ----------------------------------------------------------------------------------------------------
def test_wrong_hash_is_rejected_with_no_network_at_all():
    fake = FakePayPal()
    p = _offer(fake)
    before = len(fake.calls)
    out = service.register(fake, DISPUTE_ID, "offer", p.body, "0" * 64)
    assert out.status == "rejected_hash" and len(fake.calls) == before and not fake.posts()
    assert store.list_actions(DISPUTE_ID) == []


def test_link_gone_since_the_preview_is_rejected_with_no_post():
    fake = FakePayPal()
    p = _offer(fake)
    fake.phase = "after_offer"; fake.status = "WAITING_FOR_BUYER_RESPONSE"          # PayPal moved on after the preview
    out = service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    assert out.status == "rejected_link" and not fake.posts() and out.reason == "Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from this app, and PayPal no longer offers the option to make one."
    assert store.list_actions(DISPUTE_ID) == []


def test_a_settling_dispute_rejects_any_write():
    fake = FakePayPal()
    p = service.prepare_message(fake, DISPUTE_ID)
    fake.pending, fake.left = "after_message", 5
    out = service.register(fake, DISPUTE_ID, "message", p.body, p.sha256)
    assert out.status == "rejected_link" and "refresh in a minute" in out.reason and not fake.posts()


def test_a_tampered_body_with_its_own_hash_is_rejected_by_shape():
    fake = FakePayPal()
    too_much = act.offer_body(5000, "USD", "n")                                    # more than the 40.00 dispute amount
    out = service.register(fake, DISPUTE_ID, "offer", too_much, act.request_sha256(too_much))
    assert out.status == "rejected_body" and not fake.posts()
    odd = {"offer_type": "REFUND", "offer_amount": {"currency_code": "USD", "value": "10.00"}, "note": "n",
           "extra": 1}
    out = service.register(fake, DISPUTE_ID, "offer", odd, act.request_sha256(odd))
    assert out.status == "rejected_body" and not fake.posts()


def test_register_returns_at_once_with_one_post_and_no_poll_inside_the_request():
    fake = FakePayPal(settling_reads=2)
    p = _offer(fake)
    n_before = len(fake.calls)
    out = service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    assert out.status == "sent_pending_settle" and out.reason == "Sent to PayPal; waiting for PayPal to settle this dispute"
    assert [c[2] for c in fake.calls[n_before:]] == ["resolution_read", "resolution_offer"]      # no poll, no read-back
    assert fake.posts() == [("POST", f"{BASE}/make-offer", "resolution_offer")]
    rows = store.list_actions(DISPUTE_ID)
    assert len(rows) == 1 and rows[0]["status"] == "sent_pending_settle" and rows[0]["settled"] == 0
    assert rows[0]["request_sha256"] == p.sha256 == rows[0]["approval_sha256"]
    audit = [a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "resolution_offer"]
    assert len(audit) == 1 and audit[0]["detail"]["status"] == "sent_pending_settle"


def test_a_settled_fake_moves_the_pending_row_to_confirmed_with_the_readback_on_the_next_render():
    fake = FakePayPal(settling_reads=0)
    p = _offer(fake)
    service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    n_before = len(fake.calls)
    assert service.settle_pending(fake, DISPUTE_ID) == 1
    assert [c[2] for c in fake.calls[n_before:]] == ["resolution_poll", "resolution_readback"]   # one GET, then the read-back
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_confirmed" and row["settled"] == 1
    kept = json.loads(row["readback_json"])
    assert kept.pop("checked_at")                                  # when PayPal was read
    assert kept == {"stage": "INQUIRY", "status": "WAITING_FOR_BUYER_RESPONSE",
                    "links": ["provide-evidence", "send-message"], "outcome": None}
    assert service.settle_pending(fake, DISPUTE_ID) == 0                                         # nothing pending any more


def test_inside_the_window_the_row_stays_pending_one_get_per_call():
    fake = FakePayPal(settling_reads=99)
    p = _offer(fake)
    service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    n_before = len(fake.calls)
    for _ in range(3):
        assert service.settle_pending(fake, DISPUTE_ID) == 0
    assert [c[2] for c in fake.calls[n_before:]] == ["resolution_poll"] * 3
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_pending_settle"


def test_after_80_seconds_a_still_pending_row_is_marked_unconfirmed_with_the_limitation():
    fake = FakePayPal(settling_reads=99)
    p = _offer(fake)
    service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    created = datetime.strptime(store.list_actions(DISPUTE_ID)[0]["created_at"], "%Y-%m-%dT%H:%M:%S%z")
    assert service.settle_pending(fake, DISPUTE_ID, now=created + timedelta(seconds=79)) == 0
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_pending_settle"
    assert service.settle_pending(fake, DISPUTE_ID, now=created + timedelta(seconds=80)) == 1
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "sent_unconfirmed" and row["settled"] == 0
    assert row["limitation_text"] == "PayPal is still updating this dispute after 80 seconds; refresh later"
    assert service.SETTLE_WINDOW_SECONDS == 80 and service.REFRESH_SECONDS == 10


def test_a_failing_read_leaves_the_row_pending_until_it_times_out():
    fake = FakePayPal(settling_reads=0)
    p = _offer(fake)
    service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    fake.get_raises = paypal_error(500, None)
    assert service.settle_pending(fake, DISPUTE_ID) == 0
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_pending_settle"
    created = datetime.strptime(store.list_actions(DISPUTE_ID)[0]["created_at"], "%Y-%m-%dT%H:%M:%S%z")
    assert service.settle_pending(fake, DISPUTE_ID, now=created + timedelta(seconds=81)) == 1
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_unconfirmed"


def test_a_settled_message_is_confirmed_by_a_successful_read():
    fake = FakePayPal(settling_reads=0)
    m = service.prepare_message(fake, DISPUTE_ID)
    service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert service.settle_pending(fake, DISPUTE_ID) == 1
    assert store.list_actions(DISPUTE_ID)[0]["status"] == "sent_confirmed"


def test_page_state_knows_when_something_is_pending():
    fake = FakePayPal(settling_reads=99)
    assert service.page_state(fake, DISPUTE_ID).pending is False
    p = _offer(fake)
    service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    assert service.page_state(fake, DISPUTE_ID).pending is True


def test_a_422_in_the_settling_window_is_stored_as_refused_and_not_retried():
    fake = FakePayPal(post_raises=paypal_error(422))
    p = service.prepare_message(fake, DISPUTE_ID)
    out = service.register(fake, DISPUTE_ID, "message", p.body, p.sha256)
    assert out.status == "refused_422" and out.http_status == 422
    assert out.reason == "PayPal was still updating this dispute; wait a minute and try again"
    assert len(fake.posts()) == 1                                                 # never retried by code
    assert [c[2] for c in fake.calls].count("resolution_poll") == 0
    row = store.list_actions(DISPUTE_ID)[0]
    assert row["status"] == "refused_422" and row["settled"] == 0 and "dbg-1" not in row["limitation_text"]


def test_other_paypal_refusals_are_failed_and_a_timeout_is_unconfirmed():
    fake = FakePayPal(post_raises=paypal_error(400, "INVALID_REQUEST"))
    p = _offer(fake)
    assert service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256).status == "failed"
    fake = FakePayPal(post_raises=httpx.ReadTimeout("slow"))
    p = _offer(fake)
    assert service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256).status == "sent_unconfirmed"
    assert len(fake.posts()) == 1


def test_a_failing_read_before_the_post_sends_nothing():
    fake = FakePayPal()
    p = _offer(fake)
    fake.get_raises = paypal_error(500, None)
    out = service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    assert out.status == "read_failed" and not fake.posts() and out.limitations


def test_a_message_first_removes_the_offer_from_the_next_preview():
    fake = FakePayPal(settling_reads=0)
    m = service.prepare_message(fake, DISPUTE_ID)
    out = service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert out.status == "sent_pending_settle"
    again = _offer(fake)
    assert not again.allowed and again.body is None
    assert service.page_state(fake, DISPUTE_ID).actions.offer is False


def test_unknown_action_is_a_programming_error():
    with pytest.raises(ValueError):
        service.register(FakePayPal(), DISPUTE_ID, "escalate", {}, "x")


def test_nothing_in_the_feature_ever_posts_outside_the_two_write_paths():
    fake = FakePayPal(settling_reads=0)
    p = _offer(fake)
    service.register(fake, DISPUTE_ID, "offer", p.body, p.sha256)
    service.settle_pending(fake, DISPUTE_ID)
    m = service.prepare_message(fake, DISPUTE_ID)
    service.register(fake, DISPUTE_ID, "message", m.body, m.sha256)
    assert [c[1] for c in fake.posts()] == [f"{BASE}/make-offer", f"{BASE}/send-message"]
    assert json.dumps(fake.calls).count("escalate") == 0
