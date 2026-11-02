"""missing_then_and_now on the m1 fixture (synthetic) and a throwaway database."""
import copy
import sys

from copilot import config
from copilot.db import repo
from copilot.features.fulfilment import store as fulfil_store
from copilot.features.fulfilment import tracker_request as tr
from copilot.features.outcome import gaps
from copilot.features.resolution import store as res_store
from copilot.fixtures.m1.loader import fake_client, load_case

CASE = load_case("c01_complete")
NORM = CASE["dispute_norm"]
DISPUTE = NORM["dispute_id"]
CAPTURE = NORM["seller_transaction_ids"][0]


def _run(flags=None, norm=NORM, client=None):
    path = config.db_path()
    if flags is not None:
        repo.insert_package(path, DISPUTE, source="human_edit", validation_ok=True, review_flags=flags)
    return gaps.missing_then_and_now(client or fake_client(copy.deepcopy(CASE)), path, DISPUTE, norm)


def test_missing_now_comes_from_the_checklist_over_the_fake_context():
    g = _run()
    by_id = {m["id"]: m for m in g.missing_now}
    assert "destination" in by_id and by_id["destination"]["state"] == "missing"
    assert "case_request:PROOF_OF_FULFILLMENT" in by_id
    assert "tracking_number" not in by_id                    # PayPal's tracker verifies it
    assert all(m["state"] in ("missing", "conflict") for m in g.missing_now)


def test_addable_items_carry_the_checklists_own_text_and_the_page_that_adds_them():
    g = _run()
    texts = {a["text"]: a["href"] for a in g.addable}
    assert any(t.startswith("Delivery place") or "delivery place" in t.lower() for t in texts)
    assert texts["A tracker registered before the dispute"] == f"/fulfilment/{CAPTURE}"
    assert texts["An offer or a message to the buyer"] == f"/disputes/{DISPUTE}/resolution"
    assert {a["href"] for a in g.addable} == {f"/disputes/{DISPUTE}", f"/fulfilment/{CAPTURE}", f"/disputes/{DISPUTE}/resolution"}


def test_a_tracker_and_a_resolution_action_already_recorded_are_not_suggested_again():
    body = tr.build_tracker_body(CAPTURE, "DEMO-FDX-000001", "FEDEX", None)
    sha = tr.request_sha256(body)
    fulfil_store.save_file(CAPTURE, body, sha, sha, None, {"checks": []}, "registered")
    res_store.save_action(DISPUTE, "offer", {"offer_type": "REFUND"}, "c" * 64, "c" * 64, None, True, "sent_confirmed")
    texts = [a["text"] for a in _run().addable]
    assert "A tracker registered before the dispute" not in texts
    assert "An offer or a message to the buyer" not in texts


def test_flags_without_check_ids_say_not_recorded_at_send_time():
    g = _run(flags=["Manual package: nothing has been entered yet."])
    assert (g.at_send_recorded, g.at_send_note, g.missing_at_send) == (False, "not recorded at send time", ())
    assert g.missing_now                                       # the current evaluation is still shown


def test_flags_that_carry_check_ids_are_listed_as_missing_at_send():
    g = _run(flags=["check:destination:missing", "check:carrier:conflict", "an unrelated notice"])
    assert g.at_send_recorded and g.at_send_note == ""
    assert g.missing_at_send == ({"id": "destination", "state": "missing"}, {"id": "carrier", "state": "conflict"})


def test_no_package_at_all_is_said_plainly():
    g = _run()
    assert g.at_send_recorded is False and g.at_send_note == "No package was created for this dispute."


def test_an_unreadable_dispute_gives_a_limitation_and_no_guess():
    g = gaps.missing_then_and_now(fake_client(copy.deepcopy(CASE)), config.db_path(), DISPUTE, None)
    assert g.missing_now == () and g.addable == () and len(g.limitations) == 1


def test_absent_feature_packages_just_drop_their_suggestions(monkeypatch):
    monkeypatch.setitem(sys.modules, "copilot.features.fulfilment.lookup", None)
    monkeypatch.setitem(sys.modules, "copilot.features.resolution.store", None)
    texts = [a["text"] for a in _run().addable]
    assert texts and "A tracker registered before the dispute" not in texts
    assert "An offer or a message to the buyer" not in texts


def test_a_denied_search_is_a_limitation_from_the_context():
    case = copy.deepcopy(CASE)
    case["paypal"]["transactions"] = {"__error": 403}
    g = _run(client=fake_client(case))
    assert any(lim.source == "paypal_transaction_search" for lim in g.limitations)
