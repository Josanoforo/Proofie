"""Real process/HTTP regressions for the gate4 source-binding findings; fake transport only."""
import copy
import sqlite3
import subprocess
import sys
import pytest

from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service, store
from _hosted import authenticated_client, set_test_csrf

DID = "PP-R-DEMO-0000001"


def test_missing_package_in_fresh_database_is_a_controlled_refusal(tmp_path):
    class NoClient:
        def call(self, *args, **kwargs):
            raise AssertionError("Missing package must not query PayPal")
    with pytest.raises(service.ReviewError, match="belonging to this dispute"):
        service.approve(NoClient(), tmp_path / "fresh.db", DID, 999, "", {}, "", "")


@pytest.fixture
def mode_prepared(web_mode, prepared):
    return prepared


def decisions(review):
    return {c.key: "seller_statement" if c.verdict == "unverifiable" else "confirmed" for c in review.claims}


class SendFake:
    def __init__(self, fake, review):
        self.original = fake
        self.body = copy.deepcopy(fake.raw)
        self.body["evidences"] = [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}]
        self.review = review
        self.posts = 0

    def call(self, method, path, op, **kwargs):
        if method == "POST":
            self.posts += 1
            self.body["evidences"] = [{**e, "source": "SUBMITTED_BY_SELLER"} for e in self.review.request_input["evidences"]]
            return {}
        if "transactions" in path or "trackers" in path:
            return self.original.call(method, path, op, **kwargs)
        assert method == "GET"
        return copy.deepcopy(self.body)


def ready(fake, db, package):
    fake.raw["evidences"] = [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}]
    review = service.read(fake, db, DID, package)
    return review


def test_missing_receipt_cannot_send_shared_service(prepared):
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    repo.insert_approval(db, package, review.fingerprint, "vendor")
    sender = SendFake(fake, review)
    result = send_package(sender, db, DID, package)
    assert result.http_status == 409 and not result.sent
    assert sender.posts == 0
    assert repo.get_latest_valid_approval(db, package) is None


def test_source_only_edit_direct_http_send_refused_without_review_visit(mode_prepared):
    web, fake, db, package, doc = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?", ("\nChanged source", doc))
    sender = SendFake(fake, review)
    from copilot.web.routes_disputes import get_client
    web.app.dependency_overrides[get_client] = lambda: sender
    web = authenticated_client(web.app)
    set_test_csrf(web, "DEMO-CSRF")
    response = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": "DEMO-CSRF"})
    assert response.status_code == 409
    assert sender.posts == 0


def test_valid_active_approval_still_sends_once(prepared):
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = SendFake(fake, review)
    outcome = send_package(sender, db, DID, package)
    assert outcome.sent and sender.posts == 1


CHILD = '''
import os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests/features/active_approval'))
from conftest import FakeClient
from copilot.features.active_approval import service,store
db,pid,mode,folder=sys.argv[1:];pid=int(pid)
fake=FakeClient();review=service.read(fake,db,'PP-R-DEMO-0000001',pid)
choices={c.key:'seller_statement' if c.verdict=='unverifiable' else 'confirmed' for c in review.claims}
if mode=='crash':
    store.save=lambda *a,**kw:os._exit(73)
else:
    Path(folder,mode).write_text('ready',encoding='utf-8')
    start=time.monotonic()
    while not Path(folder,'release').exists():
        if time.monotonic()-start>20:raise RuntimeError('barrier timeout')
        time.sleep(.01)
print(service.approve(fake,db,'PP-R-DEMO-0000001',pid,review.review_sha256,choices,'',''),flush=True)
'''


def test_process_crash_rolls_back_approval_and_receipt(prepared, tmp_path):
    _, _, db, package, _ = prepared
    child = subprocess.run([sys.executable, "-c", CHILD, str(db), str(package), "crash", str(tmp_path)],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM active_approval_receipts").fetchone()[0] == 0


def test_two_processes_share_one_durable_approval(prepared, tmp_path):
    import time
    _, _, db, package, _ = prepared
    children = [subprocess.Popen([sys.executable, "-c", CHILD, str(db), str(package), str(i), str(tmp_path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    start = time.monotonic()
    while not all((tmp_path / str(i)).exists() for i in range(2)):
        assert time.monotonic() - start < 20, "child review barrier timed out"
        time.sleep(.01)
    (tmp_path / "release").write_text("release", encoding="utf-8")
    outputs = [p.communicate(timeout=30) for p in children]
    assert [p.returncode for p in children] == [0, 0], outputs
    assert outputs[0][0].strip() == outputs[1][0].strip()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM active_approval_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='approve_package'").fetchone()[0] == 1


def test_source_change_during_review_refuses_atomic_write(prepared, monkeypatch):
    _, fake, db, package, doc = prepared
    review = service.read(fake, db, DID, package)
    original = service.read
    def edit_after_read(*args):
        result = original(*args)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?", ("\nConcurrent edit", doc))
        return result
    monkeypatch.setattr(service, "read", edit_after_read)
    with pytest.raises(service.ReviewError, match="changed"):
        service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    assert repo.get_latest_valid_approval(db, package) is None


def test_source_edit_between_send_review_and_reservation_refuses_post(prepared, monkeypatch):
    _, fake, db, package, doc = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    original = service.read
    def edit_after_read(*args):
        result = original(*args)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?", ("\nRacing edit", doc))
        return result
    monkeypatch.setattr(service, "read", edit_after_read)
    sender = SendFake(fake, review)
    assert send_package(sender, db, DID, package).http_status == 409
    assert sender.posts == 0


def test_source_writer_can_change_inputs_inside_post_reservation_and_is_detected(prepared):
    _, fake, db, package, doc = prepared
    review = ready(fake, db, package)
    aid = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    with service.send_guard(fake, db, DID, package, aid) as reservation:
        with sqlite3.connect(db, timeout=0) as conn:
            conn.execute("UPDATE documents SET sha256='changed' WHERE id=?", (doc,))
    assert reservation.changed
    assert repo.get_latest_valid_approval(db, package) is None


def test_review_get_failure_is_not_a_post_rejection(prepared):
    import httpx
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    class UnreachableReview(SendFake):
        def call(self, method, path, op, **kwargs):
            if op == "show_dispute":
                raise httpx.ConnectError("Synthetic review GET unavailable")
            return super().call(method, path, op, **kwargs)
    sender = UnreachableReview(fake, review)
    result = send_package(sender, db, DID, package)
    assert result.http_status == 503 and not result.sent and sender.posts == 0
    registry = SQLiteAttemptStore(db)
    try:
        assert registry.read(DID, sha256_json(review.request_input))["state"] == "blocked"
    finally:
        registry.close()


@pytest.mark.parametrize("missing_receipt", [True, False])
def test_mcp_direct_send_cannot_bypass_active_receipt_or_source_change(prepared, monkeypatch, missing_receipt):
    from copilot import mcp_server
    _, fake, db, package, doc = prepared
    review = ready(fake, db, package)
    if missing_receipt:
        repo.insert_approval(db, package, review.fingerprint, "vendor")
    else:
        service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?", ("\nMCP stale source", doc))
    sender = SendFake(fake, review)
    monkeypatch.setattr(mcp_server, "_make_client", lambda: sender)
    outcome = mcp_server.send_approved_package(DID)
    assert not outcome["sent"] and outcome["http_status"] == 409
    assert sender.posts == 0


def test_third_send_after_confirmed_uses_finished_approval_without_post(prepared):
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    aid = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = SendFake(fake, review)
    assert send_package(sender, db, DID, package).sent
    assert sender.posts == 1
    send_package(sender, db, DID, package)
    # Later GET no longer exposes matching evidence; historical confirmed attempt remains.
    sender.body["evidences"] = []
    third = send_package(sender, db, DID, package)
    assert third.http_status == 409 and not third.sent
    assert third.title == "Package already received"
    assert "Nothing new was sent" in third.message
    assert sender.posts == 1  # repeated calls add zero POST
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM active_send_reservations WHERE approval_id=?", (aid,)).fetchone()[0] == "finished"
        assert conn.execute("SELECT invalidated_at FROM approvals WHERE id=?", (aid,)).fetchone()[0] is None
    assert store.receipt(db, aid) is not None  # retain history; reservation consumes use
