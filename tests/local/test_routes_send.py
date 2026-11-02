"""Offline tests for the send route. This route now performs the REAL send logic -
including the actual POST to PayPal - so these tests exercise all of it against a scripted FakeClient.
The rule that this route is never invoked against a REAL sandbox client without explicit approval of the exact
content is procedural, not something the code enforces -
see copilot/web/routes_send.py's module docstring.
"""
from _hosted import authenticated_client, reset_csrf
import json
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.app import create_app
from copilot.core import send_recovery, send_service
from copilot.core.disputes import PayPalError
from copilot.core.send import DEMO_PREFIX
from copilot.db import repo
from copilot.web.routes_disputes import get_client
from copilot.web.security import CSRF_COOKIE_NAME

REC = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
# Sendable double; the historical normalization fixture remains unchanged on disk.
WAITING["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
NO_LINK = {**WAITING, "links": [l for l in WAITING["links"] if l["rel"] != "provide_evidence"]}
DID = "PP-R-DEMO-0000001"
TRACKING_NUMBER = "DEMO-FDX-000001"


# PayPal's own GET must show the SAME notes we would have sent, or a confirmed/pending-looking
# fixture no longer counts as matching (verify_after_with_attachments now checks notes_match, not just
# tracking number). _approved_package_with_tracking() below never adds a claim, so the real notes a send
# would carry are just DEMO_PREFIX by itself (build_request_input's "".join(claims) is empty) - this is
# that exact string, not an arbitrary placeholder.
NOTES_NO_CLAIMS = DEMO_PREFIX.strip()


def _confirmed_body(tracking_number=TRACKING_NUMBER, documents=None, notes=NOTES_NO_CLAIMS):
    e = {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
        "evidence_info": {"tracking_info": [{"tracking_number": tracking_number}]},
        "notes": notes, "date": "2026-10-05T00:00:00Z"}
    if documents is not None:
        e["documents"] = documents
    return {"evidences": [e]}


NOT_YET_BODY = {"evidences": []}


def _minimal_pdf_with_lines(lines: list) -> bytes:
    """One BT...Td...Tj...ET block per line - pypdfium2 preserves a real line break between separate text
    runs like this (proven empirically; a single Tj with embedded multi-space gaps instead gets collapsed
    to single spaces on extraction, which breaks field_extraction.py's same-line-split logic)."""
    parts, y = [], 90
    for line in lines:
        parts.append(f"BT /F1 10 Tf 10 {y} Td ({line}) Tj ET")
        y -= 12
    stream = "\n".join(parts).encode("latin-1")
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
           b"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 300 150]/Contents 5 0 R>>",
           b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
           b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)


TRACKING_PDF = _minimal_pdf_with_lines(["Tracking no.: DEMO-FDX-000001", "Carrier: FedEx",
                                        "Status: Entregado"])


class FakeClient:
    def __init__(self, detail_body=WAITING, pre_send_get_body=None, recovery_get_body=None,
                post_send_get_bodies=None, provide_evidence_error=None,
                provide_evidence_transport_error=False, recovery_get_error=None):
        self.detail_body = detail_body
        self.pre_send_get_body = pre_send_get_body if pre_send_get_body is not None else detail_body
        self.recovery_get_body = recovery_get_body if recovery_get_body is not None else detail_body
        self.post_send_get_bodies = list(post_send_get_bodies or [])
        self.provide_evidence_error = provide_evidence_error
        self.provide_evidence_transport_error = provide_evidence_transport_error
        self.recovery_get_error = recovery_get_error
        self.calls_log = []
        self.provide_evidence_calls = []

    def call(self, method, path, op, **kw):
        self.calls_log.append(op)
        if op == "show_dispute":
            return self.detail_body
        if op == "pre_send_get":
            return self.pre_send_get_body
        if op == "recovery_get":
            if self.recovery_get_error:
                raise self.recovery_get_error
            return self.recovery_get_body
        if op == "post_send_get":
            return self.post_send_get_bodies.pop(0)
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        if op == "provide_evidence":
            self.provide_evidence_calls.append(kw)
            if self.provide_evidence_transport_error:
                raise httpx.ReadTimeout("fake timeout")
            if self.provide_evidence_error:
                raise self.provide_evidence_error
            return {}
        raise AssertionError(f"unexpected op {op}")


def _client(tmp_path=None, monkeypatch=None, **fake_kwargs):
    # tmp_path/monkeypatch accepted (unused) so every existing call site stays unchanged; conftest.py's
    # autouse _isolated_copilot_data fixture already redirects config.db_path()/uploads_dir() for us.
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)   # the reconciliation delays never really wait here
    app = create_app()
    fake = FakeClient(**fake_kwargs)
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return c, config.db_path(), token, fake


def _approved_package_with_tracking(c, db_path, token, dispute_id=DID):
    r = c.post(f"/disputes/{dispute_id}/documents", files={"file": ("label.pdf", TRACKING_PDF, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post(f"/disputes/{dispute_id}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    r = c.post(f"/disputes/{dispute_id}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "sufficiency_override": "Tracking confirmed, ready to send."},
              follow_redirects=False)
    assert r.status_code == 303, r.text
    return pkg_id


def _seed_unconfirmed_check_attempt(c, db_path, token, *, attach_file=False, partial=False):
    """Seed a local already-attempted send for read-only /check route coverage; no provider write occurs."""
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    attachment_names = []
    if attach_file:
        doc_id = repo.list_documents(db_path, DID)[0]["id"]
        assert c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}",
                      data={"csrf_token": token}, follow_redirects=False).status_code == 303
        attachment_names = ["label.pdf"]
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import build_request_input, pick_tracking
    from copilot.core.send import sha256_json
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    request_input = build_request_input(repo.list_claims(db_path, pkg_id), tn, carrier)
    package_sha = sha256_json(request_input)
    request_id = f"local-check-{pkg_id}"
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "sent_unconfirmed", "request_id": request_id,
        "request_input": request_input, "attachment_filenames": attachment_names,
        "reason": "missing_attachment:label.pdf" if partial else None})
    store.close()
    repo.log_action(db_path, "system", "send_attempt_registered", DID,
                    {"package_id": pkg_id, "request_id": request_id, "attachments": attachment_names})
    return pkg_id


def test_send_without_approval_is_refused(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 400
    assert "not approved" in r.text
    assert fake.calls_log == []   # never even reached the fresh-GET step


def test_send_without_csrf_is_rejected(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": "forged"})
    assert r.status_code == 403


def test_send_with_no_tracking_is_refused_before_any_network_call(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    repo.insert_approval(db_path, pkg_id, "whatever", "vendor")
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 400
    assert "Cannot send" in r.text and "tracking" in r.text
    assert fake.calls_log == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_refuses_when_paypal_is_no_longer_asking_for_evidence(tmp_path, monkeypatch):
    """The fresh GET (not the one at approval time) shows no provide-evidence link - nothing is
    sent, the plain-language message is shown, and it is audited. provide_evidence is never called."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch, pre_send_get_body=NO_LINK)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert "PayPal is not asking for evidence on this case right now." in r.text
    assert "provide_evidence" not in fake.calls_log

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_blocked_no_link" for e in entries)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_refuses_a_package_edited_after_approval(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/claims", data={"text": "One more note.", "csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 400
    assert "not approved" in r.text
    assert "provide_evidence" not in fake.calls_log


# ---- A persistent send-status banner on the review
# screen itself, read from the attempt registry and the last GET - never from which button happens to be
# showing, so it survives closing the browser and coming back later.
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_review_screen_says_approved_before_any_send_attempt(tmp_path, monkeypatch):
    # An approved, not-yet-sent package reads "Approved" - "Not sent yet" is reserved for a
    # package with no valid approval at all (see test_create_package_defaults_to_manual_mode-style cases).
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Approved" in r.text
    assert fake.provide_evidence_calls == []   # merely viewing the review screen never sends anything


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_review_screen_shows_the_received_banner_after_a_confirmed_send(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch, post_send_get_bodies=[_confirmed_body()])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Received by PayPal on" in r.text
    assert "(Mexico City time)" in r.text      # The shell's display format (was "CST (UTC-6)", before that "CDMX")


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_review_screen_shows_the_pending_banner_when_still_unconfirmed(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch, post_send_get_bodies=[NOT_YET_BODY] * 3)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Send attempted - not confirmed by PayPal" in r.text and "Result pending" not in r.text   # the result page's title


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_review_screen_reverts_to_not_sent_yet_after_an_edit_following_a_confirmed_send(tmp_path, monkeypatch):
    """The banner is keyed to the CURRENT content's own hash (recomputed the same way send_package does),
    not just "this package id was sent at some point" - editing after a confirmed send must show the new
    content's real, honest status, never a stale "received" for content PayPal never actually saw."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch, post_send_get_bodies=[_confirmed_body()])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Received by PayPal on" in r.text
    token2 = r.cookies.get(CSRF_COOKIE_NAME)   # the GET above rotated the CSRF cookie

    # The confirmed version is read-only. The edit is refused (409) and changes nothing, so the page keeps
    # saying what PayPal really holds; a change is made on a NEW version, which starts as "Prepared".
    refused = c.post(f"/disputes/{DID}/packages/{pkg_id}/claims",
                     data={"csrf_token": token2, "text": "A new statement."}, follow_redirects=False)
    assert refused.status_code == 409, refused.text
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Received by PayPal on" in r.text and len(repo.list_claims(db_path, pkg_id)) == 0
    token3 = r.cookies.get(CSRF_COOKIE_NAME)
    created = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token3}, follow_redirects=False)
    new_id = int(created.headers["location"].rsplit("/", 1)[-1])
    r = c.get(f"/disputes/{DID}/packages/{new_id}")
    assert "Not sent yet" in r.text and "Received by PayPal on" not in r.text   # no stale "received" text


# ---- the real send path, end to end, against a scripted fake ----------------------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_succeeds_and_confirms_on_the_first_reconciliation_check(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch, post_send_get_bodies=[_confirmed_body()])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 200
    assert "Evidence sent to PayPal" in r.text
    assert len(fake.provide_evidence_calls) == 1
    multipart = fake.provide_evidence_calls[0]["multipart"]   # list of (field_name, value) tuples
    input_value = next(v for k, v in multipart if k == "input")
    sent_input = json.loads(input_value[1])
    assert sent_input["evidences"][0]["evidence_info"]["tracking_info"][0]["tracking_number"] == TRACKING_NUMBER

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_confirmed" for e in entries)
    assert any(e["action"] == "send_attempt_registered" for e in entries)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_result_pending_after_three_unconfirmed_checks_never_resends(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      post_send_get_bodies=[NOT_YET_BODY, NOT_YET_BODY, NOT_YET_BODY])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    outcomes, real_send = [], send_service.send_package

    def spy(*args, **kwargs):
        outcomes.append(real_send(*args, **kwargs))
        return outcomes[-1]
    monkeypatch.setattr(send_service, "send_package", spy)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 202
    assert "Send attempted - not confirmed by PayPal" in r.text   # The uncertain headline
    assert len(fake.provide_evidence_calls) == 1   # exactly once - reconciliation never re-POSTs while polling
    assert fake.calls_log.count("post_send_get") == 3
    assert len(outcomes) == 1 and outcomes[0].http_status == 202
    assert outcomes[0].message == ("The request was sent, but three checks (~5s, ~15s, ~30s apart) did not yet show it in "
                                   "this dispute's record. PayPal may take a moment to show it - check again in a bit; "
                                   "nothing will be sent again automatically.")
    assert "replication" not in outcomes[0].message.lower()

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_result_pending" for e in entries)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_handles_a_paypal_error_during_the_post_cleanly(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      provide_evidence_error=PayPalError(422, "DISPUTE_ALREADY_RESOLVED",
                                                                         "dbg-1", {}))
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 502
    assert "422" in r.text and "DISPUTE_ALREADY_RESOLVED" in r.text
    assert "post_send_get" not in fake.calls_log   # a clean rejection never reconciles - nothing was sent

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_failed" for e in entries)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_handles_a_transport_error_during_the_post_as_genuinely_ambiguous_then_reconciles(tmp_path, monkeypatch):
    """A transport error (no response at all) never means 'failed' - it falls through
    to the same reconciliation a clean 2xx would, and if PayPal actually received it anyway, that is
    what gets reported - never an automatic resend either way."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch, provide_evidence_transport_error=True,
                                      post_send_get_bodies=[_confirmed_body()])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 200
    assert "Evidence sent to PayPal" in r.text
    assert len(fake.provide_evidence_calls) == 1   # never retried the POST itself

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_outcome_unknown" for e in entries)
    assert any(e["action"] == "send_confirmed" for e in entries)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_handles_a_retryable_5xx_during_the_post_as_ambiguous_not_failed(tmp_path, monkeypatch):
    """A 500/502/503/504/429 is ambiguous (PayPal may have processed it despite the
    error response) - treated like a timeout, reconciled by GET, never marked 'failed' (which would wrongly
    invite an immediate, safe-looking retry-POST of a request that might already have landed)."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      provide_evidence_error=PayPalError(503, "INTERNAL_SERVICE_ERROR",
                                                                         "dbg-3", {}),
                                      post_send_get_bodies=[NOT_YET_BODY, NOT_YET_BODY, NOT_YET_BODY])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 202
    assert "Send attempted - not confirmed by PayPal" in r.text   # The uncertain headline
    assert len(fake.provide_evidence_calls) == 1   # never retried the POST itself

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_outcome_unknown" and e["detail"]["http_status"] == 503 for e in entries)
    assert not any(e["action"] == "send_failed" for e in entries)

    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, package_sha)["state"] == "sent_unconfirmed"   # ambiguous, not 'failed'
    store.close()


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_handles_errdis040_503_without_posting_a_second_time_on_the_next_click(tmp_path, monkeypatch):
    """The exact scenario named in review: a simulated ERRDIS040 (503) during the
    first click. A second click (vendor pressing "Check readiness to send" again) must reconcile from the
    attempt already on file - not POST again - while the outcome is still ambiguous."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      provide_evidence_error=PayPalError(503, "ERRDIS040", "dbg-4", {}),
                                      post_send_get_bodies=[NOT_YET_BODY, NOT_YET_BODY, NOT_YET_BODY])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r1 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r1.status_code == 202
    assert len(fake.provide_evidence_calls) == 1

    r2 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r2.status_code == 409
    assert "already in progress" in r2.text
    assert len(fake.provide_evidence_calls) == 1   # still exactly one real POST


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_second_send_after_confirmation_never_posts_again(tmp_path, monkeypatch):
    """Idempotent re-entry: once an attempt is confirmed, re-submitting the exact same package reconciles
    from PayPal's GET and does not POST a second time."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch, pre_send_get_body=WAITING,
                                      post_send_get_bodies=[_confirmed_body()])
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r1 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r1.status_code == 200
    assert len(fake.provide_evidence_calls) == 1

    # Second attempt: the fresh pre_send_get now shows the content already submitted (as the real sandbox
    # would after it actually landed) - reconciled, not re-sent.
    fake.pre_send_get_body = _confirmed_body()
    r2 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r2.status_code == 409          # A confirmed package is answered from the registry
    assert "Package already received" in r2.text and "Nothing new was sent." in r2.text
    assert len(fake.provide_evidence_calls) == 1   # still just the one real POST


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_concurrent_claim_for_the_same_package_is_refused_not_double_sent(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    claims = repo.list_claims(db_path, pkg_id)
    extracted_docs = extracted_docs_for(db_path, DID)
    tn, carrier = pick_tracking(extracted_docs)
    req_input = build_request_input(claims, tn, carrier)
    package_sha = sha256_json(req_input)
    store = SQLiteAttemptStore(db_path)
    assert store.claim(DID, package_sha) is True   # simulates another in-flight request claiming it first
    store.close()

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert "already in progress" in r.text
    assert 'data-state="result-uncertain"' in r.text and "banner-warn" in r.text
    assert 'data-state="confirmed"' not in r.text
    assert fake.provide_evidence_calls == []


# ---- startup/crash recovery reached through the send route itself -----------------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_reconciles_a_stale_sent_unconfirmed_attempt_for_a_different_package_first(tmp_path, monkeypatch):
    """Entering send_package first reconciles any OTHER attempt left 'sent_unconfirmed' for this
    dispute (e.g. a crash before this process restarted) before doing anything about the current one."""
    stale_req_input = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                      "evidence_info": {"tracking_info": [{"tracking_number": "OLD-TRACKING"}]},
                                      "notes": "old"}]}
    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      recovery_get_body=_confirmed_body("OLD-TRACKING", notes="old"),
                                      post_send_get_bodies=[_confirmed_body()])
    from copilot.core.attempt_store import SQLiteAttemptStore
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "stale-sha")
    # A genuine prior attempt by this app (request_id set) - otherwise recover_dispute() correctly
    # refuses to confirm it from content alone, and the in-flight check would then (correctly) block the new send below.
    store.write(DID, "stale-sha", {"state": "sent_unconfirmed", "request_id": "req-stale",
                                   "request_input": stale_req_input})
    store.close()

    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 200

    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, "stale-sha")["state"] == "confirmed"   # recovered along the way
    store.close()


# ---- a real double-click (two near-simultaneous requests) must only ever POST once ---------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_two_near_simultaneous_sends_of_the_same_package_only_post_once(tmp_path, monkeypatch):
    """Found while building this: a read-existing-attempt-before-claiming order (which pilot's own send()
    also uses - see pilot/test_step5_send.py::test_concurrent_sends_only_post_once, occasionally flaky for
    exactly this reason) has a TOCTOU gap where two racing callers can both see 'no attempt yet' and both
    POST. copilot/web/routes_send.py claims FIRST (atomic INSERT) and only inspects an existing row on
    failure, closing that gap; this test exercises it with two real threads, not a pre-seeded row."""
    class SlowFakeClient(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "provide_evidence":
                time.sleep(0.05)   # widen the race window around the real send
            return super().call(method, path, op, **kw)

    db_path = config.db_path()
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    app = create_app()
    fake = SlowFakeClient(post_send_get_bodies=[_confirmed_body()] * 2)
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    results = []

    def worker():
        results.append(c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token}))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake.provide_evidence_calls) == 1, "exactly one real POST must have happened"
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 200] or codes == [200, 409], codes   # winner confirms; loser reconciles or backs off


# ---- a 'pending' row orphaned by a crash between claim() and the POST does not block forever -----
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_stale_pending_row_is_reclaimed_and_sends_exactly_once(tmp_path, monkeypatch):
    """Simulates a process that died right after claim() (state='pending') but before ever reaching the
    POST - no network call happened, so nothing was sent. A fresh send must reclaim it once it is old
    enough to no longer look like a concurrent request, and must still POST exactly once."""
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for

    c, db_path, token, fake = _client(tmp_path, monkeypatch, post_send_get_bodies=[_confirmed_body()])
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
             ).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "pending", "ts": old_ts})
    store.close()

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 200
    assert "Evidence sent to PayPal" in r.text
    assert len(fake.provide_evidence_calls) == 1

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_attempt_reclaimed_stale_pending" for e in entries)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_fresh_pending_row_is_not_reclaimed_even_if_it_looks_abandoned(tmp_path, monkeypatch):
    """The mirror case: a 'pending' row only a few seconds old must NOT be reclaimed - it could be a
    genuinely concurrent request about to POST any moment."""
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for

    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    recent_ts = (datetime.now(tz.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "pending", "ts": recent_ts})
    store.close()

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert fake.provide_evidence_calls == []


# ---- an attached document actually rides along in the real multipart POST ------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_an_attached_document_is_sent_as_a_real_evidence_file_part(tmp_path, monkeypatch):
    from copilot.core.send import EVIDENCE_FILE_FIELD

    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      post_send_get_bodies=[_confirmed_body(documents=[{"name": "label.pdf"}])])
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", TRACKING_PDF, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    doc_id = repo.list_documents(db_path, DID)[0]["id"]

    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "sufficiency_override": "Tracking + attachment, ready to send."},
              follow_redirects=False)
    assert r.status_code == 303, r.text

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 200
    assert "Evidence sent to PayPal" in r.text

    multipart = fake.provide_evidence_calls[0]["multipart"]
    file_parts = [v for k, v in multipart if k == EVIDENCE_FILE_FIELD]
    assert len(file_parts) == 1
    filename, data, content_type = file_parts[0]
    assert filename == "label.pdf" and content_type == "application/pdf" and data == TRACKING_PDF


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_an_attached_document_that_paypal_does_not_list_back_is_reported_as_partial_not_received(tmp_path, monkeypatch):
    """The exact safety net: the shipment details land (tracking matches) but PayPal's documents[]
    never lists the file - exactly what a wrong multipart field name would silently produce. Must never
    report RECEIVED_MESSAGE, and must never re-POST."""
    from copilot.core.send_recovery import PARTIAL_MESSAGE

    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      post_send_get_bodies=[_confirmed_body(documents=[])])   # no file listed
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", TRACKING_PDF, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    doc_id = repo.list_documents(db_path, DID)[0]["id"]

    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token},
          follow_redirects=False)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "sufficiency_override": "Tracking + attachment, ready to send."},
              follow_redirects=False)
    assert r.status_code == 303, r.text

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 206
    assert PARTIAL_MESSAGE in r.text
    assert "Received by PayPal" not in r.text
    assert len(fake.provide_evidence_calls) == 1   # sent once, never retried on account of the missing file
    assert "label.pdf" in r.text   # the missing filename is visible, in the technical details

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "send_missing_attachment" and e["detail"]["missing_files"] == ["label.pdf"]
              for e in entries)

    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    store = SQLiteAttemptStore(db_path)
    rec = store.read(DID, package_sha)
    assert rec["state"] == "sent_unconfirmed"   # not 'confirmed' - the attachment never landed
    assert rec["reason"] == "missing_attachment:label.pdf"
    store.close()


# ---- the claim-conflict branch must never DOWNGRADE an already-settled row (confirmed
# or abandoned) back to the ambiguous 'sent_unconfirmed'/partial state, even if THIS call's own pre-send
# GET happens to show files_ok=False for it (a differently-timed/flaky read of the same content) ----------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_claim_conflict_never_downgrades_an_already_confirmed_row_to_partial(tmp_path, monkeypatch):
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for

    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      pre_send_get_body=_confirmed_body(documents=[]))   # no file listed
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", TRACKING_PDF, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token},
          follow_redirects=False)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
          data={"csrf_token": token, "sufficiency_override": "ready"}, follow_redirects=False)

    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    req_input = build_request_input(claims, tn, carrier)
    package_sha = sha256_json(req_input)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "confirmed", "request_id": "req-earlier",
                                   "request_input": req_input, "attachment_filenames": ["label.pdf"]})
    store.close()

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409           # The confirmed row answers before any GET or POST
    assert "Package already received" in r.text
    assert fake.provide_evidence_calls == []   # never POSTs on an already-settled row

    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, package_sha)["state"] == "confirmed"   # never downgraded
    store.close()


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_claim_conflict_never_downgrades_an_abandoned_row_to_partial(tmp_path, monkeypatch):
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for

    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      pre_send_get_body=_confirmed_body(documents=[]))
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", TRACKING_PDF, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token},
          follow_redirects=False)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
          data={"csrf_token": token, "sufficiency_override": "ready"}, follow_redirects=False)

    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    req_input = build_request_input(claims, tn, carrier)
    package_sha = sha256_json(req_input)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "abandoned", "request_id": "req-earlier",
                                   "request_input": req_input, "attachment_filenames": ["label.pdf"]})
    store.close()

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    # the read lists seller evidence (a confirmed body), so the refusal says the rule, not "edit it"
    assert "marked as not received" in r.text and "Proofie sends evidence once per dispute" in r.text
    assert "edit it" not in r.text and fake.provide_evidence_calls == []

    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, package_sha)["state"] == "abandoned"   # never downgraded
    store.close()


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_second_send_attempt_after_a_missing_attachment_does_not_post_again(tmp_path, monkeypatch):
    """A vendor clicking send again right after seeing PARTIAL_MESSAGE must not trigger a second real
    POST - the attempt is already on file as 'sent_unconfirmed' and still looks the same on a fresh check."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      post_send_get_bodies=[_confirmed_body(documents=[])])
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", TRACKING_PDF, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token},
          follow_redirects=False)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
          data={"csrf_token": token, "sufficiency_override": "ready"}, follow_redirects=False)

    r1 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r1.status_code == 206
    assert len(fake.provide_evidence_calls) == 1

    r2 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r2.status_code == 409
    assert len(fake.provide_evidence_calls) == 1   # still exactly one real POST


# ============================================================================================ review findings
# Findings of a static review of the send path.
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_cannot_send_a_package_while_a_fresh_pending_one_is_still_in_flight_for_the_same_dispute(
        tmp_path, monkeypatch):
    """A FRESH 'pending' attempt for a DIFFERENT package_sha in the same dispute must block a new
    send outright - two sends in flight for one dispute is exactly the duplicate-evidence risk the send guard
    exists to prevent, even across two different packages."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "some-other-packages-sha")   # simulates another package's send still in flight
    store.close()

    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert "Send attempted - not confirmed by PayPal" in r.text   # The uncertain headline
    assert fake.provide_evidence_calls == []
    assert "provide_evidence" not in fake.calls_log


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_stale_other_pending_never_blocks_and_is_marked_superseded(tmp_path, monkeypatch):
    """A 'pending' attempt for a DIFFERENT package, old enough that it never reached a POST, must
    never block - it is neutralized to 'blocked'/"superseded, never sent" and audited."""
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore
    c, db_path, token, fake = _client(tmp_path, monkeypatch, post_send_get_bodies=[_confirmed_body()])
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
             ).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "some-other-packages-sha")
    store.write(DID, "some-other-packages-sha", {"state": "pending", "ts": old_ts})
    store.close()

    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 200
    assert len(fake.provide_evidence_calls) == 1

    store = SQLiteAttemptStore(db_path)
    other = store.read(DID, "some-other-packages-sha")
    store.close()
    assert other["state"] == "blocked" and other["reason"] == send_service.SUPERSEDED_REASON
    assert any(e["action"] == "send_attempt_superseded" for e in repo.list_audit(db_path, DID))


def test_other_attempts_block_blocks_for_real_when_supersede_fails_because_the_row_actually_sent(
        tmp_path):
    """supersede()'s guarded UPDATE can fail because the stale-looking row's OWN POST
    actually went out (now 'sent_unconfirmed') in the exact gap between list_by_dispute() and the
    supersede() attempt on it - must then genuinely block (409), never silently continue as if it had
    been neutralized."""
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore

    db_path = tmp_path / "copilot.db"
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
             ).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "other-sha")
    store.write(DID, "other-sha", {"state": "pending", "ts": old_ts})
    store.close()

    class RaceStore(SQLiteAttemptStore):
        def list_by_dispute(self, dispute_id):
            rows = super().list_by_dispute(dispute_id)
            # Simulates "other-sha"'s own POST actually landing (claim -> POST -> sent_unconfirmed) in
            # the narrow gap between this list and the supersede() attempt on it, right below.
            super().write(dispute_id, "other-sha", {"state": "sent_unconfirmed", "request_id": "req-x"})
            return rows

    race_store = RaceStore(db_path)
    blocked = send_service._other_attempts_block(race_store, db_path, DID, 1, "my-own-sha")
    assert blocked is not None and blocked.http_status == 409
    assert "may have actually reached PayPal" in blocked.message
    assert race_store.read(DID, "other-sha")["state"] == "sent_unconfirmed"   # never superseded


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_sent_unconfirmed_other_attempt_blocks_regardless_of_age(tmp_path, monkeypatch):
    """Unlike a stale 'pending' row, a 'sent_unconfirmed' attempt for a DIFFERENT package is
    NEVER silently neutralized by age alone - it may have actually reached PayPal."""
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    old_ts = (datetime.now(tz.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "some-other-packages-sha")
    store.write(DID, "some-other-packages-sha", {"state": "sent_unconfirmed", "request_id": "req-x",
                                                  "ts": old_ts})
    store.close()

    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert "Check again" in r.text
    assert fake.provide_evidence_calls == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_confirm_not_received_without_the_checkbox_is_refused(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received", data={"csrf_token": token})
    assert r.status_code == 400
    assert "must check the box" in r.text


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_confirm_not_received_marks_abandoned_and_unblocks_the_send(tmp_path, monkeypatch):
    """The explicit human action moves the OTHER package's 'sent_unconfirmed' attempt to
    'abandoned' (never 'confirmed'/'failed' - PayPal's own truth about it stays unknown), audited as
    `abandoned_by_vendor`, and only THEN does this package's own send proceed."""
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore
    c, db_path, token, fake = _client(tmp_path, monkeypatch, post_send_get_bodies=[_confirmed_body()])
    # Old enough that it is not plausibly a POST still in flight.
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
             ).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "some-other-packages-sha")
    store.write(DID, "some-other-packages-sha",
               {"state": "sent_unconfirmed", "request_id": "req-x", "ts": old_ts})
    store.close()
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r1 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r1.status_code == 409   # still blocked before confirming

    r2 = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received",
               data={"csrf_token": token, "confirmed": "on"})
    assert r2.status_code == 200
    assert "1 earlier unconfirmed attempt" in r2.text

    store = SQLiteAttemptStore(db_path)
    other = store.read(DID, "some-other-packages-sha")
    store.close()
    assert other["state"] == "abandoned" and other["reason"] == send_service.ABANDONED_REASON
    entry = next(e for e in repo.list_audit(db_path, DID) if e["action"] == "abandoned_by_vendor")
    assert entry["actor"] == "vendor"   # a human action, never actor='system'

    r3 = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r3.status_code == 200   # no longer blocked
    assert len(fake.provide_evidence_calls) == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_confirm_not_received_with_nothing_waiting_says_so(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received",
              data={"csrf_token": token, "confirmed": "on"})
    assert r.status_code == 200
    assert "Nothing was waiting" in r.text


# ---- confirm_other_attempts_abandoned must never abandon a POST that may still be
# genuinely in flight, and PayPal's own later truth must always win -----------------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_confirm_not_received_refuses_a_recent_sent_unconfirmed_attempt(tmp_path, monkeypatch):
    """Item 1(b): a 'sent_unconfirmed' row written moments ago is indistinguishable from a POST that has
    not even returned yet - must be left untouched, with a message telling the vendor to wait."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "some-other-packages-sha")
    store.write(DID, "some-other-packages-sha", {"state": "sent_unconfirmed", "request_id": "req-x"})
    store.close()
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received",
              data={"csrf_token": token, "confirmed": "on"})
    assert r.status_code == 200
    assert send_service.ABANDON_TOO_RECENT_MESSAGE in r.text

    store = SQLiteAttemptStore(db_path)
    other = store.read(DID, "some-other-packages-sha")
    store.close()
    assert other["state"] == "sent_unconfirmed"   # untouched


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_confirm_not_received_reconciles_first_and_never_touches_an_attempt_paypal_already_confirmed(
        tmp_path, monkeypatch):
    """Item 1(a): recover_dispute runs FIRST - if the other attempt actually landed (PayPal's GET now
    shows it), it is confirmed, never abandoned."""
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, sha256_json
    other_req_input = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                      "evidence_info": {"tracking_info": [{"tracking_number": "OLD-TRK"}]},
                                      "notes": "old"}]}
    c, db_path, token, fake = _client(tmp_path, monkeypatch,
                                      recovery_get_body=_confirmed_body("OLD-TRK", notes="old"))
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
             ).strftime("%Y-%m-%dT%H:%M:%S%z")
    other_sha = sha256_json(other_req_input)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, other_sha)
    store.write(DID, other_sha, {"state": "sent_unconfirmed", "request_id": "req-x",
                                 "request_input": other_req_input, "ts": old_ts})
    store.close()
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received",
              data={"csrf_token": token, "confirmed": "on"})
    assert r.status_code == 200
    assert "Nothing was waiting" in r.text   # it was confirmed by recover_dispute, not "abandoned"

    store = SQLiteAttemptStore(db_path)
    other = store.read(DID, other_sha)
    store.close()
    assert other["state"] == "confirmed"
    assert not any(e["action"] == "abandoned_by_vendor" for e in repo.list_audit(db_path, DID))


def test_a_late_landing_post_on_an_abandoned_attempt_is_confirmed_and_traced(tmp_path, monkeypatch):
    """Item 1(d): once a row is 'abandoned', a later recover_dispute (e.g. an ordinary visit to the
    dispute page) that finds PayPal's own GET now showing it promotes it to 'confirmed' and audits the
    fact that it was abandoned first - PayPal's truth always wins, but the history is kept."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    from copilot.core.send_recovery import recover_dispute
    abandoned_req_input = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                          "evidence_info": {"tracking_info": [{"tracking_number": "LATE"}]},
                                          "notes": "late"}]}
    db_path = config.db_path()
    abandoned_sha = sha256_json(abandoned_req_input)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, abandoned_sha)
    store.write(DID, abandoned_sha, {"state": "abandoned", "request_id": "req-late",
                                     "request_input": abandoned_req_input})
    store.close()

    fake = FakeClient(recovery_get_body=_confirmed_body("LATE", notes="late"))
    store = SQLiteAttemptStore(db_path)
    try:
        still_pending = recover_dispute(fake, store, DID, db_path)
    finally:
        store.close()
    assert still_pending == []   # an abandoned row is never reported as a pending banner either way

    store = SQLiteAttemptStore(db_path)
    rec = store.read(DID, abandoned_sha)
    store.close()
    assert rec["state"] == "confirmed"
    assert fake.provide_evidence_calls == []   # zero POST, ever - only GETs reconciled
    assert any(e["action"] == "abandoned_attempt_later_confirmed_by_paypal" for e in
              repo.list_audit(db_path, DID))


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_sending_a_package_whose_own_attempt_was_abandoned_says_edit_it_not_wait(tmp_path, monkeypatch):
    """Item 6: this package's OWN content was itself marked abandoned by an earlier confirm-not-received
    action - "wait and check again" would be misleading (nothing is in flight); the real fix is editing
    the package (a new version, a new content hash)."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "abandoned", "request_id": "req-x"})
    store.close()

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert "edit it" in r.text and "new version" in r.text
    assert "wait a moment" not in r.text.lower()
    assert fake.provide_evidence_calls == []


# ---- package_id must belong to dispute_id ------------------------------------------------
def test_confirm_not_received_404s_when_the_package_belongs_to_a_different_dispute(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    other_pkg_id = repo.insert_package(db_path, "PP-R-OTHER-0000", source="human_edit", validation_ok=False,
                                       review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{other_pkg_id}/confirm-not-received",
              data={"csrf_token": token, "confirmed": "on"})
    assert r.status_code == 404


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_third_concurrent_send_discovered_right_after_claiming_blocks_before_any_post(tmp_path,
                                                                                        monkeypatch):
    """A caller could win its OWN claim and then discover, an instant later, that some OTHER
    package's send started in the gap between the first in-flight check and this claim succeeding (different
    package_sha, so claim() itself never serialised against it). Simulated deterministically: the OTHER
    attempt is inserted as a side effect of the fresh pre-send GET, landing exactly between this request's
    first check (already passed, nothing blocking yet) and its post-claim re-check."""
    from copilot.core.attempt_store import SQLiteAttemptStore

    class InjectingClient(FakeClient):
        def __init__(self, db_path, **kw):
            super().__init__(**kw)
            self._db_path = db_path
            self._injected = False

        def call(self, method, path, op, **kw):
            if op == "pre_send_get" and not self._injected:
                self._injected = True
                store = SQLiteAttemptStore(self._db_path)
                store.claim(DID, "a-third-callers-sha")
                store.close()
            return super().call(method, path, op, **kw)

    db_path = config.db_path()
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    app = create_app()
    fake = InjectingClient(db_path)
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert fake.provide_evidence_calls == []
    assert "provide_evidence" not in fake.calls_log

    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    store = SQLiteAttemptStore(db_path)
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    our_sha = sha256_json(build_request_input(claims, tn, carrier))
    ours = store.read(DID, our_sha)
    store.close()
    assert ours["state"] == "blocked"   # claimed, then unwound - never left dangling as 'pending'


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_two_threads_racing_to_reclaim_a_stale_failed_row_post_exactly_once(tmp_path, monkeypatch):
    """AttemptStore.reclaim() guards its UPDATE on the exact (state, ts) the caller read, so two
    threads racing to reclaim the SAME stale 'failed' row can never both win - only the first's UPDATE can
    match; by the time the second's WHERE runs, the row's ts has already moved."""
    class SlowFakeClient(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "provide_evidence":
                time.sleep(0.05)   # widen the race window around the real send
            return super().call(method, path, op, **kw)

    db_path = config.db_path()
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    app = create_app()
    fake = SlowFakeClient(post_send_get_bodies=[_confirmed_body()] * 2)
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "failed", "reason": "PayPal rejected it"})
    store.close()

    results = []

    def worker():
        results.append(c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token}))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake.provide_evidence_calls) == 1   # exactly one of the two actually POSTed
    assert sorted(r.status_code for r in results) == [200, 409]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_two_threads_racing_to_reclaim_a_stale_pending_row_post_exactly_once(tmp_path, monkeypatch):
    """Same guarantee as the 'failed' case above, starting from a stale 'pending' row (claimed but
    the process died before ever reaching the POST)."""
    from datetime import datetime, timedelta, timezone as tz

    class SlowFakeClient(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "provide_evidence":
                time.sleep(0.05)
            return super().call(method, path, op, **kw)

    db_path = config.db_path()
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    app = create_app()
    fake = SlowFakeClient(post_send_get_bodies=[_confirmed_body()] * 2)
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
             ).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "pending", "ts": old_ts})
    store.close()

    results = []

    def worker():
        results.append(c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token}))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake.provide_evidence_calls) == 1
    assert sorted(r.status_code for r in results) == [200, 409]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_claim_failing_without_a_row_refuses_cleanly_not_a_500(tmp_path, monkeypatch):
    """claim() can return False from lock contention (sqlite3.OperationalError), not only from an
    existing row (IntegrityError) - store.read() then finds nothing at all. Must refuse with a plain 409,
    never crash on existing["state"] with a TypeError that would otherwise surface as a 500."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    monkeypatch.setattr(SQLiteAttemptStore, "claim", lambda self, dispute_id, package_sha: False)
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert "Could not register the attempt, try again." in r.text
    assert fake.provide_evidence_calls == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_responds_503_when_payp_is_unreachable_before_sending(tmp_path, monkeypatch):
    """A transport error on the fresh pre-send GET must never crash through to the POST below - 503,
    nothing claimed, nothing sent."""
    class UnreachableClient(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "pre_send_get":
                raise httpx.ConnectError("fake: connection refused")
            return super().call(method, path, op, **kw)

    db_path = config.db_path()
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    app = create_app()
    fake = UnreachableClient()
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 503
    assert "PayPal could not be reached, nothing was sent." in r.text
    assert fake.provide_evidence_calls == []
    assert "provide_evidence" not in fake.calls_log


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_responds_503_when_paypal_returns_an_error_on_the_pre_send_get(tmp_path, monkeypatch):
    """The PayPalError variant (e.g. a transient 503 from PayPal itself on the pre-send GET)."""
    class ErroringClient(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "pre_send_get":
                raise PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-1", {})
            return super().call(method, path, op, **kw)

    db_path = config.db_path()
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    app = create_app()
    fake = ErroringClient()
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 503
    assert fake.provide_evidence_calls == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_reconciliation_survives_a_get_error_during_polling(tmp_path, monkeypatch):
    """A GET failure DURING the post-POST reconciliation polling must never crash - it is treated
    exactly like 'not shown yet' and the next delay is tried; exhausting all three (every one erroring)
    still ends in the normal ambiguous 202, never an unhandled exception, and never a second POST."""
    class FlakyDuringReconcile(FakeClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.post_send_get_calls = 0

        def call(self, method, path, op, **kw):
            if op == "post_send_get":
                self.post_send_get_calls += 1
                raise httpx.ReadTimeout("fake: timed out")
            return super().call(method, path, op, **kw)

    db_path = config.db_path()
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    app = create_app()
    fake = FlakyDuringReconcile()
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 202
    assert "Send attempted - not confirmed by PayPal" in r.text   # The uncertain headline
    assert fake.post_send_get_calls == 3   # all three delays attempted, each one erroring
    assert len(fake.provide_evidence_calls) == 1   # exactly one POST, never retried


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_local_block_shows_not_sent_never_rejected_by_paypal(tmp_path, monkeypatch):
    """A LOCAL refusal (no provide-evidence link yet) must read as "Not sent: <reason>" wherever
    it is shown again later - never as PayPal rejecting it. 'failed' stays reserved for a real 4xx."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch, pre_send_get_body=NO_LINK)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 409
    assert "provide_evidence" not in fake.calls_log

    r2 = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Not sent: PayPal is not asking for evidence on this case right now." in r2.text
    assert "rejected by PayPal" not in r2.text
    assert "Failed" not in r2.text

    # the underlying registry row really is 'blocked', not just the rendered text.
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    store = SQLiteAttemptStore(db_path)
    rec = store.read(DID, package_sha)
    store.close()
    assert rec["state"] == "blocked"


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_banner_shows_abandoned_and_the_registry_row_really_is_abandoned(tmp_path, monkeypatch):
    """send_status_banner's 'abandoned' branch, checking the registry state too."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    package_sha = sha256_json(build_request_input(claims, tn, carrier))
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, package_sha)
    store.write(DID, package_sha, {"state": "abandoned", "request_id": "req-x"})
    store.close()

    package = repo.get_package(db_path, pkg_id)
    status = send_service.send_status_banner(db_path, DID, package, claims, approved=True)
    assert "Not sent" in status and "abandoned" in status.lower()

    store = SQLiteAttemptStore(db_path)
    rec = store.read(DID, package_sha)
    store.close()
    assert rec["state"] == "abandoned"   # reading the banner never mutates the registry


def test_page_helper_escapes_all_interpolated_text():
    """_page/_back interpolate PayPal- and exception-derived text with no Jinja autoescape - every
    piece (title, message, technical, dispute_id) must be escaped before reaching the response body."""
    from copilot.web import routes_send
    resp = routes_send._page("<b>Title</b>", "<script>evil</script>", "<i>bad-id</i>", 1, 200,
                             technical="<img src=x onerror=alert(1)>")
    text = resp.body.decode()
    assert "<script>" not in text and "<b>" not in text and "<img" not in text and "<i>" not in text
    assert "&lt;script&gt;" in text and "&lt;b&gt;" in text and "&lt;i&gt;" in text


def test_a_malformed_dispute_id_is_rejected_before_any_network_call(tmp_path, monkeypatch):
    """dispute_id is an untrusted URL path segment - validated against the same expression as the
    MCP server (copilot/web/security.py's DISPUTE_ID_RE) before it ever reaches a PayPal call or the HTML
    response, not merely escaped after the fact."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    bad_id = "not-a-real-paypal-id"   # wrong shape (must start "PP-") - no "<", ">" or "/" so the URL
                                      # itself is unambiguous; a raw "<script>" would just need escaping,
                                      # already covered directly by test_page_helper_escapes_all_interpolated_text
    r = c.post(f"/disputes/{bad_id}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 400
    assert "That dispute reference is not valid." in r.text
    assert fake.calls_log == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_only_gets_never_posts(tmp_path, monkeypatch):
    """'Check again' re-verifies an existing attempt via GET only and updates the banner - it must
    never call provide_evidence, under any circumstance."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch, recovery_get_body=_confirmed_body())
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    claims = repo.list_claims(db_path, pkg_id)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    req_input = build_request_input(claims, tn, carrier)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, sha256_json(req_input))
    store.write(DID, sha256_json(req_input), {"state": "sent_unconfirmed", "request_id": "req-1",
                                              "request_input": req_input})
    store.close()

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    assert r.status_code == 200
    assert "Received by PayPal on" in r.text
    # The display format in Mexico City time, never the old 'CST (UTC-6)' / 'CDMX' stamp
    assert re.search(r"Received by PayPal on \d{1,2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d{2} (?:am|pm) \(Mexico City time\)", r.text)
    assert "CST (UTC-6)" not in r.text and " CDMX" not in r.text
    assert fake.provide_evidence_calls == []
    assert "provide_evidence" not in fake.calls_log
    assert fake.calls_log.count("recovery_get") == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_with_nothing_sent_yet_just_shows_current_status(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    assert r.status_code == 200
    assert "Approved" in r.text
    assert fake.provide_evidence_calls == []


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.parametrize("outcome", ["absent", "partial", "failed"])
def test_check_again_readback_outcomes_are_read_only(tmp_path, monkeypatch, outcome):
    """The explicit Check route reports absent, partial and failed reads without inferring a resend."""
    if outcome == "absent":
        fake_options = {"recovery_get_body": NOT_YET_BODY}
    elif outcome == "partial":
        fake_options = {"recovery_get_body": _confirmed_body(documents=[])}
    else:
        fake_options = {"recovery_get_error": httpx.ReadTimeout("synthetic read timeout")}
    c, db_path, token, fake = _client(tmp_path, monkeypatch, **fake_options)
    pkg_id = _seed_unconfirmed_check_attempt(c, db_path, token,
        attach_file=outcome == "partial", partial=outcome == "partial")
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    if outcome == "absent":
        assert r.status_code == 200 and "Still not confirmed by PayPal" in r.text
    elif outcome == "partial":
        assert r.status_code == 200 and "received the details but not the attached file" in r.text
    else:
        assert r.status_code == 502 and "Proofie could not check PayPal just now" in r.text
    assert fake.provide_evidence_calls == []
    assert "provide_evidence" not in fake.calls_log
    assert fake.calls_log.count("recovery_get") == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_without_csrf_is_rejected(tmp_path, monkeypatch):
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    calls_before = len(fake.calls_log)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": "forged"})
    assert r.status_code == 403
    assert fake.calls_log[calls_before:] == []   # the CSRF check happened before any network call


def test_check_again_rejects_a_malformed_dispute_id_before_any_network_call(tmp_path, monkeypatch):
    """The dispute_id validation applies to /check too, not only /send."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/not-a-real-paypal-id/packages/{pkg_id}/check", data={"csrf_token": token})
    assert r.status_code == 400
    assert "That dispute reference is not valid." in r.text
    assert fake.calls_log == []


def test_check_again_404s_when_the_package_belongs_to_a_different_dispute(tmp_path, monkeypatch):
    """check_package must verify package_id belongs to dispute_id too, same as
    confirm-not-received."""
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    other_pkg_id = repo.insert_package(db_path, "PP-R-OTHER-0000", source="human_edit", validation_ok=False,
                                       review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{other_pkg_id}/check", data={"csrf_token": token})
    assert r.status_code == 404


# ---- AttemptStore.abandon()'s own guard, at the store level --------------------------
def test_abandon_fails_when_the_ts_no_longer_matches(tmp_path):
    from copilot.core.attempt_store import SQLiteAttemptStore
    db_path = tmp_path / "copilot.db"
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "sha-1")
    store.write(DID, "sha-1", {"state": "sent_unconfirmed", "request_id": "req-1"})
    stale_ts = store.read(DID, "sha-1")["ts"]
    store.write(DID, "sha-1", {"state": "sent_unconfirmed", "request_id": "req-1",
                               "ts": "2099-01-01T00:00:00+0000"})
    assert store.abandon(DID, "sha-1", stale_ts, "some reason") is False
    assert store.read(DID, "sha-1")["state"] == "sent_unconfirmed"


def test_abandon_fails_when_the_state_is_not_sent_unconfirmed(tmp_path):
    from copilot.core.attempt_store import SQLiteAttemptStore
    db_path = tmp_path / "copilot.db"
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "sha-1")
    store.write(DID, "sha-1", {"state": "confirmed", "request_input": {"evidences": []}})
    ts = store.read(DID, "sha-1")["ts"]
    assert store.abandon(DID, "sha-1", ts, "some reason") is False
    assert store.read(DID, "sha-1")["state"] == "confirmed"


# ---- the race, at the service level - a row confirmed in the exact gap between ------
# confirm_other_attempts_abandoned()'s own list_by_dispute() and its abandon() call must never be abandoned
def test_confirm_other_attempts_abandoned_never_abandons_a_row_confirmed_in_the_gap(tmp_path):
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core.attempt_store import SQLiteAttemptStore

    db_path = tmp_path / "copilot.db"
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
             ).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "other-sha")
    store.write(DID, "other-sha", {"state": "sent_unconfirmed", "request_id": "req-x", "ts": old_ts})

    class RaceStore(SQLiteAttemptStore):
        def list_by_dispute(self, dispute_id):
            rows = super().list_by_dispute(dispute_id)
            # Simulates a concurrent confirmation (a real send, elsewhere, whose own GET just landed)
            # arriving in the narrow gap between this list and the abandon() call that follows it.
            super().write(dispute_id, "other-sha", {"state": "confirmed", "request_input": {"evidences": []}})
            return rows

    race_store = RaceStore(db_path)

    class NoOpClient:
        def call(self, method, path, op, **kw):
            raise AssertionError(f"unexpected call {op}")   # recover_dispute skips this row (no
                                                              # request_input), so no GET should happen

    result = send_service.confirm_other_attempts_abandoned(NoOpClient(), race_store, db_path, DID, "my-own-sha")
    assert result["abandoned"] == 0
    assert store.read(DID, "other-sha")["state"] == "confirmed"   # never downgraded to abandoned


# ---- confirm-not-received leaves every OTHER state untouched, and CSRF comes first ----
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_confirm_not_received_without_csrf_is_rejected_before_any_state_change(tmp_path, monkeypatch):
    from copilot.core.attempt_store import SQLiteAttemptStore
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "other-sha")
    store.write(DID, "other-sha", {"state": "sent_unconfirmed", "request_id": "req-x"})
    store.close()
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received",
              data={"csrf_token": "forged", "confirmed": "on"})
    assert r.status_code == 403

    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, "other-sha")["state"] == "sent_unconfirmed"
    store.close()


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.parametrize("other_state, extra", [
    ("pending", {}),
    ("failed", {"reason": "x"}),
    ("blocked", {"reason": "x"}),
    ("confirmed", {"request_input": {"evidences": []}}),
])
def test_confirm_not_received_never_touches_rows_that_are_not_sent_unconfirmed(tmp_path, monkeypatch,
                                                                                other_state, extra):
    """confirm_other_attempts_abandoned only ever targets 'sent_unconfirmed' rows -
    pending/failed/blocked/confirmed must come back completely untouched."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    c, db_path, token, fake = _client(tmp_path, monkeypatch)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "other-sha")
    store.write(DID, "other-sha", {"state": other_state, **extra})
    before = store.read(DID, "other-sha")
    store.close()
    pkg_id = _approved_package_with_tracking(c, db_path, token)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received",
              data={"csrf_token": token, "confirmed": "on"})
    assert r.status_code == 200

    store = SQLiteAttemptStore(db_path)
    after = store.read(DID, "other-sha")
    store.close()
    assert after == before   # byte-for-byte untouched


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf
