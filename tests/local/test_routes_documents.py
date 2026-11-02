"""Offline tests for document upload."""
import pytest
from _hosted import authenticated_client, reset_csrf
import io
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.app import create_app
from copilot.core.raw_storage import read_raw, write_raw
from copilot.db import repo
from copilot.web import routes_documents
from copilot.web.security import CSRF_COOKIE_NAME

PDF_BYTES = b"%PDF-1.7\nnot a fully valid pdf body, just enough to be detected by its magic header\n"
DID = "PP-R-TEST-0001"


def _client():
    # conftest.py's autouse _isolated_copilot_data fixture already redirects config.db_path()/
    # uploads_dir() to this test's own tmp_path - nothing to override here.
    c = authenticated_client(create_app(), client=("127.0.0.1", 12345))
    return c, config.db_path()


def _get_csrf(c):
    # any GET from a route that issues the cookie would do; upload doesn't need a prior page render in
    # these tests, so set the cookie directly like a real browser already holding one from a prior page.
    import secrets
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return token


@pytest.mark.parametrize('action,initial', [('exclude', False), ('include', True)])
@pytest.mark.parametrize('matches', [False, True])
def test_document_action_binds_resource_and_audit_to_dispute(action, initial, matches):
    from starlette.requests import Request
    db = config.db_path()
    other = 'PP-R-TEST-0002'
    owner = DID if matches else other
    doc = repo.insert_document(db, owner, 'synthetic.pdf', 'application/pdf', 10,
                               'synthetic-sha', 'synthetic', True)
    repo.set_document_excluded(db, doc, initial)
    before = repo.get_document(db, doc)
    token = 'synthetic-csrf'
    request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http',
                       'path': f'/disputes/{DID}/documents/{doc}/{action}',
                       'headers': [(b'cookie', ('csrf_token=' + token).encode())],
                       'query_string': b'', 'server': ('127.0.0.1', 8000),
                       'client': ('127.0.0.1', 12345)})
    handler = getattr(routes_documents, action + '_document')
    response = handler(request, DID, doc, token)
    after = repo.get_document(db, doc)
    audit = repo.list_audit(db, DID)
    if matches:
        assert response.status_code == 303
        assert after['excluded'] == (action == 'exclude')
        assert len(audit) == 1 and audit[0]['detail']['document_id'] == doc
        assert audit[0]['action'] == action + '_document'
    else:
        assert response.status_code == 404
        assert after == before
        assert not audit and not repo.list_audit(db, other)


def test_sanitize_filename_strips_path_components_and_unsafe_chars():
    assert routes_documents.sanitize_filename("../../etc/passwd") == "passwd"
    assert routes_documents.sanitize_filename("my file (1).pdf") == "my_file__1_.pdf"
    assert routes_documents.sanitize_filename("C:\\Users\\x\\label.pdf") == "label.pdf"
    assert routes_documents.sanitize_filename(None) == "upload"
    assert routes_documents.sanitize_filename("") == "upload"


def test_upload_without_csrf_cookie_is_rejected():
    c, db_path = _client()
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
              data={"csrf_token": "forged"})
    assert r.status_code == 403
    assert repo.list_documents(db_path, DID) == []


def test_upload_valid_pdf_is_stored_and_redirects():
    c, db_path = _client()
    token = _get_csrf(c)
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    docs = repo.list_documents(db_path, DID)
    assert len(docs) == 1 and docs[0]["filename"] == "label.pdf"
    assert docs[0]["content_type"] == "application/pdf"


def test_original_get_returns_case_scoped_hash_verified_bytes_and_download_headers():
    c, db_path = _client()
    body = b"%PDF original synthetic bytes"
    import hashlib
    digest = hashlib.sha256(body).hexdigest()
    doc_id = repo.insert_document(db_path, DID, "receipt.pdf", "application/pdf", len(body), digest,
                                  "Extracted claim text", True)
    write_raw(config.uploads_dir(), digest, body)
    response = c.get(f"/disputes/{DID}/documents/{doc_id}/original")
    if config.hosted_demo():                   # the shared demo never serves stored originals
        assert response.status_code == 404 and body not in response.content
        return
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="receipt.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_original_get_rejects_cross_case_missing_and_corrupt_bytes():
    c, db_path = _client()
    import hashlib
    body = b"%PDF scoped bytes"
    digest = hashlib.sha256(body).hexdigest()
    doc_id = repo.insert_document(db_path, DID, "receipt.pdf", "application/pdf", len(body), digest, "text", True)
    write_raw(config.uploads_dir(), digest, body)
    assert c.get(f"/disputes/PP-R-OTHER-0001/documents/{doc_id}/original").status_code == 404
    write_raw(config.uploads_dir(), digest, b"tampered")
    assert c.get(f"/disputes/{DID}/documents/{doc_id}/original").status_code == 404
    assert c.get(f"/disputes/{DID}/documents/999999/original").status_code == 404


def test_upload_rejects_a_malformed_dispute_id_before_any_write(tmp_path):
    """The dispute id is validated before anything is written."""
    c, db_path = _client()
    token = _get_csrf(c)
    r = c.post("/disputes/not-a-real-paypal-id/documents",
              files={"file": ("label.pdf", PDF_BYTES, "application/pdf")}, data={"csrf_token": token})
    assert r.status_code == 400
    assert "That dispute reference is not valid." in r.text
    assert repo.list_documents(db_path, "not-a-real-paypal-id") == []


def test_upload_error_message_escapes_the_filename_even_if_unsanitized(monkeypatch):
    """html.escape defends the error message even if sanitize_filename were ever
    bypassed - forced here deliberately, since the real sanitizer already strips '<'/'>' well before this
    point in the normal flow (so a literal <script> can never reach here through the public route alone)."""
    from copilot.web import routes_documents
    monkeypatch.setattr(routes_documents, "sanitize_filename", lambda name: "<script>evil</script>.pdf")
    c, db_path = _client()
    token = _get_csrf(c)
    r = c.post(f"/disputes/{DID}/documents",
              files={"file": ("x.pdf", b"not a real pdf", "application/pdf")}, data={"csrf_token": token})
    assert r.status_code == 400
    assert "<script>evil</script>" not in r.text
    assert "evil" not in r.text                                       # The file name is never echoed


def test_upload_invalid_content_is_rejected():
    c, db_path = _client()
    token = _get_csrf(c)
    r = c.post(f"/disputes/{DID}/documents",
              files={"file": ("invoice.pdf", b"not actually a pdf", "application/pdf")},
              data={"csrf_token": token})
    assert r.status_code == 400
    assert repo.list_documents(db_path, DID) == []


def test_upload_logs_audit_entry():
    c, db_path = _client()
    token = _get_csrf(c)
    c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
          data={"csrf_token": token}, follow_redirects=False)   # the redirect target hits the real
                                                                # SandboxClient (not overridden here) -
                                                                # never follow it in a test
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "upload_document" and e["actor"] == "vendor" for e in entries)


def test_upload_sanitizes_a_path_traversal_filename():
    c, db_path = _client()
    token = _get_csrf(c)
    c.post(f"/disputes/{DID}/documents",
          files={"file": ("../../../evil.pdf", PDF_BYTES, "application/pdf")},
          data={"csrf_token": token}, follow_redirects=False)
    docs = repo.list_documents(db_path, DID)
    assert len(docs) == 1
    assert "/" not in docs[0]["filename"] and ".." not in docs[0]["filename"]


# ---- exclude/include ("remove the document" conflict resolution) -------------------------
def test_exclude_document_marks_it_excluded_and_is_reversible():
    c, db_path = _client()
    token = _get_csrf(c)
    doc_id = repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 1, "s1", "x", True)
    assert repo.get_document(db_path, doc_id)["excluded"] is False

    r = c.post(f"/disputes/{DID}/documents/{doc_id}/exclude", data={"csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_document(db_path, doc_id)["excluded"] is True

    r = c.post(f"/disputes/{DID}/documents/{doc_id}/include", data={"csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_document(db_path, doc_id)["excluded"] is False


def test_exclude_document_without_csrf_is_rejected():
    c, db_path = _client()
    doc_id = repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 1, "s1", "x", True)
    r = c.post(f"/disputes/{DID}/documents/{doc_id}/exclude", data={"csrf_token": "forged"})
    assert r.status_code == 403
    assert repo.get_document(db_path, doc_id)["excluded"] is False


def test_exclude_unknown_document_is_a_plain_404_not_a_crash():
    c, db_path = _client()
    token = _get_csrf(c)
    r = c.post(f"/disputes/{DID}/documents/999999/exclude", data={"csrf_token": token})
    assert r.status_code == 404


def test_exclude_document_is_audited():
    c, db_path = _client()
    token = _get_csrf(c)
    doc_id = repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 1, "s1", "x", True)
    c.post(f"/disputes/{DID}/documents/{doc_id}/exclude", data={"csrf_token": token}, follow_redirects=False)
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "exclude_document" and e["detail"]["document_id"] == doc_id for e in entries)


# ---- raw bytes are persisted too (a real send needs them back to attach a file) ----------------------
def test_upload_persists_the_raw_bytes_readable_back_by_sha256():
    c, db_path = _client()
    token = _get_csrf(c)
    c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
          data={"csrf_token": token}, follow_redirects=False)
    doc = repo.list_documents(db_path, DID)[0]
    stored = read_raw(config.uploads_dir(), doc["sha256"])
    assert stored == PDF_BYTES


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf


@pytest.mark.parametrize("stage", ["before-row", "after-row"])
def test_upload_busy_does_not_claim_partial_writes_were_rolled_back(monkeypatch, stage):
    import hashlib
    import sqlite3
    c, db = _client()
    token = _get_csrf(c)
    def busy(*args, **kwargs):
        error = sqlite3.OperationalError("PRIVATE synthetic SQL/path")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error
    with monkeypatch.context() as patch:
        patch.setattr(repo, "insert_document" if stage == "before-row" else "log_action", busy)
        response = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
                          data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 503
    assert "review what was saved" in response.text and "Retry only unsaved edits manually" in response.text
    assert "Nothing was changed" not in response.text and "PRIVATE" not in response.text
    docs = repo.list_documents(db, DID)
    assert len(docs) == (0 if stage == "before-row" else 1)
    assert read_raw(config.uploads_dir(), hashlib.sha256(PDF_BYTES).hexdigest()) == PDF_BYTES
    assert repo.list_audit(db, DID) == []


@pytest.mark.parametrize("code", [5, 6, 5 | (1 << 8), 6 | (1 << 8), None])
def test_sqlite_busy_classification_uses_result_code_not_message(monkeypatch, code):
    import sqlite3
    c, db = _client()
    token = _get_csrf(c)
    doc = repo.insert_document(db, DID, "a.pdf", "application/pdf", 1, "s1", "x", True)
    def error(*args, **kwargs):
        exc = sqlite3.OperationalError("database is locked PRIVATE")
        if code is not None:
            exc.sqlite_errorcode = code
        raise exc
    monkeypatch.setattr(repo, "set_document_excluded", error)
    response = c.post(f"/disputes/{DID}/documents/{doc}/exclude", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == (500 if code is None else 503)
    assert "PRIVATE" not in response.text
    assert ('data-state="local-update-busy"' in response.text) == (code is not None)


def test_non_lock_operational_error_retains_safe_internal_error(monkeypatch):
    import sqlite3
    c, db = _client()
    token = _get_csrf(c)
    doc = repo.insert_document(db, DID, "a.pdf", "application/pdf", 1, "s1", "x", True)
    def invalid_query(*args, **kwargs):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("SELECT * FROM PRIVATE_missing_table")
    monkeypatch.setattr(repo, "set_document_excluded", invalid_query)
    response = c.post(f"/disputes/{DID}/documents/{doc}/exclude", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 500
    assert 'data-state="internal-error"' in response.text
    assert "PRIVATE" not in response.text and "temporarily busy" not in response.text
    assert repo.get_document(db, doc)["excluded"] is False


def _hosted_client(monkeypatch, with_fake_dispute=False, hosted=True):
    import json
    from _hosted import TEST_ACCESS_CODE
    if hosted:
        monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
        monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    app = create_app()
    if with_fake_dispute:
        from copilot.web.routes_disputes import get_client
        body = json.loads((Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic" /
                           "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]

        class _Fake:
            def call(self, method, path, op, **kw):
                return {"context_transaction_search": {"transaction_details": []},
                        "context_tracking": {"trackers": []}}.get(op, body)
        app.dependency_overrides[get_client] = lambda: _Fake()
    return authenticated_client(app, client=("127.0.0.1", 12345))


def test_original_get_is_refused_in_the_hosted_demo(monkeypatch):
    import hashlib
    body = b"%PDF hosted visitor bytes"
    digest = hashlib.sha256(body).hexdigest()
    c = _hosted_client(monkeypatch)
    doc_id = repo.insert_document(config.db_path(), DID, "receipt.pdf", "application/pdf", len(body), digest, "t", True)
    write_raw(config.uploads_dir(), digest, body)
    response = c.get(f"/disputes/{DID}/documents/{doc_id}/original")
    assert response.status_code == 404 and body not in response.content


def test_open_original_link_is_hidden_in_the_hosted_demo_and_shown_locally(monkeypatch):
    did = "PP-R-DEMO-0000001"
    repo.insert_document(config.db_path(), did, "label.pdf", "application/pdf", 10, "a" * 64, "t", True)
    if not config.hosted_demo():
        local = _hosted_client(monkeypatch, with_fake_dispute=True, hosted=False)
        assert "Open original" in local.get(f"/disputes/{did}/workspace/prepare").text
    hosted = _hosted_client(monkeypatch, with_fake_dispute=True)
    page = hosted.get(f"/disputes/{did}/workspace/prepare")
    assert page.status_code == 200 and "label.pdf" in page.text and "Open original" not in page.text
