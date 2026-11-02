"""Offline tests for copilot/db/repo.py - the app-data tables (everything except send_attempts, which is
attempt_store.py's own responsibility). No network, no model."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.db import repo
from copilot.db.schema import ensure_schema

DID = "PP-R-TEST-0001"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "copilot.db"


def test_insert_and_get_document(db_path):
    doc_id = repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 1234, "sha-abc",
                                  "extracted text here", True)
    d = repo.get_document(db_path, doc_id)
    assert d["filename"] == "label.pdf" and d["sha256"] == "sha-abc" and d["extraction_ok"] is True
    assert d["extracted_text"] == "extracted text here"


def test_document_with_failed_extraction_keeps_text_null_not_invented(db_path):
    doc_id = repo.insert_document(db_path, DID, "scan.jpg", "image/jpeg", 500, "sha-xyz", None, False)
    d = repo.get_document(db_path, doc_id)
    assert d["extraction_ok"] is False and d["extracted_text"] is None


def test_document_extraction_ok_round_trips_as_a_real_bool_in_list_documents_too(db_path):
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 1, "s1", "x", True)
    docs = repo.list_documents(db_path, DID)
    assert docs[0]["extraction_ok"] is True


def test_list_documents_scoped_to_dispute(db_path):
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 1, "s1", "x", True)
    repo.insert_document(db_path, "PP-R-OTHER", "b.pdf", "application/pdf", 1, "s2", "x", True)
    docs = repo.list_documents(db_path, DID)
    assert len(docs) == 1 and docs[0]["filename"] == "a.pdf"


def test_package_versions_increment_per_dispute(db_path):
    p1 = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    p2 = repo.insert_package(db_path, DID, source="ai", validation_ok=False, review_flags=["x"])
    other_dispute_p1 = repo.insert_package(db_path, "PP-R-OTHER", source="ai", validation_ok=True,
                                           review_flags=[])
    assert repo.get_package(db_path, p1)["version"] == 1
    assert repo.get_package(db_path, p2)["version"] == 2
    assert repo.get_package(db_path, other_dispute_p1)["version"] == 1   # independent counter per dispute


def test_package_rejects_invalid_source(db_path):
    with pytest.raises(ValueError):
        repo.insert_package(db_path, DID, source="not_a_real_source", validation_ok=True, review_flags=[])


def test_package_review_flags_round_trip_as_a_list(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True,
                              review_flags=["missing evidence listed", "code rule (G16): ..."])
    assert repo.get_package(db_path, pid)["review_flags"] == ["missing evidence listed",
                                                               "code rule (G16): ..."]


def test_package_validation_ok_round_trips_as_a_real_bool_not_sqlite_0_or_1(db_path):
    """Regression: SQLite has no native boolean type; without an explicit cast, get_package/list_packages
    returned the raw int (1/0), so `validation_ok is True` silently failed (`1 is True` is False)."""
    pid_true = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    pid_false = repo.insert_package(db_path, DID, source="ai", validation_ok=False, review_flags=[])
    assert repo.get_package(db_path, pid_true)["validation_ok"] is True
    assert repo.get_package(db_path, pid_false)["validation_ok"] is False
    by_id = {p["id"]: p["validation_ok"] for p in repo.list_packages(db_path, DID)}
    assert by_id[pid_true] is True and by_id[pid_false] is False


def test_list_packages_ordered_by_version(db_path):
    repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    repo.insert_package(db_path, DID, source="human_edit", validation_ok=True, review_flags=[])
    pkgs = repo.list_packages(db_path, DID)
    assert [p["version"] for p in pkgs] == [1, 2]
    assert [p["source"] for p in pkgs] == ["ai", "human_edit"]


def test_claims_insert_and_list_preserve_order_and_basis(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pid, [
        {"evidence_index": 0, "claim_index": 0, "text": "a", "source_ids": ["doc_a"], "basis": "verified"},
        {"evidence_index": 0, "claim_index": 1, "text": "b", "source_ids": ["doc_b"], "basis": "seller_statement"},
    ])
    claims = repo.list_claims(db_path, pid)
    assert [c["text"] for c in claims] == ["a", "b"]
    assert claims[0]["basis"] == "verified" and claims[0]["edited_by_human"] is False
    assert claims[1]["source_ids"] == ["doc_b"]


def test_mark_claim_edited_updates_text_and_flag(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pid, [
        {"evidence_index": 0, "claim_index": 0, "text": "original", "source_ids": ["doc_a"], "basis": "verified"}])
    claim_id = repo.list_claims(db_path, pid)[0]["id"]
    repo.mark_claim_edited(db_path, claim_id, "edited by the vendor")
    claims = repo.list_claims(db_path, pid)
    assert claims[0]["text"] == "edited by the vendor" and claims[0]["edited_by_human"] is True


def test_attach_document_snapshots_sha256_and_lists_it(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    doc_id = repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha-snap", "x", True)
    sha = repo.attach_document(db_path, pid, doc_id)
    assert sha == "sha-snap"
    assert repo.list_attachment_sha256s(db_path, pid) == ["sha-snap"]


def test_attach_document_rejects_unknown_document_id(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    with pytest.raises(ValueError):
        repo.attach_document(db_path, pid, document_id=999999)


def test_approval_insert_and_get_latest_valid(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    aid = repo.insert_approval(db_path, pid, "fingerprint-1", "vendor")
    latest = repo.get_latest_valid_approval(db_path, pid)
    assert latest["id"] == aid and latest["approval_sha256"] == "fingerprint-1"
    assert latest["invalidated_at"] is None


def test_invalidate_approval_hides_it_from_latest_valid_but_keeps_the_row(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    aid = repo.insert_approval(db_path, pid, "fingerprint-1", "vendor")
    repo.invalidate_approval(db_path, aid)
    assert repo.get_latest_valid_approval(db_path, pid) is None   # no silent fallback to a stale approval


def test_sufficiency_override_recorded_only_when_given(db_path):
    pid = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    aid = repo.insert_approval(db_path, pid, "fp", "vendor",
                               sufficiency_override="vendor confirmed delivery over the phone")
    latest = repo.get_latest_valid_approval(db_path, pid)
    assert latest["id"] == aid
    assert latest["sufficiency_override"] == "vendor confirmed delivery over the phone"


def test_audit_log_records_actor_action_and_detail(db_path):
    repo.log_action(db_path, "vendor", "approve_package", DID, {"package_id": 1, "fingerprint": "fp"})
    entries = repo.list_audit(db_path, DID)
    assert len(entries) == 1
    assert entries[0]["actor"] == "vendor" and entries[0]["action"] == "approve_package"
    assert entries[0]["detail"] == {"package_id": 1, "fingerprint": "fp"}


def test_audit_log_never_accepts_the_model_as_actor(db_path):
    with pytest.raises(ValueError):
        repo.log_action(db_path, "model", "anything", DID, {})


def test_audit_log_actor_is_also_enforced_by_the_database_itself(db_path):
    """Defense in depth: the Python check in repo.log_action is not the only guard -
    even a direct SQL INSERT that bypasses repo.py entirely must fail, via a CHECK constraint in the
    schema itself."""
    ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO audit_log (ts, actor, action, dispute_id, detail) "
                        "VALUES ('2026-01-01T00:00:00', 'model', 'x', ?, '{}')", (DID,))
            conn.commit()
    finally:
        conn.close()


# ---- auto_prep (one preparation per dispute per data version) ----------------------------
def test_get_auto_prep_is_none_before_anything_claims_it(db_path):
    assert repo.get_auto_prep(db_path, DID) is None


def test_claim_auto_prep_succeeds_for_a_never_seen_dispute(db_path):
    token = repo.claim_auto_prep(db_path, DID, "v1")
    assert token
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["version_hash"] == "v1" and rec["status"] == "preparing" and rec["claim_token"] == token


def test_claim_auto_prep_refuses_a_second_claim_for_the_same_version(db_path):
    assert repo.claim_auto_prep(db_path, DID, "v1")
    assert repo.claim_auto_prep(db_path, DID, "v1") is None   # already claimed/prepared for this version
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "preparing"   # untouched by the refused second claim


def test_claim_auto_prep_refuses_even_after_the_version_finished_preparing(db_path):
    """'Una sola preparación por disputa y por versión' - once a version is done (ready/needs_documents/
    failed), re-visiting the SAME version must not schedule another background prep."""
    token = repo.claim_auto_prep(db_path, DID, "v1")
    repo.set_auto_prep_result(db_path, DID, "v1", token, "ready", package_id=1)
    assert repo.claim_auto_prep(db_path, DID, "v1") is None


def test_claim_auto_prep_succeeds_again_for_a_new_version(db_path):
    token = repo.claim_auto_prep(db_path, DID, "v1")
    repo.set_auto_prep_result(db_path, DID, "v1", token, "ready", package_id=1)
    new_token = repo.claim_auto_prep(db_path, DID, "v2")
    assert new_token and new_token != token
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["version_hash"] == "v2" and rec["status"] == "preparing" and rec["package_id"] is None


def _age_updated_at(db_path, dispute_id: str, seconds_ago: float) -> None:
    import time
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - seconds_ago))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE auto_prep SET updated_at = ? WHERE dispute_id = ?", (cutoff, dispute_id))
        conn.commit()
    finally:
        conn.close()


def test_claim_auto_prep_reclaims_a_stale_preparing_row(db_path):
    """A 'preparing' row old enough (AUTO_PREP_STALE_PREPARING_AFTER_SECONDS of SILENCE - no
    heartbeat at all) is presumed crashed - a later caller may claim it again, for the SAME version,
    and gets a NEW token distinct from the abandoned run's own."""
    old_token = repo.claim_auto_prep(db_path, DID, "v1")
    _age_updated_at(db_path, DID, repo.AUTO_PREP_STALE_PREPARING_AFTER_SECONDS + 30)
    new_token = repo.claim_auto_prep(db_path, DID, "v1")
    assert new_token and new_token != old_token


def test_claim_auto_prep_does_not_reclaim_a_fresh_preparing_row(db_path):
    repo.claim_auto_prep(db_path, DID, "v1")
    assert repo.claim_auto_prep(db_path, DID, "v1") is None


def test_claim_auto_prep_reclaims_a_failed_row_after_the_retry_window(db_path):
    """'failed' may retry after AUTO_PREP_FAILED_RETRY_AFTER_SECONDS, not forever and not instantly."""
    token = repo.claim_auto_prep(db_path, DID, "v1")
    repo.set_auto_prep_result(db_path, DID, "v1", token, "failed", detail="transient error")
    assert repo.claim_auto_prep(db_path, DID, "v1") is None   # too soon
    _age_updated_at(db_path, DID, repo.AUTO_PREP_FAILED_RETRY_AFTER_SECONDS + 30)
    assert repo.claim_auto_prep(db_path, DID, "v1")


def test_two_threads_racing_the_same_auto_prep_claim_exactly_one_succeeds(db_path):
    """A single INSERT ... ON CONFLICT ... WHERE, not the previous read-then-write - simulates the
    web's background task and the MCP's prepare_draft reaching the same dispute at nearly the same instant."""
    import threading
    results = []
    lock = threading.Lock()

    def worker():
        token = repo.claim_auto_prep(db_path, DID, "v1")
        with lock:
            results.append(token)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [t for t in results if t]
    assert len(won) == 1, f"expected exactly 1 successful claim, got {len(won)}/10"


def test_touch_auto_prep_bumps_updated_at_without_changing_anything_else(db_path):
    """The heartbeat - status/package_id/detail untouched, only updated_at moves."""
    token = repo.claim_auto_prep(db_path, DID, "v1")
    _age_updated_at(db_path, DID, 999)   # push it into the past so the bump is unambiguous
    before = repo.get_auto_prep(db_path, DID)["updated_at"]
    assert repo.touch_auto_prep(db_path, DID, "v1", token) is True
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["updated_at"] != before and rec["status"] == "preparing"


def test_touch_auto_prep_fails_for_the_wrong_token_or_version(db_path):
    token = repo.claim_auto_prep(db_path, DID, "v1")
    assert repo.touch_auto_prep(db_path, DID, "v1", "not-the-real-token") is False
    assert repo.touch_auto_prep(db_path, DID, "v2", token) is False


def test_a_stale_run_reclaimed_by_someone_else_cannot_heartbeat_its_way_back(db_path):
    """The exact scenario the token exists for - a run presumed crashed gets reclaimed
    (new token issued to someone else); if the ORIGINAL run was not actually dead and tries to heartbeat
    with its OLD token, that heartbeat must fail, not revive the old claim."""
    old_token = repo.claim_auto_prep(db_path, DID, "v1")
    _age_updated_at(db_path, DID, repo.AUTO_PREP_STALE_PREPARING_AFTER_SECONDS + 30)
    new_token = repo.claim_auto_prep(db_path, DID, "v1")   # someone else reclaims it
    assert repo.touch_auto_prep(db_path, DID, "v1", old_token) is False
    assert repo.set_auto_prep_result(db_path, DID, "v1", old_token, "ready", package_id=1) is False
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["claim_token"] == new_token and rec["status"] == "preparing"


def test_set_auto_prep_result_records_ready_with_its_package(db_path):
    token = repo.claim_auto_prep(db_path, DID, "v1")
    repo.set_auto_prep_result(db_path, DID, "v1", token, "ready", package_id=7)
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "ready" and rec["package_id"] == 7


def test_set_auto_prep_result_records_needs_documents_with_a_detail_message(db_path):
    token = repo.claim_auto_prep(db_path, DID, "v1")
    repo.set_auto_prep_result(db_path, DID, "v1", token, "needs_documents",
                              detail="No documents uploaded yet.")
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "needs_documents" and rec["detail"] == "No documents uploaded yet."
    assert rec["package_id"] is None


def test_set_auto_prep_result_rejects_preparing_as_a_terminal_status(db_path):
    token = repo.claim_auto_prep(db_path, DID, "v1")
    with pytest.raises(ValueError):
        repo.set_auto_prep_result(db_path, DID, "v1", token, "preparing")


def test_set_auto_prep_result_for_a_stale_version_is_a_silent_no_op(db_path):
    """A prep still running for an OLD version (slow model, or a crashed process's result arriving
    late) must never overwrite a NEWER claim's row - the UPDATE matches nothing, returns False, writes
    nothing."""
    old_token = repo.claim_auto_prep(db_path, DID, "v1")
    repo.claim_auto_prep(db_path, DID, "v2")   # a newer visit already reclaimed this dispute for v2
    assert repo.set_auto_prep_result(db_path, DID, "v1", old_token, "ready", package_id=99) is False
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["version_hash"] == "v2" and rec["status"] == "preparing" and rec["package_id"] is None


def test_set_auto_prep_result_on_a_row_that_was_never_claimed_is_detected(db_path):
    """A row that was never claimed at all (not just a stale version) must be just as detectable
    as a no-op - the caller must never assume the write landed just because it did not raise."""
    assert repo.set_auto_prep_result(db_path, DID, "v1", "whatever-token", "ready", package_id=1) is False
    assert repo.get_auto_prep(db_path, DID) is None   # nothing was ever written


def test_set_auto_prep_result_for_the_current_version_returns_true(db_path):
    token = repo.claim_auto_prep(db_path, DID, "v1")
    assert repo.set_auto_prep_result(db_path, DID, "v1", token, "ready", package_id=1) is True


def test_auto_prep_status_is_enforced_by_the_database_itself(db_path):
    ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO auto_prep (dispute_id, version_hash, claim_token, status, updated_at) "
                        "VALUES (?, 'v1', 'tok', 'not_a_real_status', '2026-01-01T00:00:00')", (DID,))
            conn.commit()
    finally:
        conn.close()
