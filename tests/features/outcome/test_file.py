"""sent_file on a throwaway database (conftest isolates config.db_path() per test)."""
import sys

import pytest

from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.db import repo
from copilot.features.fulfilment import store as fulfil_store
from copilot.features.fulfilment import tracker_request as tr
from copilot.features.outcome import file as sent
from copilot.features.resolution import store as res_store
from outcome_fakes import CAPTURE, DISPUTE_ID

SHA_DOC = "d" * 64


@pytest.fixture
def db():
    path = config.db_path()
    doc_id = repo.insert_document(path, DISPUTE_ID, "estafeta_demo_letter.pdf", "application/pdf", 2048, SHA_DOC,
                                  "text", True)
    pkg = repo.insert_package(path, DISPUTE_ID, source="human_edit", validation_ok=True, review_flags=["a notice"])
    repo.insert_claims(path, pkg, [
        {"evidence_index": 0, "claim_index": 0, "text": "Shipped on 2026-09-30", "source_ids": [], "basis": "seller_statement"},
        {"evidence_index": 0, "claim_index": 1, "text": "Delivered 2026-10-02", "source_ids": [], "basis": "seller_statement",
         "edited_by_human": True}])
    repo.attach_document(path, pkg, doc_id)
    repo.insert_approval(path, pkg, approval_sha256="a" * 64, approved_by="vendor")
    store = SQLiteAttemptStore(path)
    store.write(DISPUTE_ID, "b" * 64, {"state": "confirmed", "http_status": 200, "request_input": {"evidences": []},
                                      "attachment_filenames": ["estafeta_demo_letter.pdf"]})
    store.close()
    repo.log_action(path, "vendor", "approve_package", DISPUTE_ID, {"package_id": pkg})
    return path, pkg


def test_the_file_lists_packages_claims_attachments_attempts_and_audit(db):
    path, pkg = db
    f = sent.sent_file(path, DISPUTE_ID)
    assert len(f.packages) == 1
    p = f.packages[0]
    assert (p["id"], p["validation_ok"], p["approval_sha256"]) == (pkg, True, "a" * 64)
    assert [(c["text"], c["edited"]) for c in p["claims"]] == [("Shipped on 2026-09-30", False), ("Delivered 2026-10-02", True)]
    assert p["attachments"] == ({"filename": "estafeta_demo_letter.pdf", "sha256": SHA_DOC, "size_bytes": 2048},)
    assert [(a["package_sha256"], a["state"], a["http_status"]) for a in f.send_attempts] == [("b" * 64, "confirmed", 200)]
    assert f.send_attempts[0]["attachment_filenames"] == ["estafeta_demo_letter.pdf"] and f.send_attempts[0]["ts"]
    assert [a["action"] for a in f.audit] == ["approve_package"]


def test_a_dispute_with_nothing_is_empty_not_an_error():
    f = sent.sent_file(config.db_path(), "PP-R-TST-0000009")
    assert (f.packages, f.send_attempts, f.audit, f.tracker) == ((), (), (), None)


def test_cross_feature_reads_are_none_or_empty_when_nothing_is_stored(db):
    f = sent.sent_file(db[0], DISPUTE_ID, CAPTURE)
    assert f.tracker is None and f.resolution_actions == () and f.notes == ()


def test_cross_feature_reads_list_what_the_other_features_stored(db):
    body = tr.build_tracker_body(CAPTURE, "TRK1TEST20261006", "FEDEX", "FedEx")
    sha = tr.request_sha256(body)
    fulfil_store.save_file(CAPTURE, body, sha, sha, None, {"checks": []}, "registered")
    res_store.save_action(DISPUTE_ID, "message", {"message": "hello"}, "c" * 64, "c" * 64, None, True, "sent_confirmed")
    f = sent.sent_file(db[0], DISPUTE_ID, CAPTURE)
    assert f.tracker["tracking_number"] == "TRK1TEST20261006" and f.tracker["status"] == "registered"
    assert [(r["action"], r["status"], r["request_sha256"]) for r in f.resolution_actions] == [("message", "sent_confirmed", "c" * 64)]


def test_without_a_capture_id_the_tracker_is_not_looked_up(db):
    assert sent.sent_file(db[0], DISPUTE_ID).tracker is None


def test_an_absent_feature_package_is_none_with_a_note_not_an_error(db, monkeypatch):
    monkeypatch.setitem(sys.modules, "copilot.features.fulfilment.lookup", None)
    monkeypatch.setitem(sys.modules, "copilot.features.resolution.store", None)
    f = sent.sent_file(db[0], DISPUTE_ID, CAPTURE)
    assert f.tracker is None and f.resolution_actions is None and len(f.notes) == 2
    assert f.packages and f.send_attempts          # the core tables are still read
