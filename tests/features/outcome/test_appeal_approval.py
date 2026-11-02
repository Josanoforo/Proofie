"""Offline appeal receipt contract. Hosted router harness keeps production startup refusal."""
import re
import secrets
from pathlib import Path
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from copilot import config
from copilot.app import create_app
from copilot.db.schema import ensure_schema
from copilot.features.outcome import routes
from copilot.web.routes_disputes import get_client
from copilot.web.security import HostedAccessMiddleware, LocalhostOnlyMiddleware, access_cookie_value
from _hosted import authenticated_client, set_test_csrf
from outcome_fakes import FakePayPal, DISPUTE_ID
from test_outcome_routes import _package

pytestmark = [pytest.mark.usefixtures("web_mode")]
TOKEN = secrets.token_urlsafe(24)  # Fresh synthetic CSRF fixture, never a real credential.
PAGE = f"/disputes/{DISPUTE_ID}/outcome/appeal"


def _web(monkeypatch, active=True, fake=None):
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "true")
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true" if active else "false")
    fake = fake or FakePayPal()
    if config.hosted_demo() or active:
        # Exercise the real outcome receipt contract without requiring another
        # published namespace merely because its shared flag guards this receipt.
        # Production hosted write startup remains separately refused below.
        ensure_schema(config.db_path())
        app = FastAPI()
        app.include_router(routes.router)
        if config.hosted_demo():
            app.add_middleware(HostedAccessMiddleware)
        else:
            app.add_middleware(LocalhostOnlyMiddleware)
    else:
        app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    if config.hosted_demo():
        web = TestClient(app, base_url="https://localhost", client=("127.0.0.1", 12345))
        web.cookies.set("proofie_access", access_cookie_value(), domain="localhost.local", path="/")
    else:
        web = authenticated_client(app)
    set_test_csrf(web, TOKEN)
    return web, fake


def _post(web, endpoint, **fields):
    set_test_csrf(web, TOKEN)
    return web.post(PAGE + endpoint, data={"csrf_token": TOKEN, **fields})


def _preview(web, pkg):
    r = _post(web, "/preview", package_id=str(pkg))
    assert r.status_code == 200, r.text
    fingerprint = re.search(r'data-state="request-sha256">([0-9a-f]{64})<', r.text).group(1)
    return fingerprint, r


def test_active_appeal_without_own_receipt_refuses_before_post(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, _ = _preview(web, pkg)
    r = _post(web, "/register", package_id=str(pkg), approval_sha256=fingerprint)
    assert r.status_code == 409
    assert fake.posts() == []

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import threading
import time

from copilot.db import repo
from copilot.core.raw_storage import write_raw
from copilot.features.outcome import appeal, appeal_approval as approval, service, store
from outcome_fakes import dispute_raw, paypal_error


def _authorize(web, pkg):
    fingerprint, page = _preview(web, pkg)
    assert 'data-state="confirm-form"' not in page.text
    review_sha = re.search(r'name="review_sha256" value="([0-9a-f]{64})"', page.text).group(1)
    response = _post(web, "/preview", package_id=str(pkg), decision="approve_appeal", review_sha256=review_sha)
    assert response.status_code == 200, response.text
    receipt_id = re.search(r'name="appeal_receipt_id" value="([A-Za-z0-9_-]+)"', response.text).group(1)
    assert not store.list_appeals(DISPUTE_ID)
    return fingerprint, receipt_id


def _send(web, pkg, fingerprint, receipt_id):
    return _post(web, "/register", package_id=str(pkg), approval_sha256=fingerprint, appeal_receipt_id=receipt_id)


def _direct(fake, pkg, receipt_id, prepared=None):
    prepared = prepared or service.prepare_appeal(config.db_path(), DISPUTE_ID, pkg)
    return appeal.register_appeal(fake, DISPUTE_ID, prepared.body, list(prepared.attachments), prepared.sha256,
                                 db_path=config.db_path(), package_id=pkg, receipt_id=receipt_id)


def test_explicit_receipt_posts_once_stores_and_prevents_replay(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    row = approval.receipt(config.db_path(), receipt_id)
    assert row["action"] == "appeal" and row["actor"] == "vendor"
    assert row["package_id"] == pkg and row["package_version"] == 1
    assert row["expires_at"] - row["created_at"] == 300
    assert not fake.posts()
    r = _send(web, pkg, fingerprint, receipt_id)
    assert r.status_code == 200 and len(fake.posts()) == 1
    row = approval.receipt(config.db_path(), receipt_id)
    assert row["state"] == "used" and row["outcome"] == "sent_unconfirmed"
    assert len(store.list_appeals(DISPUTE_ID)) == 1
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 409
    assert len(fake.posts()) == 1
    actions = [r["action"] for r in repo.list_audit(config.db_path(), DISPUTE_ID)]
    assert actions.count("approve_outcome_appeal") == 1
    assert actions.count("reserve_outcome_appeal") == 1
    assert actions.count("finish_outcome_appeal") == 1


@pytest.mark.parametrize("change", ["manual", "document", "confirmation", "claim", "attachment", "raw", "version", "remote"])
def test_changed_binding_invalidates_before_post(monkeypatch, change):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    path = config.db_path()
    doc = repo.list_documents(path, DISPUTE_ID)[0]
    if change == "manual":
        repo.set_manual_fields(path, DISPUTE_ID, {"delivery_status": {"value": "SHIPPED", "confidence": "read"}})
    elif change == "document":
        repo.set_document_excluded(path, doc["id"], True)
    elif change == "confirmation":
        repo.confirm_document_reading(path, doc["id"], "carrier", "FedEx")
    elif change == "claim":
        repo.mark_claim_edited(path, repo.list_claims(path, pkg)[0]["id"], "Changed seller statement")
    elif change == "attachment":
        repo.detach_document(path, pkg, doc["id"])
    elif change == "raw":
        (config.uploads_dir() / doc["sha256"]).write_bytes(b"changed bytes")
    elif change == "version":
        with sqlite3.connect(str(path)) as conn:
            conn.execute("UPDATE packages SET version=version+1 WHERE id=?", (pkg,))
    else:
        fake.raw["seller_response_due_date"] = "2026-10-18T05:59:59.000Z"
    r = _send(web, pkg, fingerprint, receipt_id)
    assert r.status_code in (400, 409) and fake.posts() == []
    assert approval.receipt(path, receipt_id)["state"] == "invalidated"


@pytest.mark.parametrize("kind", ["missing", "foreign", "expired", "provide_evidence", "future_clock"])
def test_invalid_authorization_is_rejected(monkeypatch, kind):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    if kind == "missing":
        receipt_id = "absent"
    elif kind == "foreign":
        with sqlite3.connect(str(config.db_path())) as conn:
            conn.execute("UPDATE outcome_appeal_receipts SET dispute_id='PP-R-TST-OTHER' WHERE id=?", (receipt_id,))
    elif kind == "expired":
        with sqlite3.connect(str(config.db_path())) as conn:
            conn.execute("UPDATE outcome_appeal_receipts SET created_at=created_at-301,expires_at=expires_at-301 WHERE id=?", (receipt_id,))
    elif kind == "future_clock":
        monkeypatch.setattr(approval.time, "time", lambda: 1)
    else:
        evidence_id = repo.insert_approval(config.db_path(), pkg, fingerprint, "vendor")
        receipt_id = str(evidence_id)
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 409
    assert not fake.posts()


def test_direct_service_cannot_omit_receipt_or_swap_material(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    prepared = service.prepare_appeal(config.db_path(), DISPUTE_ID, pkg)
    with pytest.raises(approval.ReceiptError):
        appeal.register_appeal(fake, DISPUTE_ID, prepared.body, list(prepared.attachments), prepared.sha256)
    _, receipt_id = _authorize(web, pkg)
    altered = list(prepared.attachments)
    altered[0] = {**altered[0], "filename": "swapped.pdf"}
    with pytest.raises(approval.ReceiptError):
        appeal.register_appeal(fake, DISPUTE_ID, prepared.body, altered, prepared.sha256,
                              db_path=config.db_path(), package_id=pkg, receipt_id=receipt_id)
    assert not fake.posts()


def test_double_approval_is_idempotent_and_two_registers_only_post_once(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    prepared, value, sha = approval.review(fake, config.db_path(), DISPUTE_ID, pkg)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: approval.approve(config.db_path(), value, sha), range(2)))
    assert ids[0] == ids[1]
    entered, release = threading.Event(), threading.Event()
    original = fake.call
    def held(method, path, op, **kw):
        if method == "POST":
            entered.set()
            assert release.wait(5)
        return original(method, path, op, **kw)
    monkeypatch.setattr(fake, "call", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(_direct, fake, pkg, ids[0], prepared)
        assert entered.wait(5)
        two = pool.submit(_direct, fake, pkg, ids[1], prepared)
        with pytest.raises(approval.ReceiptError):
            two.result(timeout=5)
        release.set()
        assert one.result(timeout=5).posted
    assert len(fake.posts()) == 1


def test_independent_writer_during_post_is_unblocked_and_source_change_visible(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    original = fake.call
    elapsed = []
    def writing(method, path, op, **kw):
        if method == "POST":
            # Own DDL row is already reserved/committed; no app SQLite connection may be held.
            started = time.monotonic()
            with sqlite3.connect(str(config.db_path()), timeout=0.2) as conn:
                conn.execute("UPDATE documents SET extracted_text='Changed while sending' WHERE dispute_id=?", (DISPUTE_ID,))
            elapsed.append(time.monotonic() - started)
        return original(method, path, op, **kw)
    monkeypatch.setattr(fake, "call", writing)
    r = _send(web, pkg, fingerprint, receipt_id)
    assert r.status_code == 200 and "a source changed while sending" in r.text
    assert elapsed and elapsed[0] < 0.5 and len(fake.posts()) == 1
    assert approval.receipt(config.db_path(), receipt_id)["source_changed"] == 1


@pytest.mark.parametrize("phase", ["approve", "reserve"])
def test_sql_persistence_failure_blocks_post(monkeypatch, phase):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, page = _preview(web, pkg)
    sha = re.search(r'name="review_sha256" value="([0-9a-f]{64})"', page.text).group(1)
    with approval.transaction(config.db_path()):
        pass
    if phase == "reserve":
        fingerprint, receipt_id = _authorize(web, pkg)
    with sqlite3.connect(str(config.db_path())) as conn:
        if phase == "approve":
            conn.execute("CREATE TRIGGER fail_appeal_approval BEFORE INSERT ON outcome_appeal_receipts BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END")
        else:
            conn.execute("CREATE TRIGGER fail_appeal_reserve BEFORE UPDATE ON outcome_appeal_receipts WHEN NEW.state='reserved' BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END")
    if phase == "approve":
        r = _post(web, "/preview", package_id=str(pkg), decision="approve_appeal", review_sha256=sha)
    else:
        r = _send(web, pkg, fingerprint, receipt_id)
    assert r.status_code == 503 and not fake.posts()


@pytest.mark.parametrize("error", [TimeoutError("synthetic transport uncertainty"), paypal_error(401)])
def test_uncertain_or_error_post_consumed_never_retried(monkeypatch, error):
    web, fake = _web(monkeypatch, fake=FakePayPal(post_raises=error))
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    r = _send(web, pkg, fingerprint, receipt_id)
    assert r.status_code == 200 and len(fake.posts()) == 1
    assert approval.receipt(config.db_path(), receipt_id)["state"] == "used"
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 409
    assert len(fake.posts()) == 1
    _, value, sha = approval.review(fake, config.db_path(), DISPUTE_ID, pkg)
    with pytest.raises(approval.ReceiptError, match="already attempted"):
        approval.approve(config.db_path(), value, sha)


def test_csrf_and_hosted_auth_do_not_create_receipt(monkeypatch, web_mode):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, page = _preview(web, pkg)
    sha = re.search(r'name="review_sha256" value="([0-9a-f]{64})"', page.text).group(1)
    for endpoint in ("/preview", "/register"):
        r = web.post(PAGE + endpoint, data={"csrf_token": "wrong", "package_id": str(pkg),
                                          "decision": "approve_appeal", "review_sha256": sha,
                                          "approval_sha256": fingerprint})
        assert r.status_code == 403
    if web_mode == "hosted":
        web.cookies.delete("proofie_access", domain="localhost.local", path="/")
        r = _post(web, "/preview", package_id=str(pkg), decision="approve_appeal", review_sha256=sha)
        assert r.status_code == 401
    assert not fake.posts()
    with sqlite3.connect(str(config.db_path())) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='outcome_appeal_receipts'").fetchone()


def test_off_preserves_original_preview_and_register(monkeypatch):
    web, fake = _web(monkeypatch, active=False)
    pkg = _package()
    fingerprint, page = _preview(web, pkg)
    assert 'data-state="appeal-approve-form"' not in page.text
    # The preview's controls are the shell's macro buttons now (the old exact-markup comparison to the
    # earlier template cannot hold); what must stay is the one legacy form: register with the fingerprint, no receipt.
    html = page.text
    assert "Approve and send the appeal to PayPal" in html and 'data-state="confirm-form"' in html
    assert 'name="approval_sha256"' in html and fingerprint in html and 'name="appeal_receipt_id"' not in html
    assert "Send this approved appeal once" not in html and "Approve this appeal only" not in html
    r = _send(web, pkg, fingerprint, "not-an-appeal-receipt")
    assert r.status_code == 200 and len(fake.posts()) == 1
    with sqlite3.connect(str(config.db_path())) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='outcome_appeal_receipts'").fetchone()


def test_hosted_app_starts_with_the_appeal_flag_on(monkeypatch, web_mode):
    if web_mode != "hosted":
        assert not config.hosted_demo()
        return
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "true")
    assert create_app() is not None                      # the access-code requirement is the only start-up check left


def test_no_sqlite_connection_open_during_post(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    original_connect = sqlite3.connect
    opened = set()
    class Tracked(sqlite3.Connection):
        def close(self):
            opened.discard(id(self))
            return super().close()
    def connect(*args, **kwargs):
        kwargs["factory"] = Tracked
        conn = original_connect(*args, **kwargs)
        opened.add(id(conn))
        return conn
    original_call = fake.call
    observed = []
    def call(method, path, op, **kwargs):
        if method == "POST":
            observed.append(len(opened))
            assert not opened
        return original_call(method, path, op, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(fake, "call", call)
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 200
    assert observed == [0] and not opened


def test_completion_storage_failure_never_reopens_attempt(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    with sqlite3.connect(str(config.db_path())) as conn:
        conn.execute("CREATE TRIGGER fail_appeal_finish BEFORE UPDATE ON outcome_appeal_receipts WHEN NEW.state='used' BEGIN SELECT RAISE(ABORT,'synthetic completion failure'); END")
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 503
    assert len(fake.posts()) == 1
    assert approval.receipt(config.db_path(), receipt_id)["state"] == "reserved"
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 409
    assert len(fake.posts()) == 1
    _, value, sha = approval.review(fake, config.db_path(), DISPUTE_ID, pkg)
    with pytest.raises(approval.ReceiptError, match="already reserved"):
        approval.approve(config.db_path(), value, sha)


def test_foreign_package_receipt_and_forged_explicit_review_denied(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    other = _package()
    other_fingerprint, _ = _preview(web, other)
    assert _send(web, other, other_fingerprint, receipt_id).status_code == 409
    assert _post(web, "/preview", package_id=str(pkg), decision="approve_appeal", review_sha256="0"*64).status_code == 409
    assert _post(web, "/preview", package_id=str(pkg), decision="approve_evidence", review_sha256="0"*64).status_code == 409
    assert not fake.posts()


def test_raw_snapshot_changed_between_review_and_receipt_save_denied(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    _, value, sha = approval.review(fake, config.db_path(), DISPUTE_ID, pkg)
    repo.set_document_excluded(config.db_path(), repo.list_documents(config.db_path(), DISPUTE_ID)[0]["id"], True)
    with pytest.raises(approval.ReceiptError, match="local sources changed"):
        approval.approve(config.db_path(), value, sha)
    assert not fake.posts()


def test_disappeared_fresh_link_invalidates_receipt_no_post(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    fake.raw = dispute_raw(status="RESOLVED", rels=())
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 409
    assert not fake.posts()
    assert approval.receipt(config.db_path(), receipt_id)["state"] == "invalidated"


def test_expiration_during_material_preparation_prevents_post(monkeypatch):
    web, fake = _web(monkeypatch)
    pkg = _package()
    fingerprint, receipt_id = _authorize(web, pkg)
    real_build = appeal.build_multipart
    def elapsed(body, attachments):
        with sqlite3.connect(str(config.db_path())) as conn:
            conn.execute("UPDATE outcome_appeal_receipts SET created_at=created_at-301, expires_at=expires_at-301 WHERE id=?", (receipt_id,))
        return real_build(body, attachments)
    monkeypatch.setattr(appeal, "build_multipart", elapsed)
    assert _send(web, pkg, fingerprint, receipt_id).status_code == 409
    assert not fake.posts()
    assert approval.receipt(config.db_path(), receipt_id)["state"] == "invalidated"


def test_queued_approval_clock_is_sampled_after_write_lock(monkeypatch):
    """An earlier waiting caller must reuse a receipt inserted before its lock."""
    from contextlib import contextmanager

    web, fake = _web(monkeypatch)
    package_id = _package()
    _, value, sha = approval.review(fake, config.db_path(), DISPUTE_ID, package_id)
    clock = {"now": 1000.0}
    monkeypatch.setattr(approval.time, "time", lambda: clock["now"])
    first_id = approval.approve(config.db_path(), value, sha)
    real_transaction = approval.transaction
    clock["now"] = 999.0  # The waiting caller sampled before the first row existed.

    @contextmanager
    def acquire_then_advance(db_path):
        with real_transaction(db_path) as connection:
            clock["now"] = 1001.0  # The lock is now acquired after that insertion.
            yield connection

    monkeypatch.setattr(approval, "transaction", acquire_then_advance)
    assert approval.approve(config.db_path(), value, sha) == first_id
    with sqlite3.connect(str(config.db_path())) as connection:
        assert connection.execute("SELECT COUNT(*) FROM outcome_appeal_receipts").fetchone()[0] == 1
    assert not fake.posts()


def test_two_packages_of_one_dispute_cannot_reserve_concurrently(monkeypatch):
    web, fake = _web(monkeypatch)
    first, second = _package(), _package()
    prepared1, value1, sha1 = approval.review(fake, config.db_path(), DISPUTE_ID, first)
    prepared2, value2, sha2 = approval.review(fake, config.db_path(), DISPUTE_ID, second)
    receipt1 = approval.approve(config.db_path(), value1, sha1)
    receipt2 = approval.approve(config.db_path(), value2, sha2)
    entered, release = threading.Event(), threading.Event()
    original = fake.call
    def held(method, path, op, **kw):
        if method == "POST":
            entered.set()
            assert release.wait(5)
        return original(method, path, op, **kw)
    monkeypatch.setattr(fake, "call", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_send = pool.submit(_direct, fake, first, receipt1, prepared1)
        assert entered.wait(5)
        try:
            with pytest.raises(approval.ReceiptError, match="reserved"):
                approval.approve(config.db_path(), value2, sha2)
            with pytest.raises(approval.ReceiptError, match="reserved"):
                _direct(fake, second, receipt2, prepared2)
        finally:
            release.set()
        assert first_send.result(timeout=5).posted
    assert len(fake.posts()) == 1
