"""LINK-FIX H1: on a fresh hosted session the judge pool's re-armed cases read "Ready to answer".

The crawl found SAM and VWT-like cases listed "Used" because the re-arm note exception wanted our own evidence file to hold the
helper's 200 escalate POST, and a hosted session's evidence folder is ephemeral. PayPal's own read is the receipt now, for pool
ids only. Offline: a fake PayPal that answers GET only, a fresh (empty) evidence folder.

Expected, written before the first run (the first test fails on a14ee06f):
- a pool case whose only seller entry is the exact re-arm note, empty evidence folder: state ANSWER, /judge lists it ready, the
  pick redirects to the dispute page and that page answers 200;
- any other seller evidence next to the note, or a note with other words: USED, the pick answers 409;
- a dispute outside the pool with the same note and no receipt: still counts as evidence sent;
- the flag off: nothing is forgiven;
- nothing is ever written to PayPal.
"""
import pytest

from copilot.core import disputes
from copilot.features.judge_pool import service

from test_judge_pool import (BUYER, POOL3, REAL, SELLER, case, env, fake, norm_of, poof, web)  # noqa: F401

NOTE_ENTRY = {"source": "SUBMITTED_BY_SELLER", "evidence_type": "OTHER", "notes": disputes.REARM_NOTE, "date": "2026-11-01T22:59:53Z"}
ARMED = ("provide_evidence", "accept_claim")


def armed(did, *evidences):
    return case(did, "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK", ARMED, evidences=list(evidences))


def test_a_pool_case_with_only_the_rearm_note_is_ready_without_a_local_receipt(env):
    assert not list(env.glob("evidence/*"))                                  # a fresh folder: no receipt file at all
    assert service.case_state(norm_of(armed(SELLER, NOTE_ENTRY))) == service.ANSWER


def test_the_judge_list_offers_it_and_the_pick_works(env, monkeypatch):
    from copilot.features.judge_pool import routes
    paypal = fake()
    paypal.cases[SELLER] = armed(SELLER, NOTE_ENTRY)
    client = web(paypal, monkeypatch)
    listing = client.get("/judge")
    assert listing.status_code == 200
    assert f'href="/judge/cases/{SELLER}"' in listing.text and f"{SELLER} - Used" not in listing.text
    assert "Ready to answer" in listing.text
    pick = client.get(f"/judge/cases/{SELLER}", follow_redirects=False)
    assert pick.status_code in (302, 303) and pick.headers["location"] == f"/disputes/{SELLER}"
    assert client.get(pick.headers["location"]).status_code == 200
    assert paypal.posts() == [] and routes.COOKIE


@pytest.mark.parametrize("entries", [
    [NOTE_ENTRY, poof()],
    [{**NOTE_ENTRY, "evidence_type": "PROOF_OF_FULFILLMENT"}],
    [{**NOTE_ENTRY, "notes": "DEMO DATA - sandbox re-arm by escalate; no buyer action. And more."}],
    [{**NOTE_ENTRY, "notes": "something else"}],
])
def test_any_other_seller_evidence_marks_the_case_used_and_the_pick_is_refused(env, monkeypatch, entries):
    raw = armed(SELLER, *entries)
    assert service.case_state(norm_of(raw)) == service.USED
    paypal = fake()
    paypal.cases[SELLER] = raw
    client = web(paypal, monkeypatch)
    assert f"{SELLER} - Used" in client.get("/judge").text
    assert client.get(f"/judge/cases/{SELLER}", follow_redirects=False).status_code == 409


def test_a_second_note_entry_is_forgiven_too_but_a_real_send_beside_it_is_not(env):
    assert disputes.normalize(armed(SELLER, NOTE_ENTRY, NOTE_ENTRY), __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))["evidence_submitted_by_seller"] == []
    mixed = norm_of(armed(SELLER, NOTE_ENTRY, poof()))
    assert mixed["evidence_submitted_by_seller"] == ["PROOF_OF_FULFILLMENT"]


def test_a_dispute_outside_the_pool_is_never_forgiven_without_a_receipt(env):
    outside = "PP-R-ZZZ-10009999"
    assert outside not in POOL3
    assert norm_of(armed(outside, NOTE_ENTRY))["evidence_submitted_by_seller"] == ["OTHER"]


def test_the_record_case_is_not_forgiven_either(env):
    record = "PP-R-HJE-10194967"
    assert norm_of(armed(record, NOTE_ENTRY))["evidence_submitted_by_seller"] == ["OTHER"]


def test_with_the_flag_off_nothing_is_forgiven(env, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_REARM_NOTE_IGNORED", "false")
    assert norm_of(armed(SELLER, NOTE_ENTRY))["evidence_submitted_by_seller"] == ["OTHER"]
    assert service.case_state(norm_of(armed(SELLER, NOTE_ENTRY))) == service.USED


def test_outside_the_hosted_demo_the_receipt_is_still_required(env, monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    assert norm_of(armed(SELLER, NOTE_ENTRY))["evidence_submitted_by_seller"] == ["OTHER"]
