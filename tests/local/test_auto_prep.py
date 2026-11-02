"""Offline tests for copilot/core/auto_prep.py (automatic background preparation). The real
model/GPU are never touched here - propose_package is monkeypatched for the AI-path tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.core import auto_prep
from copilot.core.checklist import UnknownCountryError
from copilot.db import repo

DID = "PP-R-TEST-0001"
DISPUTE_NORM = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
                "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "update_time": "2026-10-05T00:00:00Z",
                "create_time": "2026-10-01T00:00:00Z", "seller_transaction_ids": ["TXN1"],
                "evidence_requested_from_seller": ["PROOF_OF_FULFILLMENT"], "evidence_submitted_by_seller": []}


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append(op)
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


# ---- compute_version_hash -----------------------------------------------------------------------------
def test_version_hash_is_stable_without_any_change(db_path):
    h1 = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    h2 = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    assert h1 == h2


def test_version_hash_changes_when_a_document_is_added(db_path):
    before = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "x", True)
    after = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    assert before != after


def test_version_hash_changes_when_manual_fields_change(db_path):
    before = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    repo.set_manual_fields(db_path, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    after = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    assert before != after


def test_version_hash_changes_when_paypals_update_time_changes(db_path):
    before = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    after = auto_prep.compute_version_hash(db_path, {**DISPUTE_NORM, "update_time": "2026-10-06T00:00:00Z"})
    assert before != after


def test_version_hash_ignores_an_excluded_document(db_path):
    doc_id = repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "x", True)
    with_doc = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    repo.set_document_excluded(db_path, doc_id, True)
    after_exclude = auto_prep.compute_version_hash(db_path, DISPUTE_NORM)
    assert with_doc != after_exclude   # excluding it IS a real change (docs/db/repo.py's auto_prep is
                                       # expected to notice and re-prepare without the excluded document)


# ---- should_prepare -------------------------------------------------------------------------------------
def test_should_prepare_true_for_waiting_for_seller_response():
    assert auto_prep.should_prepare(DISPUTE_NORM) is True


def test_should_prepare_false_for_other_statuses():
    assert auto_prep.should_prepare({**DISPUTE_NORM, "status": "WAITING_FOR_BUYER_RESPONSE"}) is False
    assert auto_prep.should_prepare({**DISPUTE_NORM, "status": "UNDER_REVIEW"}) is False


def test_should_prepare_false_for_excluded_disputes(monkeypatch):
    monkeypatch.setattr(config, "AUTOPREP_EXCLUDE_IDS", frozenset({DID, "PP-R-OTHER-9999"}))
    assert auto_prep.should_prepare(DISPUTE_NORM) is False
    assert auto_prep.should_prepare({**DISPUTE_NORM, "dispute_id": "PP-R-NOT-EXCLUDED"}) is True


# ---- prepare_dispute: needs_documents ---------------------------------------------------------------------
def test_prepare_dispute_with_nothing_uploaded_is_needs_documents(db_path):
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    rec = repo.get_auto_prep(db_path, DID)
    assert rec is None   # prepare_dispute itself does not claim/write the auto_prep row - the caller does;
                         # this only asserts the RESULT path (needs_documents) via a direct call, see the
                         # routes_disputes integration tests for the claim->schedule->prepare flow
    assert client.calls == []   # never even reached fetch_context - nothing to build from
    assert repo.list_packages(db_path, DID) == []

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_needs_documents" for e in entries)


def test_prepare_dispute_needs_documents_detail_names_what_paypal_requested(db_path):
    """Name WHAT PayPal is actually asking for (evidence_requested_from_seller), in human language -
    not just "no documents uploaded yet", which says nothing about what to upload."""
    token = repo.claim_auto_prep(db_path, DID, "v1")
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token=token)
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "needs_documents"
    assert "proof the order was shipped or fulfilled" in rec["detail"]   # humanize_evidence_type(PROOF_OF_FULFILLMENT)

    entries = repo.list_audit(db_path, DID)
    entry = next(e for e in entries if e["action"] == "auto_prep_needs_documents")
    assert entry["detail"]["evidence_requested_from_seller"] == ["PROOF_OF_FULFILLMENT"]


# ---- An image-only dispute is no longer "needs_documents" - a purely additive
# vision pass can still suggest something, even with zero readable text and zero manual fields ----------
def test_prepare_dispute_with_only_an_image_builds_a_manual_draft_with_vision_claims(db_path, monkeypatch):
    repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha1", None, False)
    monkeypatch.setattr(auto_prep, "claims_for_dispute_images",
                        lambda *a, **kw: [{"evidence_index": 0, "claim_index": 0,
                                          "text": "A carrier logo is visible.", "source_ids": ["doc_1"],
                                          "basis": "model_interpretation"}])
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    pkgs = repo.list_packages(db_path, DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "auto_manual"
    claims = repo.list_claims(db_path, pkgs[0]["id"])
    assert any(c["text"] == "A carrier logo is visible." for c in claims)


# ---- prepare_dispute: manual fields only (no documents) still builds a draft --------------------------
def test_prepare_dispute_with_only_manual_fields_builds_a_manual_draft(db_path, monkeypatch):
    repo.set_manual_fields(db_path, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    called = []
    monkeypatch.setattr(auto_prep, "propose_package", lambda *a, **kw: called.append(1))
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    assert called == []   # no document -> never even tries the model
    pkgs = repo.list_packages(db_path, DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "auto_manual"

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_context_fetched" for e in entries)
    assert any(e["action"] == "auto_prep_checklist_built" for e in entries)
    assert any(e["action"] == "auto_prep_manual_draft" for e in entries)


# ---- prepare_dispute: a document exists, AI unavailable (GPU busy etc.) falls back to manual -----------
def test_prepare_dispute_falls_back_to_manual_when_the_model_is_unavailable(db_path, monkeypatch):
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)

    def _raise(*a, **kw):
        raise RuntimeError("GPU or Ollama busy (another model is loaded or the GPU is in use), not running")

    monkeypatch.setattr(auto_prep, "propose_package", _raise)
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    pkgs = repo.list_packages(db_path, DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "auto_manual"

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_ai_unavailable" for e in entries)
    assert any(e["action"] == "auto_prep_manual_draft" for e in entries)
    assert not any(e["action"] == "auto_prep_ai_draft" for e in entries)


# ---- prepare_dispute: a document exists, AI available builds a real draft ------------------------------
GOOD_OUT = {"proposed_evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "shipped",
                                  "tracking_info": [{"carrier_name": "MEX_ESTAFETA", "tracking_number": "DEMO1",
                                                     "source_ids": ["doc_1"]}],
                                  "claims": [{"text": "Shipped via Estafeta.", "source_ids": ["doc_1"],
                                             "basis": "verified"}]}],
           "missing_evidence": [], "conflicts": [], "recommended_next_step": "submit_evidence_for_human_approval",
           "reviewer_note_draft": "n"}


def _fake_rec():
    return {"validation_ok": True, "review_flags": [], "model": "qwen3.5:9b", "prompt_sha256": "abc123",
           "raw_output": "{}", "effective_next_step": "submit_evidence_for_human_approval",
           "_output": GOOD_OUT, "unsupported_delivery_assertions": []}


def test_prepare_dispute_builds_an_ai_draft_when_a_document_exists_and_the_model_is_available(db_path, monkeypatch):
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(auto_prep, "propose_package", lambda *a, **kw: _fake_rec())
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    pkgs = repo.list_packages(db_path, DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "ai" and pkgs[0]["validation_ok"] is True
    claims = repo.list_claims(db_path, pkgs[0]["id"])
    assert len(claims) == 1 and claims[0]["text"] == "Shipped via Estafeta."

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_ai_draft" for e in entries)
    assert not any(e["action"] == "auto_prep_manual_draft" for e in entries)


# ---- prepare_dispute: an unsupported delivery assertion must be visible on the review screen
# itself, not only discovered later when a human tries to approve ----------------------------------------
UNSUPPORTED_OUT = {"proposed_evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "shipped",
                                          "tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                             "tracking_number": "DEMO1",
                                                             "source_ids": ["doc_1"]}],
                                          "claims": [{"text": "Package was delivered to the customer.",
                                                     "source_ids": ["doc_1"], "basis": "verified"}]}],
                   "missing_evidence": [], "conflicts": [],
                   "recommended_next_step": "submit_evidence_for_human_approval", "reviewer_note_draft": "n"}


def test_prepare_dispute_flags_an_ai_delivery_claim_unsupported_by_its_own_source(db_path, monkeypatch):
    """The claim asserts 'delivered', but the document it cites ('Carrier: Estafeta') never says
    so - without the fix this test fails (nothing lands in review_flags and the audit count stays 0)."""
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(auto_prep, "propose_package", lambda *a, **kw: {
        "validation_ok": True, "review_flags": [], "model": "qwen3.5:9b", "prompt_sha256": "abc123",
        "raw_output": "{}", "effective_next_step": "submit_evidence_for_human_approval",
        "_output": UNSUPPORTED_OUT})
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    pkg = repo.list_packages(db_path, DID)[0]
    assert any("delivered" in f.lower() for f in pkg["review_flags"])

    entries = repo.list_audit(db_path, DID)
    draft_entry = next(e for e in entries if e["action"] == "auto_prep_ai_draft")
    assert draft_entry["detail"]["unsupported_delivery_assertions"] == 1


def test_prepare_dispute_never_approves_or_sends_anything(db_path, monkeypatch):
    """The rule that never changes, checked directly: no approval row, no send_attempts row, regardless
    of which path built the draft."""
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(auto_prep, "propose_package", lambda *a, **kw: _fake_rec())
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    pkg_id = repo.list_packages(db_path, DID)[0]["id"]
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None
    entries = repo.list_audit(db_path, DID)
    assert not any(e["actor"] != "system" for e in entries)   # every auto_prep step is actor='system'


# ---- prepare_dispute: checklist country configuration error --------------------------------------------
def test_prepare_dispute_unknown_country_is_recorded_as_failed_not_a_crash(db_path, monkeypatch):
    def _raise():
        raise UnknownCountryError("no rule set for country 'ZZ'")

    monkeypatch.setattr(auto_prep, "cached_rules", _raise)
    repo.set_manual_fields(db_path, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")
    assert repo.list_packages(db_path, DID) == []   # never got far enough to build a draft
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_failed" and e["detail"]["stage"] == "checklist" for e in entries)


# ---- prepare_dispute: a real programming error reading PayPal context is recorded, never crashes --------
def test_prepare_dispute_context_error_is_recorded_as_failed_not_a_crash(db_path, monkeypatch):
    def _raise(client, dispute_norm):
        raise RuntimeError("boom")

    monkeypatch.setattr(auto_prep, "fetch_context", _raise)
    repo.set_manual_fields(db_path, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    client = FakeClient()
    auto_prep.prepare_dispute(DID, DISPUTE_NORM, client, db_path=db_path, version_hash="v1",
                              claim_token="tok1")   # must not raise
    assert repo.list_packages(db_path, DID) == []
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_failed" and e["detail"]["stage"] == "context" for e in entries)
