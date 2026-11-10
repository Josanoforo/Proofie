"""v1.1.2 version race: the background preparation never creates a package version on top of a package the seller approved,
started or changed by hand. On v1.1.1 each of the three "kept" tests below fails: a second version is created, so her statement
or file sits on a superseded version and her approval has no send. Fake PayPal only."""
import pytest

from copilot.core import auto_prep
from copilot.db import repo

DID = "PP-R-TEST-0001"
NORM = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "update_time": "2026-10-05T00:00:00Z", "create_time": "2026-10-01T00:00:00Z", "seller_transaction_ids": ["TXN1"],
        "evidence_requested_from_seller": ["PROOF_OF_FULFILLMENT"], "evidence_submitted_by_seller": []}


class FakePayPal:
    def call(self, method, path, op, **kw):
        assert method == "GET"
        return {"transaction_details": []} if op == "context_transaction_search" else {"trackers": []}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(auto_prep, "propose_package", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("model off")))
    return path


def _prepare(db):
    """One background preparation exactly as the inbox schedules it: hash of the current data, atomic claim, run."""
    version = auto_prep.compute_version_hash(db, NORM)
    token = repo.claim_auto_prep(db, DID, version)
    if token:
        auto_prep.prepare_dispute(DID, NORM, FakePayPal(), db_path=db, version_hash=version, claim_token=token)
    return token


def _first_draft(db):
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    assert _prepare(db)
    packages = repo.list_packages(db, DID)
    assert len(packages) == 1
    return packages[0]["id"]


def _upload(db):
    repo.insert_document(db, DID, "receipt.pdf", "application/pdf", 10, "sha-new", "Carrier: Estafeta", True)


def test_a_statement_typed_by_the_seller_keeps_her_version_after_an_upload(db):
    package = _first_draft(db)
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "We shipped it.", "source_ids": [],
                                      "basis": "seller_statement"}])
    repo.log_action(db, "vendor", "add_claim", DID, {"package_id": package})        # what the package page records
    _upload(db)
    assert _prepare(db)                                                             # new data: the inbox does schedule it
    packages = repo.list_packages(db, DID)
    assert [p["id"] for p in packages] == [package]                                 # v1.1.1: a second version appears here
    assert repo.get_auto_prep(db, DID)["status"] == "ready" and repo.get_auto_prep(db, DID)["package_id"] == package
    assert any(e["action"] == "auto_prep_kept_seller_package" for e in repo.list_audit(db, DID))


def test_an_approved_package_is_not_superseded_by_an_upload(db):
    package = _first_draft(db)
    repo.insert_approval(db, package, "fingerprint", "vendor")
    _upload(db)
    assert _prepare(db)
    assert [p["id"] for p in repo.list_packages(db, DID)] == [package]
    assert repo.get_latest_valid_approval(db, package) is not None


def test_a_file_attached_by_the_seller_keeps_her_version_after_a_leave_out(db):
    package = _first_draft(db)
    doc = repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha-a", "x", True)
    repo.attach_document(db, package, doc)
    repo.log_action(db, "vendor", "attach_document", DID, {"package_id": package, "document_id": doc})
    _upload(db)
    assert _prepare(db)
    assert [p["id"] for p in repo.list_packages(db, DID)] == [package]


def test_a_draft_only_the_system_touched_is_still_prepared_again_on_new_data(db):
    package = _first_draft(db)
    _upload(db)
    assert _prepare(db)
    assert len(repo.list_packages(db, DID)) == 2 and auto_prep.seller_package(db, DID) is None
    assert package in [p["id"] for p in repo.list_packages(db, DID)]


def test_a_change_made_while_the_preparation_runs_is_seen_before_the_version_is_created(db, monkeypatch):
    package = _first_draft(db)
    _upload(db)

    def seller_types_meanwhile(*a, **kw):                    # the model call is the slow step: she edits during it
        repo.log_action(db, "vendor", "edit_claim", DID, {"package_id": package, "claim_id": 1})
        raise RuntimeError("model off")
    monkeypatch.setattr(auto_prep, "propose_package", seller_types_meanwhile)
    assert _prepare(db)
    assert [p["id"] for p in repo.list_packages(db, DID)] == [package]
