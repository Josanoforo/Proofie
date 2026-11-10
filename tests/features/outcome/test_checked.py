"""The ten-check card: the rows from stored facts (pure), and the page on the fake PayPal with a temp database."""
import hashlib
from types import SimpleNamespace

from _hosted import authenticated_client

from copilot import config
from copilot.app import create_app
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.features.outcome import checked
from copilot.web.routes_disputes import get_client
from outcome_fakes import DISPUTE_ID, FakePayPal, dispute_raw

FLAG = "COPILOT_FEATURE_OUTCOME"


def check(check_id, state, message="msg", label=None):
    return SimpleNamespace(id=check_id, state=state, seller_message=message, label_for_seller=label or check_id)


CLEAN = [check("tracking_number", "verified", "Matches the tracker in PayPal."), check("destination", "verified", "Matches."),
         check("delivery_status", "verified"), check("delivery_date", "verified"), check("signature", "not_applicable")]
BACKED = [{"source_ids": ["doc_3"]}, {"source_ids": ["paypal_dispute"]}]
DUE = {"text": "Respond by 11 Nov 2026, 11:59 pm (Mexico City time)", "remaining": "7 days left", "passed": False}


def card(**kw):
    base = dict(checks=CLEAN, claims=BACKED, package={"source": "human_edit", "validation_ok": False}, approval=None,
                record=SimpleNamespace(state="none", confirmed_at=None), due=DUE)
    return checked.rows(**{**base, **kw})


def states(rows):
    return [r.state for r in rows]


def test_ten_rows_in_the_cards_order_with_the_cards_words():
    rows = card()
    assert [r.number for r in rows] == list(range(1, 11)) and tuple(r.name for r in rows) == checked.NAMES
    assert rows[0].name == "The tracking number is this order's" and rows[9].name == "No deadline passes unseen"
    assert rows[3].name == "Every sentence shows where it comes from" and rows[5].name == "No proof of delivery? Proofie asks you"


def test_a_clean_package_before_approval():
    rows = card()
    assert states(rows) == ["ok", "ok", "ok", "ok", "na", "na", "todo", "todo", "todo", "ok"]
    assert rows[0].line == "Matches the tracker in PayPal." and rows[2].line == "All there."
    assert rows[3].line == "2 of 2 statements name their source." and "7 days left" in rows[9].line
    assert checked.summary(rows) == "5 of 8 checked"


def test_the_wrong_receipt_stops_row_2_with_the_checklists_own_words():
    wrong = [check("tracking_number", "verified"),
             check("destination", "conflict", "The document says postal code 03100; PayPal's record says 06600."),
             check("delivery_status", "verified"), check("delivery_date", "missing", label="Delivery date"),
             check("signature", "not_applicable")]
    rows = card(checks=wrong)
    assert rows[1].state == "stop" and "03100" in rows[1].line and rows[1].word == "Needs you"
    assert rows[2].state == "stop" and rows[2].line == "1 of 3 not there yet: Delivery date."
    assert "2 need you" in checked.summary(rows)


def test_no_checklist_result_is_not_yet_never_checked():
    rows = card(checks=[])
    assert states(rows)[:3] == ["todo", "todo", "todo"]


def test_a_statement_she_only_typed_has_no_paper_behind_it():
    rows = card(claims=[{"source_ids": ["doc_3"]}, {"source_ids": ["doc_manual"]}, {"source_ids": []}])
    assert rows[3].state == "own" and rows[3].word == "Your own words"
    assert rows[3].line == "2 of 3 statements have no document behind them; they go to PayPal as your own words."
    one = card(claims=[{"source_ids": []}])
    assert one[3].line == "1 of 1 statement has no document behind it; it goes to PayPal as your own words."
    assert "need" not in checked.summary(one)                       # her own words are not a stop
    assert card(claims=[])[3].state == "todo"


def test_ai_rows_only_for_an_ai_draft():
    passed = card(package={"source": "ai", "validation_ok": True, "effective_next_step": "submit_evidence_for_human_approval"})
    assert states(passed)[4:6] == ["ok", "ok"]
    held = card(package={"source": "ai", "validation_ok": True, "effective_next_step": "need_more_information"})
    assert states(held)[4:6] == ["ok", "stop"] and "held for you" in held[5].line
    failed = card(package={"source": "ai", "validation_ok": False, "effective_next_step": None})
    assert states(failed)[4:6] == ["stop", "na"]
    assert states(card())[4:6] == ["na", "na"]


def test_approval_send_and_read_back_rows():
    approved = card(approval={"approved_at": "x"}, approved_on="4 Nov 2026, 2:06 pm (Mexico City time)")
    assert approved[6].state == "ok" and "exactly the text and files you approved" in approved[6].line
    stale = card(approval={"approved_at": "x"}, approval_stale=True)
    assert stale[6].state == "stop" and "approval was cleared" in stale[6].line
    assert card(approval=None, approval_cleared=True)[6].state == "stop"
    sent = card(approval={"approved_at": "x"}, approved_on="4 Nov 2026", record=SimpleNamespace(state="confirmed", confirmed_at="t"),
                received_on="4 Nov 2026, 2:07 pm (Mexico City time)", due=None)
    assert states(sent)[6:] == ["ok", "ok", "ok", "na"]
    assert sent[8].line.startswith("Received by PayPal on 4 Nov 2026, 2:07 pm") and "second send" in sent[7].line
    assert checked.summary(sent) == "7 of 7 checked"
    pending = card(record=SimpleNamespace(state="attempted", confirmed_at=None))
    assert states(pending)[7:9] == ["stop", "stop"] and "does not send again" in pending[7].line


def test_deadline_row():
    assert card(due={"text": "Respond by 1 Nov 2026", "remaining": "The due date passed 3 days ago", "passed": True})[9].state == "stop"
    assert card(due=None)[9].state == "stop" and card(due=None)[9].line == "No verified deadline available."


# ---- the page ------------------------------------------------------------------------------------------------------------------

def _web(monkeypatch, fake, flag="true"):
    monkeypatch.setenv(FLAG, flag)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return authenticated_client(app, client=("127.0.0.1", 12345))


def _package():
    path = config.db_path()
    data = b"%PDF-1.4 demo letter"
    sha = hashlib.sha256(data).hexdigest()
    write_raw(config.uploads_dir(), sha, data)
    doc = repo.insert_document(path, DISPUTE_ID, "letter.pdf", "application/pdf", len(data), sha, "text", True)
    pkg = repo.insert_package(path, DISPUTE_ID, source="human_edit", validation_ok=False, review_flags=[])
    repo.insert_claims(path, pkg, [{"evidence_index": 0, "claim_index": 0, "text": "Shipped with FedEx", "source_ids": [f"doc_{doc}"],
                                    "basis": "document"}])
    repo.attach_document(path, pkg, doc)
    repo.set_manual_fields(path, DISPUTE_ID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
                                              "carrier": {"value": "FedEx", "confidence": "read"}})
    return pkg


def test_card_page_lists_ten_rows_and_writes_nothing(monkeypatch):
    fake = FakePayPal(raw=dispute_raw())
    web = _web(monkeypatch, fake)
    pkg = _package()
    page = web.get(f"/disputes/{DISPUTE_ID}/packages/{pkg}/checked")
    assert page.status_code == 200, page.text[:400]
    assert page.text.count('data-check="') == 10 and "What Proofie checked before sending" in page.text
    for name in ("The tracking number is this order", "One answer per case", "No deadline passes unseen"):
        assert name in page.text
    assert 'data-check="7" data-check-state="todo"' in page.text and "Not approved yet." in page.text
    assert 'data-check="8" data-check-state="todo"' in page.text
    assert not fake.posts() and all(call[0] == "GET" for call in fake.calls)


def test_newest_package_redirect_and_missing_package(monkeypatch):
    web = _web(monkeypatch, FakePayPal(raw=dispute_raw()))
    assert web.get(f"/disputes/{DISPUTE_ID}/checked", follow_redirects=False).status_code == 404
    pkg = _package()
    hop = web.get(f"/disputes/{DISPUTE_ID}/checked", follow_redirects=False)
    assert hop.status_code == 303 and hop.headers["location"] == f"/disputes/{DISPUTE_ID}/packages/{pkg}/checked"
    assert web.get(f"/disputes/{DISPUTE_ID}/packages/999/checked").status_code == 404
    assert web.get(f"/disputes/PP-R-OTH-0000009/packages/{pkg}/checked").status_code == 404


def test_card_does_not_exist_with_the_outcome_flag_off(monkeypatch):
    web = _web(monkeypatch, FakePayPal(raw=dispute_raw()), flag="false")
    assert web.get(f"/disputes/{DISPUTE_ID}/packages/1/checked").status_code == 404



def test_on_a_sent_package_nothing_is_asked_of_her_for_a_cleared_approval_or_a_proof_not_there():
    """v1.1.2, found on the IMP1-902 take: right after a send PayPal received, the card read '2 need you'."""
    from types import SimpleNamespace
    from copilot.core.checklist import Check
    from copilot.features.outcome import checked
    proof = [Check(cid, cid, "missing" if cid == checked.PROOF_CHECKS[0] else "verified", "published_policy", "Synthetic", [], "none",
                   False, "x") for cid in checked.PROOF_CHECKS]
    sent = SimpleNamespace(state="confirmed")
    card = checked.rows(checks=proof, claims=[], package={}, approval=None, approval_cleared=True, record=sent,
                        received_on="Nov 10")
    assert card[6].state == checked.OK and card[6].line == "PayPal received the package you approved."
    assert card[2].state != checked.STOP and "Your answer is already with PayPal." in card[2].line
    assert "need" not in checked.summary([card[2], card[6]])
    before = checked.rows(checks=proof, claims=[], package={}, approval=None, approval_cleared=True, record=None)
    assert before[6].state == checked.STOP and before[2].state == checked.STOP        # not sent: both still ask her
