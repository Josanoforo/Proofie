"""Case history page (N2, surface 1). Synthetic rows in a temporary database; no PayPal call.

Expected, written before the first run:
- flag off: GET /disputes/<id>/history is 404 and the case page has no history link;
- flag on, nothing recorded: 200 with the empty-state words and no entry list;
- flag on, with one package, an approval cleared by an edit, a send attempt, a refused send and a later read: 200, the
  four kinds each appear under their own words (Local work, Send attempted, PayPal response, Later read from PayPal),
  entries run oldest first, the read names its source and time, hashes sit inside a <details> next to "A hash does not
  prove authenticity", and the page names no judge route;
- the case page links to the history and the history links back to the case.
"""
import re

import pytest
from fastapi.testclient import TestClient
from _hosted import authenticated_client

from copilot import config
from copilot.app import create_app
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.db import repo
from copilot.features.case_history import service
from copilot.web.routes_disputes import get_client

DID = "PP-R-DEMO-0000001"
URL = f"/disputes/{DID}/history"


class Fake:
    def call(self, method, path, op, **kw):
        assert method == "GET"
        if op == "show_dispute":
            return {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
                    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "dispute_life_cycle_stage": "INQUIRY",
                    "dispute_amount": {"currency_code": "USD", "value": "25.00"}, "create_time": "2026-09-25T16:00:00Z",
                    "disputed_transactions": [{"seller_transaction_id": "DEMOTXN0000000001"}], "evidences": [],
                    "links": []}
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(op)


def _client(monkeypatch, on):
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    if on:
        monkeypatch.setenv("COPILOT_FEATURE_CASE_HISTORY", "true")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    return authenticated_client(app)


def _at(db, table, column, value, where, args):
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        conn.execute(f"UPDATE {table} SET {column}=? WHERE {where}", (value, *args))


def _record(db):
    """One package; approved 10:05; cleared by an edit 10:10; a send attempt 10:20; refused 10:21; a later read 10:30."""
    package = repo.insert_package(db, DID, "human_edit", True, [])
    _at(db, "packages", "created_at", "2026-10-20T10:00:00-0600", "id=?", (package,))
    approval = repo.insert_approval(db, package, "a" * 64, "vendor")
    _at(db, "approvals", "approved_at", "2026-10-20T10:05:00-0600", "id=?", (approval,))
    repo.invalidate_approval(db, approval)
    _at(db, "approvals", "invalidated_at", "2026-10-20T10:10:00-0600", "id=?", (approval,))
    ids = {}
    for action, detail, ts in (
            ("invalidate_approval_after_edit", {"package_id": package, "approval_id": approval}, "2026-10-20T10:10:00-0600"),
            ("send_attempt_registered", {"package_id": package, "approval_sha256": "a" * 64, "request_id": "REQ-1",
                                         "attachments": ["label.pdf"], "attachment_sha256": ["b" * 64]},
             "2026-10-20T10:20:00-0600"),
            ("send_failed", {"package_id": package, "http_status": 422, "issue": "INVALID_EVIDENCE"},
             "2026-10-20T10:21:00-0600"),
            ("send_confirmed", {"package_id": package, "details": {}}, "2026-10-20T10:30:00-0600")):
        ids[action] = repo.log_action(db, "system", action, DID, detail)
        _at(db, "audit_log", "ts", ts, "id=?", (ids[action],))
    store = SQLiteAttemptStore(db)
    try:
        store.claim(DID, "c" * 64)
        store.write(DID, "c" * 64, {"state": "confirmed", "request_id": "REQ-1",
                                    "readback": {"evidence_type": "PROOF_OF_FULFILLMENT", "documents_present": ["label.pdf"],
                                                 "read_at": "2026-10-20T10:31:00-0600"}})
    finally:
        store.close()
    return package


def test_flag_off_is_404_and_the_case_page_has_no_link(monkeypatch):
    web = _client(monkeypatch, on=False)
    assert web.get(URL).status_code == 404
    case = web.get(f"/disputes/{DID}")
    assert case.status_code == 200 and URL not in case.text


def test_empty_state_explains_that_nothing_is_recorded(monkeypatch):
    web = _client(monkeypatch, on=True)
    response = web.get(URL)
    assert response.status_code == 200
    assert "Nothing is recorded yet" in response.text and 'data-state="history-empty"' in response.text
    assert 'class="history-list"' not in response.text


def test_four_kinds_are_kept_apart_in_words_and_in_time_order(monkeypatch):
    web = _client(monkeypatch, on=True)
    _record(config.db_path())
    html = web.get(URL).text
    items = re.findall(r'<li class="history-item" data-kind="(\w+)">(.*?)</li>', html, re.S)
    kinds = [k for k, _ in items]
    assert set(kinds) == {"local", "attempt", "response", "read"}
    # prepared, approved, cleared (local), attempt, response, read: oldest first
    assert kinds == ["local", "local", "local", "attempt", "response", "read"]
    for k, body in items:
        assert service.KIND_WORDS[k] in body
    read = [b for k, b in items if k == "read"][0]
    assert "Source: PayPal dispute record" in read and "Read at" in read and "Files listed: label.pdf" in read
    assert "was cleared because the package was edited" in html
    assert "PayPal refused the send of package v1 (HTTP 422)" in html
    # local work never claims a PayPal state
    local = " ".join(b for k, b in items if k == "local")
    assert "PayPal" not in local.replace("PayPal-", "")


def test_hashes_are_on_demand_with_the_authenticity_words_and_no_judge_content(monkeypatch):
    web = _client(monkeypatch, on=True)
    _record(config.db_path())
    html = web.get(URL).text
    assert html.count("<details>") >= 2 and "A hash does not prove authenticity" in html
    for details in re.findall(r"<details>(.*?)</details>", html, re.S):
        assert "A hash does not prove authenticity" in details
    assert "a" * 64 in html and "b" * 64 in html
    low = html.lower()
    assert "judge" not in low and "hackathon" not in low and "<main" not in html[html.index("<main") + 5:]


def test_the_case_page_links_to_the_history_and_back(monkeypatch):
    web = _client(monkeypatch, on=True)
    case = web.get(f"/disputes/{DID}/workspace/records").text
    assert f'href="{URL}"' in case
    page = web.get(URL).text
    assert f'href="/disputes/{DID}"' in page


def test_versions_table_has_column_headers(monkeypatch):
    web = _client(monkeypatch, on=True)
    _record(config.db_path())
    html = web.get(URL).text
    assert html.count('scope="col"') == 4 and 'scope="row"' in html
    assert "Package v1" in html
