"""outcome_view, the sentences, appeal_allowed, prepare_appeal and the appeal store."""
import json

import pytest

from copilot import config
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.features.outcome import appeal, service, store
from outcome_fakes import CAPTURE, DECIDED, DISPUTE_ID, FakePayPal, dispute_raw, paypal_error

APPEAL_FLAG = "COPILOT_FEATURE_OUTCOME_APPEAL"


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(APPEAL_FLAG, raising=False)


def test_undecided_sentence_and_no_appeal():
    v = service.outcome_view(FakePayPal(dispute_raw()), config.db_path(), DISPUTE_ID)
    assert v.sentence == "PayPal has not decided yet."
    assert v.appeal_sentence is None and v.appeal_allowed is False and not v.read.decided


def test_decided_sentence_states_the_label_as_stated_by_paypal():
    v = service.outcome_view(FakePayPal(dispute_raw(**DECIDED)), config.db_path(), DISPUTE_ID)
    assert v.sentence == "Decided: Resolved in the buyer's favor as stated by PayPal"
    assert v.appeal_sentence == ("Appeal window: PayPal shows an appeal link in this dispute; PayPal accepts an appeal only with new information (PayPal Help Center article 230).")


def test_appeal_allowed_needs_the_link_a_decision_and_the_flag(monkeypatch):
    decided = FakePayPal(dispute_raw(**DECIDED))
    assert service.outcome_view(decided, config.db_path(), DISPUTE_ID).appeal_allowed is False      # flag off
    monkeypatch.setenv(APPEAL_FLAG, "true")
    assert service.outcome_view(decided, config.db_path(), DISPUTE_ID).appeal_allowed is True
    no_link = FakePayPal(dispute_raw(status="RESOLVED", rels=(), outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"}))
    assert service.outcome_view(no_link, config.db_path(), DISPUTE_ID).appeal_allowed is False
    undecided_with_link = FakePayPal(dispute_raw(rels=("appeal",)))
    assert service.outcome_view(undecided_with_link, config.db_path(), DISPUTE_ID).appeal_allowed is False


def test_an_unreadable_dispute_gives_a_plain_sentence_and_limitations_and_the_file_still_shows():
    pkg = repo.insert_package(config.db_path(), DISPUTE_ID, source="human_edit", validation_ok=True, review_flags=[])
    v = service.outcome_view(FakePayPal(get_raises=paypal_error(500)), config.db_path(), DISPUTE_ID)
    assert v.sentence.startswith("Outcome not read") and v.read.limitations and v.gaps.limitations
    assert [p["id"] for p in v.file.packages] == [pkg]


def test_the_view_reads_the_dispute_through_the_outcome_op_and_writes_nothing():
    fake = FakePayPal(dispute_raw())
    service.outcome_view(fake, config.db_path(), DISPUTE_ID)
    assert all(c[0] == "GET" for c in fake.calls) and "outcome_read" in {c[2] for c in fake.calls}


# ---- prepare_appeal -----------------------------------------------------------------------------------------------
def _package_with_tracking_and_file():
    path = config.db_path()
    data = b"%PDF-1.4 demo letter"
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    write_raw(config.uploads_dir(), sha, data)
    doc = repo.insert_document(path, DISPUTE_ID, "letter.pdf", "application/pdf", len(data), sha, "text", True)
    pkg = repo.insert_package(path, DISPUTE_ID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(path, pkg, [{"evidence_index": 0, "claim_index": 0, "text": "Delivered 2026-10-02",
                                    "source_ids": [], "basis": "seller_statement"}])
    repo.attach_document(path, pkg, doc)
    repo.set_manual_fields(path, DISPUTE_ID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
                                              "carrier": {"value": "FedEx", "confidence": "read"}})
    return pkg, sha


def test_prepare_appeal_builds_the_body_the_hash_and_the_attachments_from_a_package():
    pkg, sha = _package_with_tracking_and_file()
    p = service.prepare_appeal(config.db_path(), DISPUTE_ID, pkg)
    assert p.reason == "" and p.body_sha256 == appeal.request_sha256(p.body)
    assert p.sha256 == appeal.approval_sha256(DISPUTE_ID, p.body, [sha]) != p.body_sha256
    assert p.attachments[0]["sha256"] == sha
    assert p.body["evidences"][0]["evidence_info"]["tracking_info"][0]["tracking_number"] == "DEMO-FDX-000001"
    assert [(a["filename"], a["data"]) for a in p.attachments] == [("letter.pdf", b"%PDF-1.4 demo letter")]


def test_the_preview_fingerprint_changes_when_an_attached_file_is_swapped():
    pkg, sha = _package_with_tracking_and_file()
    before = service.prepare_appeal(config.db_path(), DISPUTE_ID, pkg).sha256
    import hashlib
    data = b"another letter"
    new_sha = hashlib.sha256(data).hexdigest()
    write_raw(config.uploads_dir(), new_sha, data)
    doc = repo.insert_document(config.db_path(), DISPUTE_ID, "other.pdf", "application/pdf", len(data), new_sha, "t", True)
    repo.attach_document(config.db_path(), pkg, doc)
    assert service.prepare_appeal(config.db_path(), DISPUTE_ID, pkg).sha256 != before


def test_prepare_appeal_refuses_a_file_whose_content_changed_on_disk():
    pkg, sha = _package_with_tracking_and_file()
    path = config.uploads_dir() / sha
    candidates = [p for p in config.uploads_dir().rglob("*") if p.is_file()]
    assert candidates
    candidates[0].write_bytes(b"tampered")
    assert "changed on disk" in service.prepare_appeal(config.db_path(), DISPUTE_ID, pkg).reason


def test_prepare_appeal_refuses_another_disputes_package_and_a_missing_tracking_number():
    other = repo.insert_package(config.db_path(), "PP-R-TST-0000009", source="human_edit", validation_ok=True, review_flags=[])
    assert service.prepare_appeal(config.db_path(), DISPUTE_ID, other).body is None
    mine = repo.insert_package(config.db_path(), DISPUTE_ID, source="human_edit", validation_ok=True, review_flags=[])
    p = service.prepare_appeal(config.db_path(), DISPUTE_ID, mine)
    assert p.body is None and "tracking number" in p.reason
    assert service.prepare_appeal(config.db_path(), DISPUTE_ID, 999).body is None


def test_prepare_appeal_says_so_when_an_attached_file_is_gone_from_disk():
    pkg, _sha = _package_with_tracking_and_file()
    import shutil
    shutil.rmtree(config.uploads_dir(), ignore_errors=True)
    assert "could not be found on disk" in service.prepare_appeal(config.db_path(), DISPUTE_ID, pkg).reason


# ---- the appeal store ------------------------------------------------------------------------------------------------
def _save(**kw):
    args = dict(dispute_id=DISPUTE_ID, request={"evidences": []}, request_sha="a" * 64, approval_sha="a" * 64,
                readback={"stage": "ARBITRATION", "status": "X", "links": [], "outcome": None}, status="sent_confirmed")
    args.update(kw)
    return store.save_appeal(**args)


def test_the_store_round_trips_oldest_first_and_audits_hashes_and_status_only():
    first = _save()
    second = _save(status="failed", readback=None, limitation_text="PayPal could not send the appeal (error 422).")
    rows = store.list_appeals(DISPUTE_ID)
    assert [r["id"] for r in rows] == [first["id"], second["id"]] and rows[1]["limitation_text"].startswith("PayPal could not")
    assert json.loads(rows[0]["readback_json"])["stage"] == "ARBITRATION" and store.list_appeals("PP-R-TST-0000009") == []
    audit = [a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "outcome_appeal"]
    assert len(audit) == 2 and audit[0]["actor"] == "vendor"
    assert set(audit[0]["detail"]) == {"request_sha256", "approval_sha256", "status"}


def test_an_unknown_store_status_is_refused():
    with pytest.raises(ValueError):
        _save(status="rejected_link")
