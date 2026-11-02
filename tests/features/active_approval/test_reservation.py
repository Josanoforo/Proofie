"""Short reservations must not retain SQLite writers during a fake network POST."""
import sqlite3
import time
import pytest
from concurrent.futures import ThreadPoolExecutor
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service
from .test_guarantees import DID, SendFake, ready, decisions


@pytest.mark.parametrize("change", ["notes", "file_omitted", "file_changed", "extra_entry"])
def test_dr1b_finished_read_still_invalidates_real_remote_changes(prepared, change):
    import copy
    import hashlib
    from copilot import config
    from copilot.core.raw_storage import write_raw
    from copilot.core.send import verify_after_with_attachments, sha256_json
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.features.active_approval import store
    from test_routes_send import _minimal_pdf_with_lines
    _, fake, db, package, _ = prepared
    text = "DEMO DATA Synthetic attachment."
    raw = _minimal_pdf_with_lines([text]); sha = hashlib.sha256(raw).hexdigest()
    document = repo.insert_document(db,DID,"dr1b-proof.pdf","application/pdf",len(raw),sha,text,True)
    write_raw(config.uploads_dir(),sha,raw)
    repo.attach_document(db,package,document)
    review = ready(fake,db,package)
    aid = service.approve(fake,db,DID,package,review.review_sha256,decisions(review),"","")
    class Attached(SendFake):
        def call(self,method,path,op,**kwargs):
            result=super().call(method,path,op,**kwargs)
            if method == "POST":
                files=[value for key,value in kwargs["multipart"] if key != "input"]
                assert files == [("dr1b-proof.pdf",raw,"application/pdf")]
                self.body["evidences"][0]["documents"]=[{"name":"dr1b-proof.pdf"}]
            return result
    sender=Attached(fake,review)
    assert send_package(sender,db,DID,package).http_status == 200
    assert service.read(sender,db,DID,package).approval["id"] == aid
    assert store.reservation_state(db,aid) == "finished" and sender.posts == 1
    if change == "notes": sender.body["evidences"][0]["notes"] += " Unreviewed synthetic change."
    elif change == "file_omitted": sender.body["evidences"][0]["documents"] = []
    elif change == "file_changed": sender.body["evidences"][0]["documents"] = [{"name":"different-proof.pdf"}]
    else:
        extra=copy.deepcopy(sender.body["evidences"][0]);extra["notes"]="Another synthetic entry."
        sender.body["evidences"].append(extra)
    landed=verify_after_with_attachments(sender.body,review.request_input,["dr1b-proof.pdf"])
    if change.startswith("file_"): assert not landed["files_ok"]
    if change == "notes": assert not landed["content_ok"]
    after=service.read(sender,db,DID,package)
    assert after.cleared and after.approval is None
    assert sender.posts == 1 and store.reservation_state(db,aid) == "finished"
    assert store.receipt(db,aid) is not None
    attempts=SQLiteAttemptStore(db)
    try: assert attempts.read(DID,sha256_json(review.request_input))["state"] == "confirmed"
    finally: attempts.close()


def test_dr1b_confirmed_result_has_next_task_and_no_warning():
    from copilot.web.routes_send import _page
    from copilot.core.send_recovery import RECEIVED_MESSAGE
    text = _page(RECEIVED_MESSAGE, "Confirmed by PayPal's record.", DID, 1, 200).body.decode()
    assert "<h1>Evidence sent to PayPal</h1>" in text
    assert "What happens next" in text and "Follow PayPal's status on the dispute page." in text
    assert f'href="/disputes/{DID}"' in text
    assert "banner-warn" not in text


def test_dr1b_unreviewed_extra_evidence_is_not_masked_as_our_outcome(prepared):
    import copy
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    class ExtraEvidence(SendFake):
        def call(self,method,path,op,**kwargs):
            result=super().call(method,path,op,**kwargs)
            if method == "POST":
                extra=copy.deepcopy(self.body["evidences"][0]);extra["notes"]="Unreviewed synthetic statement."
                self.body["evidences"].append(extra)
            return result
    sender=ExtraEvidence(fake,review)
    result=send_package(sender,db,DID,package)
    # PayPal's read-back confirms our entry, so the headline is sent; the extra entry is the secondary note
    assert result.http_status == 200 and result.sent and sender.posts == 1
    assert result.note.startswith("Changed after sending:")
    assert repo.get_latest_valid_approval(db,package) is None


def test_dr1b_confirmed_status_and_evidence_transition_is_not_an_edit(prepared):
    _, fake, db, package, _ = prepared
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE claims SET text=?, source_ids_json=? WHERE package_id=?",
                     (DID, '["paypal_dispute"]', package))
    review = ready(fake, db, package)
    approval_id = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    class ConfirmedTransition(SendFake):
        def call(self, method, path, op, **kwargs):
            result = super().call(method, path, op, **kwargs)
            if method == "POST":
                self.body["status"] = "UNDER_REVIEW"
                self.body["dispute_life_cycle_stage"] = "CLAIM"
                self.body["links"] = [link for link in self.body.get("links", []) if link["method"] == "GET"]
            return result
    sender = ConfirmedTransition(fake, review)
    result = send_package(sender, db, DID, package)
    assert result.http_status == 200 and result.sent and sender.posts == 1
    from copilot.web.routes_send import _page
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    from copilot.web.timefmt import display
    attempts = SQLiteAttemptStore(db)
    try: confirmation = attempts.read(DID, sha256_json(review.request_input))
    finally: attempts.close()
    rendered = _page(result.title, result.message, DID, package, result.http_status).body.decode()
    assert "Confirmed by reading the dispute back from PayPal at" in rendered
    assert display(confirmation["ts"]) in rendered and "Mexico City time" in rendered
    assert repo.get_latest_valid_approval(db, package)["id"] == approval_id
    after = service.read(sender, db, DID, package)
    assert after.approval and after.approval["id"] == approval_id
    assert not after.cleared
    assert not any(c.id.startswith("case_request:") and c.state == "missing" for c in after.checks)
    # History is not a new permission: the same content never issues another POST.
    repeat = send_package(sender, db, DID, package)
    assert repeat.http_status == 409 and "Nothing new was sent." in repeat.message
    assert not repeat.sent and sender.posts == 1
    claim = repo.list_claims(db, package)[0]
    repo.mark_claim_edited(db, claim["id"], "A changed seller statement.")
    assert service.read(sender, db, DID, package).cleared


def test_writer_succeeds_during_post_and_change_is_visible(prepared):
    _, fake, db, package, doc = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    class SlowPost(SendFake):
        def call(self, method, path, op, **kwargs):
            if method == "POST":
                def write():
                    start = time.monotonic()
                    with sqlite3.connect(db, timeout=.4) as conn:
                        conn.execute("UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?", ("\nConcurrent source", doc))
                    return time.monotonic()-start
                with ThreadPoolExecutor(max_workers=1) as pool:
                    elapsed = pool.submit(write).result(timeout=1)
                assert elapsed < .5
                time.sleep(2)
            return super().call(method, path, op, **kwargs)
    sender=SlowPost(fake,review)
    result=send_package(sender,db,DID,package)
    assert sender.posts == 1
    assert result.sent and "changed after sending" in result.note.lower()   # Secondary note, not the headline
    assert repo.get_latest_valid_approval(db,package) is None
    assert any(row["action"] == "active_send_sources_changed" for row in repo.list_audit(db,DID))


def test_feature_schema_is_idempotent_and_expired_reservation_is_not_honoured(prepared):
    from copilot.features.active_approval import store
    _, fake, db, package, _ = prepared
    review=ready(fake,db,package)
    aid=service.approve(fake,db,DID,package,review.review_sha256,decisions(review),"","")
    for _ in range(2):
        with store.transaction(db) as conn:
            assert conn.execute("SELECT count(*) FROM active_send_reservations").fetchone()[0] == 0
    with store.transaction(db) as conn:
        conn.execute("INSERT INTO active_send_reservations VALUES (?, 'expired', 0, 'old', 'old', 'reserved')",(aid,))
    sender=SendFake(fake,review)
    outcome=send_package(sender,db,DID,package)
    assert outcome.http_status == 409 and sender.posts == 0


def test_paypal_source_changes_during_post_is_detected(prepared):
    _, fake, db, package, _ = prepared
    review=ready(fake,db,package)
    service.approve(fake,db,DID,package,review.review_sha256,decisions(review),"","")
    class ChangedRemote(SendFake):
        def call(self,method,path,op,**kw):
            result=super().call(method,path,op,**kw)
            if method == "POST":
                self.body["dispute_amount"]["value"]="26.00"
            return result
    sender=ChangedRemote(fake,review)
    outcome=send_package(sender,db,DID,package)
    assert outcome.http_status == 200 and outcome.sent and sender.posts == 1   # Confirmed read-back is the headline
    assert "changed after sending" in outcome.note.lower()
    assert repo.get_latest_valid_approval(db,package) is None
