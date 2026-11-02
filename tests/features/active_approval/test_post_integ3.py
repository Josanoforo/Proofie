"""Causal regression probes for the integration findings, synthetic transport only."""
import copy
import sqlite3
import httpx
import pytest
from copilot.core.disputes import PayPalError
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service
from .test_guarantees import DID, SendFake, ready, decisions


def authorize(fake, db, package):
    review = ready(fake, db, package)
    return review, service.approve(fake, db, DID, package, review.review_sha256,
                                  decisions(review), "", "")


def test_definitive_rejection_requires_new_approval_and_posts_once(prepared):
    _, fake, db, package, _ = prepared
    review, first = authorize(fake, db, package)
    class Rejected(SendFake):
        def call(self, method, path, op, **kw):
            if method == "POST":
                self.posts += 1
                raise PayPalError(422, "synthetic", "VALIDATION_ERROR", "synthetic")
            return super().call(method, path, op, **kw)
    sender = Rejected(fake, review)
    assert send_package(sender, db, DID, package).http_status == 502
    assert sender.posts == 1
    review, second = authorize(fake, db, package)
    assert second != first
    sender = SendFake(fake, review)
    assert send_package(sender, db, DID, package).http_status == 200
    assert sender.posts == 1
    assert send_package(sender, db, DID, package).http_status == 409      # Package already received
    assert sender.posts == 1


@pytest.mark.parametrize("with_prior", [False, True])
def test_recorded_documents_readback_preserves_prior_evidence(prepared, monkeypatch, with_prior):
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    prior = {"evidence_type": "PROOF_OF_REFUND", "source": "SUBMITTED_BY_SELLER",
             "notes": "Earlier seller statement", "evidence_info": copy.deepcopy(review.request_input["evidences"][0]["evidence_info"])}
    if with_prior:
        fake.raw["evidences"].append(prior)
        # Core intentionally refuses ANY earlier seller evidence. Isolate the
        # post-send source comparison without changing that protected policy.
        from copilot.core import send_service
        monkeypatch.setattr(send_service, "check_preconditions", lambda *a: [])
    review = service.read(fake, db, DID, package)
    aid = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    class Documents(SendFake):
        def call(self, method, path, op, **kw):
            result = super().call(method, path, op, **kw)
            if method == "POST":
                self.body["evidences"][0]["documents"] = [{"name": "estafeta_demo_letter.pdf"}]
                if with_prior:
                    self.body["evidences"].insert(0, copy.deepcopy(prior))
            return result
    sender = Documents(fake, review)
    sender.body["evidences"] = copy.deepcopy(fake.raw["evidences"])
    result = send_package(sender, db, DID, package)
    assert result.http_status == 200 and result.sent and sender.posts == 1
    assert repo.get_latest_valid_approval(db, package)["id"] == aid


def test_transport_error_and_changed_sources_report_unknown(prepared, monkeypatch):
    from copilot.core import send_recovery
    monkeypatch.setattr(send_recovery.time, "sleep", lambda _: None)
    _, fake, db, package, doc = prepared
    review, _ = authorize(fake, db, package)
    class Unknown(SendFake):
        def call(self, method, path, op, **kw):
            if method == "POST":
                self.posts += 1
                with sqlite3.connect(db) as conn:
                    conn.execute("UPDATE documents SET extracted_text='Changed source' WHERE id=?", (doc,))
                raise httpx.ReadTimeout("Synthetic unknown outcome")
            return super().call(method, path, op, **kw)
    sender = Unknown(fake, review)
    result = send_package(sender, db, DID, package)
    assert result.http_status == 202 and "unknown" in result.title.lower()
    assert not result.sent and sender.posts == 1
    assert any(r["action"] == "send_outcome_unknown" for r in repo.list_audit(db, DID))


@pytest.mark.parametrize("age", [0, 301])
def test_live_reservation_is_not_stolen_and_stale_needs_new_approval(prepared, age):
    import time
    from copilot.features.active_approval import store
    _, fake, db, package, _ = prepared
    review, first = authorize(fake, db, package)
    with store.transaction(db) as conn:
        conn.execute("INSERT INTO active_send_reservations VALUES (?, 'old-token', ?, 'local', 'review', 'reserved')",
                     (first, time.time()-age))
    if age == 0:
        with pytest.raises(service.ReviewError, match="still reserved"):
            authorize(fake, db, package)
    else:
        _, second = authorize(fake, db, package)
        assert second != first
        sender = SendFake(fake, review)
        assert send_package(sender, db, DID, package).http_status == 200
        assert sender.posts == 1
    with store.transaction(db) as conn:
        assert conn.execute("SELECT state FROM active_send_reservations WHERE token='old-token'").fetchone()[0] == "reserved"


def test_new_explicit_approval_never_reposts_unknown_attempt(prepared, monkeypatch):
    from copilot.core import send_recovery
    monkeypatch.setattr(send_recovery.time, "sleep", lambda _: None)
    _, fake, db, package, _ = prepared
    review, first = authorize(fake, db, package)
    class Unknown(SendFake):
        def call(self, method, path, op, **kw):
            if method == "POST":
                self.posts += 1
                raise httpx.ReadTimeout("Synthetic uncertain attempt")
            return super().call(method, path, op, **kw)
    sender = Unknown(fake, review)
    assert send_package(sender, db, DID, package).http_status == 202
    _, second = authorize(fake, db, package)
    assert second != first
    retry = SendFake(fake, review)
    assert send_package(retry, db, DID, package).http_status == 409
    assert retry.posts == 0 and sender.posts == 1
