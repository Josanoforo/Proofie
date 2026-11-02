"""COPILOT_FEATURE_REARM_NOTE_IGNORED: the escalate note that tools/sandbox_dispute_state.py --rearm leaves on a dispute is not
"evidence already sent", but ANY other seller evidence still closes the send. Offline: raw dispute dicts, a temp evidence folder."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import test_routes_send as send_tests
from copilot.core import disputes, send_service
from copilot.web import inbox_view

ROOT = Path(__file__).resolve().parents[2]
DID = "PP-R-DEMO-0000001"
NOTE = disputes.REARM_NOTE
NOW = datetime.now(timezone.utc)


def seller_entry(evidence_type="OTHER", notes=NOTE, source="SUBMITTED_BY_SELLER"):
    return {"source": source, "evidence_type": evidence_type, "notes": notes, "date": "2026-11-01T22:59:53.786Z"}


def raw_with(*entries, dispute_id=DID):
    base = [e for e in send_tests.WAITING.get("evidences", []) if e.get("source") != "SUBMITTED_BY_SELLER"]
    return {**send_tests.WAITING, "dispute_id": dispute_id, "evidences": base + list(entries)}


def write_log(folder, *rows):
    (Path(folder) / disputes.REARM_LOG).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def escalate_line(dispute_id=DID, status=200, op=disputes.REARM_OP, method="POST"):
    return {"op": op, "method": method, "path": f"/v1/customer/disputes/{dispute_id}/escalate", "status": status}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setenv("COPILOT_FEATURE_REARM_NOTE_IGNORED", "true")
    return tmp_path


def listed(raw):
    return disputes.normalize(raw, NOW)["evidence_submitted_by_seller"]


def test_with_the_flag_off_nothing_changes(evidence, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_REARM_NOTE_IGNORED", "false")
    write_log(evidence, escalate_line())
    assert listed(raw_with(seller_entry())) == ["OTHER"]


def test_our_own_note_with_our_own_escalate_line_is_not_evidence_already_sent(evidence):
    write_log(evidence, escalate_line())
    norm = disputes.normalize(raw_with(seller_entry()), NOW)
    assert norm["evidence_submitted_by_seller"] == []
    assert send_service.no_way_out(bool(norm["evidence_submitted_by_seller"]), send_service.provide_evidence_offered(norm)) is None


@pytest.mark.parametrize("rows,why", [
    ([], "the evidence file has no lines"),
    ([escalate_line("PP-R-DEMO-9999999")], "the escalate line is for another dispute"),
    ([escalate_line(status=400)], "PayPal refused that escalate"),
    ([escalate_line(op="p2_escalate")], "the line is not the helper's"),
    ([escalate_line(method="GET")], "the line is a read"),
])
def test_without_our_own_escalate_line_for_this_dispute_the_note_still_closes_the_send(evidence, rows, why):
    write_log(evidence, *rows)
    norm = disputes.normalize(raw_with(seller_entry()), NOW)
    assert norm["evidence_submitted_by_seller"] == ["OTHER"], why
    assert send_service.no_way_out(True, send_service.provide_evidence_offered(norm)) == send_service.ONCE_PER_DISPUTE


def test_a_missing_evidence_file_is_not_an_error(evidence):
    assert listed(raw_with(seller_entry())) == ["OTHER"]


@pytest.mark.parametrize("other_notes", ["a different note", NOTE + " extra", NOTE.lower(), "", None])
def test_another_note_text_still_closes_the_send(evidence, other_notes):
    write_log(evidence, escalate_line())
    assert listed(raw_with(seller_entry(notes=other_notes))) == ["OTHER"]


def test_a_second_other_entry_with_another_text_still_closes_the_send_and_only_ours_is_dropped(evidence):
    write_log(evidence, escalate_line())
    assert listed(raw_with(seller_entry(), seller_entry(notes="something the seller typed"))) == ["OTHER"]


def test_real_evidence_still_closes_the_send_even_with_our_note(evidence):
    write_log(evidence, escalate_line())
    proof = seller_entry("PROOF_OF_FULFILLMENT", notes="DEMO DATA shipped with a carrier")
    assert listed(raw_with(proof, seller_entry())) == ["PROOF_OF_FULFILLMENT"]
    norm = disputes.normalize(raw_with(proof, seller_entry()), NOW)
    assert send_service.no_way_out(True, send_service.provide_evidence_offered(norm)) == send_service.ONCE_PER_DISPUTE


def test_only_as_many_notes_are_ignored_as_there_are_escalate_lines(evidence):
    write_log(evidence, escalate_line())
    assert listed(raw_with(seller_entry(), seller_entry())) == ["OTHER"]
    write_log(evidence, escalate_line(), escalate_line())
    assert listed(raw_with(seller_entry(), seller_entry())) == []


def test_the_buyer_and_requested_entries_are_not_touched(evidence):
    write_log(evidence, escalate_line())
    buyer = seller_entry(source="SUBMITTED_BY_BUYER")
    requested = seller_entry(source="REQUESTED_FROM_SELLER", notes=None)
    norm = disputes.normalize(raw_with(buyer, requested, seller_entry()), NOW)
    assert norm["evidence_submitted_by_seller"] == []
    assert norm["evidence_requested_from_seller"].count("OTHER") >= 1


def test_the_helper_and_the_app_agree_on_the_note_and_the_evidence_line():
    spec = importlib.util.spec_from_file_location("sandbox_dispute_state_for_test", ROOT / "tools" / "sandbox_dispute_state.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.REARM_NOTE == disputes.REARM_NOTE
    assert "dispute_state_" + tool.ACTIONS["rearm"]["op"] == disputes.REARM_OP
    assert tool.LOG_NAME + ".jsonl" == disputes.REARM_LOG


def test_the_inbox_reads_cases_through_the_same_normalize():
    assert inbox_view.normalize is disputes.normalize


def ipr_after_its_send(rels):
    """The recorded state of IPR after its send (walk/data/evidence/copilot_web.jsonl:149, 2026-11-01 18:06): UNDER_REVIEW,
    CHARGEBACK, seller entries OTHER (our re-arm note) and PROOF_OF_FULFILLMENT. Only the fields the app reads."""
    proof = seller_entry("PROOF_OF_FULFILLMENT", notes="DEMO DATA (sandbox test with synthetic documents). I shipped this parcel with Estafeta.")
    raw = raw_with(seller_entry(), proof, dispute_id="PP-R-IPR-10191157")
    raw.update(status="UNDER_REVIEW", dispute_life_cycle_stage="CHARGEBACK",
               links=[{"rel": rel, "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-IPR-10191157/{rel}", "method": "POST"}
                      for rel in rels])
    return raw


def test_after_its_send_a_second_send_on_ipr_is_not_offered_and_the_guard_refuses_it(evidence):
    from copilot.core.send_service import ONCE_PER_DISPUTE, check_preconditions, no_way_out, provide_evidence_offered
    req_input = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT"}]}
    write_log(evidence, escalate_line("PP-R-IPR-10191157"))
    for rels in (["provide_supporting_info", "self"], ["provide_evidence", "self"]):       # as recorded, and if PayPal asked again
        norm = disputes.normalize(ipr_after_its_send(rels), NOW)
        assert norm["evidence_submitted_by_seller"] == ["PROOF_OF_FULFILLMENT"]           # our note is ignored, the real send is not
        assert no_way_out(True, provide_evidence_offered(norm)) == ONCE_PER_DISPUTE
        problems = check_preconditions(norm, req_input, "PP-R-IPR-10191157", None)
        assert any("evidence already submitted by the seller" in p for p in problems), problems
