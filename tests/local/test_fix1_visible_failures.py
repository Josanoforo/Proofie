"""The seven failures a judge was most likely to meet silently or with a false message. Each test names
the assertion that fails without its fix. Offline: fake PayPal client, no model, no network."""
from _hosted import authenticated_client, reset_csrf
import json
import logging
import secrets
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pilot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dom import actions_in, parse, state  # noqa: E402
from copilot import config  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.core import auto_prep, document_ingest  # noqa: E402
from copilot.core.disputes import PayPalError  # noqa: E402
from copilot.db import repo  # noqa: E402
from copilot.web import routes_package  # noqa: E402
from copilot.web.routes_disputes import get_client  # noqa: E402
from copilot.web.security import CSRF_COOKIE_NAME  # noqa: E402

REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
OTHER = "PP-R-DEMO-0000002"


class FakeClient:
    """show_dispute answers WAITING or raises; the two context lookups answer empty (as in the other suites)."""
    def __init__(self, detail_raises=None):
        self.detail_raises = detail_raises

    def call(self, method, path, op, **kw):
        if op == "show_dispute":
            if self.detail_raises:
                raise self.detail_raises
            return WAITING
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _app(fake=None):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake or FakeClient()
    return app


def _client(app=None, **kw):
    c = authenticated_client(app or _app(), client=("127.0.0.1", 12345), **kw)
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return c, token


# ---- 1. OK-wrong: rejected credentials are not "could not be reached" ------------------------------------------
@pytest.mark.parametrize("status", [401, 403])
def test_item1_a_credentials_rejection_says_so_and_never_says_unreachable(status):
    secretish = "CLIENT-AUTH-FAILED-SECRETVALUE"
    exc = PayPalError(status, "invalid_client", "dbg-secret-id", {"error_description": secretish})
    c, _ = _client(_app(FakeClient(detail_raises=exc)))
    r = c.get(f"/disputes/{DID}")
    dom = parse(r.text)
    assert state(dom, "hosted-no-credentials" if config.hosted_demo() else "credentials-rejected") is not None
    if config.hosted_demo():
        assert "come back later" in r.text and ".env" not in r.text
    else:
        assert "Client ID and Secret" in r.text and ".env" in r.text
    assert "could not be reached" not in r.text
    assert state(dom, "paypal-unreachable") is None
    for leaked in (secretish, "invalid_client", "dbg-secret-id"):    # PayPal's own body never reaches the page
        assert leaked not in r.text


@pytest.mark.parametrize("status", [503, 429, 500])
def test_item1_other_failures_keep_the_could_not_be_reached_wording(status):
    c, _ = _client(_app(FakeClient(detail_raises=PayPalError(status, "X", "d", {}))))
    r = c.get(f"/disputes/{DID}")
    assert state(parse(r.text), "paypal-unreachable") is not None and "could not be reached" in r.text
    assert state(parse(r.text), "credentials-rejected") is None


def test_item1_a_404_stays_a_not_found_page():
    c, _ = _client(_app(FakeClient(detail_raises=PayPalError(404, "RESOURCE_NOT_FOUND", "d", {}))))
    r = c.get(f"/disputes/{DID}")
    assert r.status_code == 404 and state(parse(r.text), "not-found") is not None


def test_item1_a_rejection_on_a_post_does_not_claim_nothing_was_changed(caplog):
    app = _app()

    @app.post("/__reject")
    async def reject():
        raise PayPalError(401, "invalid_client", "d", {})

    c, _ = _client(app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger="copilot.app"):
        r = c.post("/__reject")
    assert state(parse(r.text), "hosted-no-credentials" if config.hosted_demo() else "credentials-rejected") is not None
    assert "could not confirm what happened" in r.text and "Nothing was changed" not in r.text
    assert any("HTTP 401" in rec.getMessage() for rec in caplog.records)       # the log line (fails without the fix)


# ---- 2. a generic handler with a safe log line -------------------------------------------------------------
def test_item2_an_unexpected_exception_renders_the_error_page_and_logs_class_only(caplog):
    app = _app()

    @app.post("/__boom")
    async def boom():
        raise OSError("disk full - SECRET-IN-EXCEPTION-TEXT")

    c, _ = _client(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="copilot.app"):
        r = c.post("/__boom", data={"note": "BODYSECRET-123"}, headers={"Authorization": "Bearer HEADERSECRET-456"})
    assert r.status_code == 500
    assert state(parse(r.text), "internal-error") is not None        # fails without the fix: Starlette's bare 500
    assert "Something went wrong inside Proofie" in r.text and 'href="/disputes"' in r.text
    assert r.text.strip() != "Internal Server Error"
    records = [rec for rec in caplog.records if rec.name == "copilot.app"]
    assert records, "the generic handler must write a log record"
    logged = caplog.text + "".join(rec.getMessage() for rec in records)
    assert "OSError" in logged
    for secret in ("BODYSECRET-123", "HEADERSECRET-456", "SECRET-IN-EXCEPTION-TEXT"):
        assert secret not in logged and secret not in r.text
    assert all(rec.exc_info is None for rec in records)               # no traceback with the exception text either


def test_item2_the_specific_handlers_keep_priority():
    c, _ = _client(_app(FakeClient(detail_raises=PayPalError(503, "X", "d", {}))), raise_server_exceptions=False)
    r = c.get(f"/disputes/{DID}")
    assert r.status_code == 502 and state(parse(r.text), "paypal-unreachable") is not None
    assert state(parse(r.text), "internal-error") is None


# ---- 3. an AI answer that failed validation is flagged in the main view ---------------------------------------
def _review(c, package_id, dispute_id=DID):
    return c.get(f"/disputes/{dispute_id}/packages/{package_id}")


def test_item3_an_unchecked_ai_package_shows_a_visible_notice():
    c, _ = _client()
    db = config.db_path()
    bad = repo.insert_package(db, DID, source="ai", validation_ok=False, review_flags=[])
    good = repo.insert_package(db, DID, source="ai", validation_ok=True, review_flags=[])
    manual = repo.insert_package(db, DID, source="human_edit", validation_ok=False, review_flags=[])
    page = parse(_review(c, bad).text)
    box = state(page, "ai-unchecked")
    assert box is not None                                            # fails without the fix: no such banner
    assert "The assistant's answer could not be checked" in box.text() and "Type the shipment details" in box.text()
    assert state(parse(_review(c, good).text), "ai-unchecked") is None
    assert state(parse(_review(c, manual).text), "ai-unchecked") is None   # a manual shell is not an AI answer


# ---- 4. an auto-prep shell made after an AI failure says the AI was not available -----------------------------
class _PrepClient:
    def call(self, method, path, op, **kw):
        return {"transaction_details": []} if op == "context_transaction_search" else {"trackers": []}


_DISPUTE_NORM = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
                 "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "update_time": "2026-10-05T00:00:00Z",
                 "create_time": "2026-10-01T00:00:00Z", "seller_transaction_ids": ["TXN1"],
                 "evidence_requested_from_seller": ["PROOF_OF_FULFILLMENT"], "evidence_submitted_by_seller": []}


@pytest.mark.local_spec
def test_item4_the_auto_prep_shell_carries_the_shared_ai_unavailable_flag(monkeypatch):
    db = config.db_path()
    repo.insert_document(db, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(auto_prep, "propose_package", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("busy")))
    auto_prep.prepare_dispute(DID, _DISPUTE_NORM, _PrepClient(), db_path=db, version_hash="v1", claim_token="t1")
    shell = repo.list_packages(db, DID)[0]
    assert shell["source"] == "auto_manual"
    assert auto_prep.AI_UNAVAILABLE_FLAG in shell["review_flags"]     # fails without the fix: only the generic line
    assert routes_package.AI_UNAVAILABLE_FLAG is auto_prep.AI_UNAVAILABLE_FLAG    # one wording, shared with the route
    assert "not available" in auto_prep.AI_UNAVAILABLE_FLAG and "manual mode" in auto_prep.AI_UNAVAILABLE_FLAG


@pytest.mark.local_spec
def test_item4_with_no_readable_document_the_ai_was_never_tried_so_no_such_flag(monkeypatch):
    db = config.db_path()
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})   # input, no document
    called = []
    monkeypatch.setattr(auto_prep, "propose_package", lambda *a, **kw: called.append(1))
    auto_prep.prepare_dispute(DID, _DISPUTE_NORM, _PrepClient(), db_path=db, version_hash="v1", claim_token="t1")
    shell = repo.list_packages(db, DID)[0]
    assert called == [] and auto_prep.AI_UNAVAILABLE_FLAG not in shell["review_flags"]


# ---- 5. an OAuth transport failure leaves the same sanitised JSONL line call() writes ------------------------
def test_item5_a_transport_failure_in_token_writes_a_sanitised_line(monkeypatch, tmp_path):
    from client import SandboxClient
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "FAKE-CLIENT-ID-ABC")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "FAKE-SECRET-XYZ")
    c = SandboxClient("unused_fix1_probe")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not connect for FAKE-SECRET-XYZ")

    c._http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler))
    c.log_path = tmp_path / "probe.jsonl"
    with pytest.raises(httpx.ConnectError):
        c.token()
    text = c.log_path.read_text(encoding="utf-8")                      # fails without the fix: the file does not exist
    lines = text.splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["op"] == "oauth_token" and rec["status"] is None
    assert rec["transport_error"] == "ConnectError" and rec["outcome"] == "unknown"
    for secret in ("FAKE-CLIENT-ID-ABC", "FAKE-SECRET-XYZ"):          # board rule 9: never a credential in a log
        assert secret not in text


# ---- 6. why a PDF could not be read, and a missing PDF reader is not an unreadable document ------------------
def test_item6_an_unreadable_pdf_logs_the_cause_without_the_content(caplog):
    garbage = b"%PDF-1.7\nCONTENT-MARKER-THAT-MUST-NOT-BE-LOGGED not a pdf"
    with caplog.at_level(logging.WARNING, logger="copilot.core.document_ingest"):
        text, ok = document_ingest.extract_text("application/pdf", garbage)
    assert (text, ok) == (None, False)                                  # unchanged result
    messages = [rec.getMessage() for rec in caplog.records if rec.name == "copilot.core.document_ingest"]
    assert len(messages) == 1 and messages[0].startswith("A PDF could not be read:")   # fails without the fix: silent
    assert "CONTENT-MARKER-THAT-MUST-NOT-BE-LOGGED" not in caplog.text


def test_item6_a_missing_pdf_reader_is_reported_apart_and_the_upload_says_so(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)                 # `import pypdfium2` now raises ImportError
    with caplog.at_level(logging.ERROR, logger="copilot.core.document_ingest"):
        with pytest.raises(document_ingest.PdfReaderUnavailable):       # fails without the fix: returns (None, False)
            document_ingest.extract_text("application/pdf", b"%PDF-1.7\nx")
    assert any("PDF reader is not available" in rec.getMessage() for rec in caplog.records)
    c, token = _client()
    pdf = (ROOT / "demo" / "documents" / "01_coherent_delivery.pdf").read_bytes()
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", pdf, "application/pdf")},
               data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 503 and "PDF reader is not available" in r.text
    assert "unreadable" not in r.text.lower() and "enter details" not in r.text.lower()
    assert repo.list_documents(config.db_path(), DID) == []            # nothing stored that would read as "unreadable"
    assert any(e["action"] == "upload_pdf_reader_unavailable" for e in repo.list_audit(config.db_path(), DID))


def test_item6_a_readable_pdf_still_uploads_when_the_reader_is_present():
    c, token = _client()
    pdf = (ROOT / "demo" / "documents" / "01_coherent_delivery.pdf").read_bytes()
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", pdf, "application/pdf")},
               data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and repo.list_documents(config.db_path(), DID)[0]["extraction_ok"] == 1


# ---- 7. a missing package, or one of another dispute, is the styled not-found page with a 404 ----
def test_item7_review_of_a_missing_or_foreign_package_is_the_not_found_page_with_a_404():
    c, _ = _client()
    db = config.db_path()
    mine = repo.insert_package(db, DID, source="human_edit", validation_ok=False, review_flags=[])
    foreign = repo.insert_package(db, OTHER, source="human_edit", validation_ok=False, review_flags=[])
    repo.insert_claims(db, foreign, [{"evidence_index": 0, "claim_index": 0, "text": "FOREIGN-CLAIM-TEXT", "source_ids": [], "basis": "seller_statement"}])
    ok = _review(c, mine)
    assert ok.status_code == 200 and state(parse(ok.text), "package-not-found") is None
    missing = _review(c, 987654)
    assert missing.status_code == 404                                   # fails without the fix: status 200
    assert state(parse(missing.text), "package-not-found") is not None  # the styled page is kept (a bare <p> broke it)
    crossed = _review(c, foreign)                                       # package of OTHER, opened under DID
    assert crossed.status_code == 404                                   # fails without the fix: the foreign package shows
    box = state(parse(crossed.text), "package-not-found")
    assert box is not None and ("a", f"/disputes/{DID}") in actions_in(box)
    assert "FOREIGN-CLAIM-TEXT" not in crossed.text and "Approve" not in crossed.text
    assert _review(c, foreign, dispute_id=OTHER).status_code == 200


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf
