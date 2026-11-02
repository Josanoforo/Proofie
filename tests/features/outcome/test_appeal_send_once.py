"""The appeal goes through the send-once reservation (copilot/core/write_guard.py): one POST per approved request.
Fixtures only: nothing here reaches PayPal."""
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from copilot.core import write_guard
from copilot.features.outcome import appeal
from outcome_fakes import DISPUTE_ID, FakePayPal, dispute_raw, paypal_error

CLAIMS = [{"text": "Shipped on 2026-09-30 with FedEx"}, {"text": "Delivered 2026-10-02"}]
BODY = appeal.appeal_body(CLAIMS, "DEMO-FDX-000001", "FedEx")
ATTACH = [{"filename": "estafeta_demo_letter.pdf", "content_type": "application/pdf", "data": b"%PDF-1.4 demo"}]
SHA = appeal.approval_sha256(DISPUTE_ID, BODY, appeal.attachment_sha256s(ATTACH))
BODY_SHA = appeal.request_sha256(BODY)
WITH_LINK = dict(status="RESOLVED", rels=("appeal",), outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})
pytestmark = pytest.mark.usefixtures("legacy_approval_scenario")


def _fake(**kw):
    return FakePayPal(dispute_raw(**WITH_LINK), **kw)


def _go(fake):
    return appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, SHA)


def test_a_double_submit_sends_exactly_one_post():
    first, second = _fake(), _fake()
    r1, r2 = _go(first), _go(second)
    assert len(first.posts()) == 1 and r1.posted
    assert second.posts() == [] and r2.status == "already_attempted" and not r2.posted
    assert [c[0] for c in second.calls] == ["GET"]                     # only the fresh link read
    assert "already sent" in r2.reason
    assert write_guard.lookup(DISPUTE_ID, "appeal", BODY_SHA)["state"] == write_guard.SENT


def test_two_submits_at_the_same_moment_send_exactly_one_post():
    entered, release = threading.Event(), threading.Event()
    first, second = _fake(), _fake()
    original = first.call

    def held(method, path, op, **kw):
        if method == "POST":
            entered.set()
            assert release.wait(5)
        return original(method, path, op, **kw)
    first.call = held
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(_go, first)
        assert entered.wait(5)
        try:
            r2 = _go(second)                                           # the reload / second click while the POST is in flight
        finally:
            release.set()
        r1 = running.result(timeout=5)
    assert r1.posted and r2.status == "already_attempted" and second.posts() == []
    assert "being sent right now" in r2.reason
    assert len(first.posts()) == 1


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("timed out"), paypal_error(500), paypal_error(429)])
def test_an_uncertain_post_is_never_repeated_the_seller_reads_paypal_instead(error):
    first = _fake(post_raises=error)
    r1 = _go(first)
    assert r1.posted and r1.status in ("sent_unconfirmed", "failed") and len(first.posts()) == 1
    assert write_guard.lookup(DISPUTE_ID, "appeal", BODY_SHA)["state"] == write_guard.UNKNOWN
    second = _fake()
    r2 = _go(second)
    assert r2.status == "already_attempted" and second.posts() == []
    assert "do not send it again" in r2.reason
    assert [c[0] for c in second.calls] == ["GET"]                     # reading PayPal is all a retry does


def test_a_definite_paypal_refusal_frees_the_key_for_a_new_approval():
    refused = _fake(post_raises=paypal_error(422))
    assert _go(refused).status == "failed" and len(refused.posts()) == 1
    assert write_guard.lookup(DISPUTE_ID, "appeal", BODY_SHA)["state"] == write_guard.REFUSED
    again = _fake()
    assert _go(again).posted and len(again.posts()) == 1


def test_a_different_body_is_a_different_key():
    other_body = appeal.appeal_body(CLAIMS + [{"text": "One more fact"}], "DEMO-FDX-000001", "FedEx")
    other_sha = appeal.approval_sha256(DISPUTE_ID, other_body, appeal.attachment_sha256s(ATTACH))
    _go(_fake())
    other = _fake()
    assert appeal.register_appeal(other, DISPUTE_ID, other_body, ATTACH, other_sha).posted and len(other.posts()) == 1


def test_without_the_appeal_link_nothing_is_sent_and_nothing_is_reserved():
    fake = FakePayPal(dispute_raw(status="RESOLVED", rels=("send_message",)))
    r = _go(fake)
    assert r.status == "rejected_link" and fake.posts() == []
    assert write_guard.lookup(DISPUTE_ID, "appeal", BODY_SHA) is None


@pytest.mark.parametrize("wrong", ["", "0" * 64, SHA.upper()])
def test_a_sha256_mismatch_is_refused_before_any_call_or_reservation(wrong):
    fake = _fake()
    with pytest.raises(appeal.ApprovalMismatch):
        appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, wrong)
    assert fake.calls == [] and write_guard.lookup(DISPUTE_ID, "appeal", BODY_SHA) is None
