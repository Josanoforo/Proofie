"""rc4.3 W1-1a: the read-back list of a confirmed send is kept and shown (lane RX).

EXPECTED (written before the first run; every test below is red on the code before the change unless it says it is a guard):
  details        : verify_after_with_attachments returns documents_present (the names PayPal lists with the matching
                   evidence, sorted) also when no file is attached; with no matching evidence it does not invent one.
  attempt store  : send_attempts has a readback_json column (added by ALTER on an old database); a write carries
                   `readback`, read()/list_by_dispute() return it, a later write that omits it keeps it; a row written
                   before the column has no readback.
  the three writes: reconcile_after_send, recover_dispute and the claim-conflict branch of send_service each store
                   {evidence_type, documents_present, paypal_date, read_at} with the confirmed state. read_at is an ISO
                   time with an offset; paypal_date is PayPal's own `date` text, unchanged.
  status         : SendRecord.readback is that record for a confirmed attempt and None for every other state.
  sent page      : after a confirmed send the result page lists the documents PayPal lists (two names, one of them not
                   attached by the seller), says so in the "readback-record" block, and a legacy row (no readback) gets the
                   "readback-record-missing" sentence instead. Guard: the existing readback-confirmed sentence stays.
  zero POST      : nothing here sends; the only POSTs are the one send of each route scenario.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from _dom import parse, state

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core import send_recovery
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json, verify_after_with_attachments
from copilot.core.send_recovery import reconcile_after_send, recover_dispute
from copilot.db import repo
from copilot.web import status

import test_routes_send as send_tests
from test_ux_rc41_fix_state import STATEMENT, LABEL_A, add_file, confirmed_body, posts, request_input, result_page
from test_ux_rc41_fix_state_status import seed_attempt
from test_ux_rc41_package import DID, approve_passive, draft, env  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]

REQ = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                      "evidence_info": {"tracking_info": [{"carrier_name": "MEX_ESTAFETA", "tracking_number": "DEMO1"}]},
                      "notes": "demo"}]}


def _raw(names, date="2026-10-05T10:00:00Z"):
    return {"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]},
                           "notes": "demo", "date": date, "documents": [{"name": n} for n in names]}]}


class Scripted:
    def __init__(self, *bodies):
        self.bodies = list(bodies)

    def call(self, method, path, op, **kw):
        assert method == "GET"
        return self.bodies.pop(0)


def _store(tmp_path):
    return SQLiteAttemptStore(tmp_path / "attempts.db")


def _is_iso_with_offset(text):
    parsed = datetime.fromisoformat(text)
    return parsed.tzinfo is not None


# ----------------------------------------------------------------------------------------------- the details
def test_documents_present_is_returned_when_no_file_is_attached():
    result = verify_after_with_attachments(_raw(["b.pdf", "a.pdf"]), REQ, [])
    assert result["content_ok"] and result["files_ok"]
    assert result["details"]["documents_present"] == ["a.pdf", "b.pdf"]


def test_documents_present_is_an_empty_list_when_paypal_lists_no_document():
    result = verify_after_with_attachments(_raw([]), REQ, [])
    assert result["content_ok"] and result["details"]["documents_present"] == []


def test_no_matching_evidence_gives_no_documents_present():
    result = verify_after_with_attachments({"evidences": []}, REQ, [])
    assert not result["content_ok"] and "documents_present" not in result["details"]


def test_the_attached_case_keeps_its_documents_present_guard():
    result = verify_after_with_attachments(_raw(["label.pdf", "extra.pdf"]), REQ, ["label.pdf"])
    assert result["files_ok"] and result["details"]["documents_present"] == ["extra.pdf", "label.pdf"]


# ---------------------------------------------------------------------------------------------- the store
READBACK = {"evidence_type": "PROOF_OF_FULFILLMENT", "documents_present": ["a.pdf"], "paypal_date": "2026-10-05",
            "read_at": "2026-10-20T12:00:00+00:00"}


def test_the_store_keeps_the_readback_and_a_later_write_without_it_leaves_it(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.claim("D1", "sha1")
        store.write("D1", "sha1", {"state": "confirmed", "request_input": REQ, "readback": READBACK})
        assert store.read("D1", "sha1")["readback"] == READBACK
        assert store.list_by_dispute("D1")[0]["readback"] == READBACK
        store.write("D1", "sha1", {"state": "confirmed", "request_input": REQ})
        assert store.read("D1", "sha1")["readback"] == READBACK
    finally:
        store.close()


def test_a_row_written_without_a_readback_has_none(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.claim("D1", "sha1")
        store.write("D1", "sha1", {"state": "sent_unconfirmed", "request_input": REQ})
        assert store.read("D1", "sha1").get("readback") is None
    finally:
        store.close()


def test_an_old_database_gets_the_column_and_its_rows_have_no_readback(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE send_attempts (dispute_id TEXT NOT NULL, package_sha256 TEXT NOT NULL, state TEXT NOT NULL, "
                 "request_id TEXT, http_status INTEGER, reason TEXT, request_input_json TEXT, "
                 "attachment_filenames_json TEXT, attachment_sha256_json TEXT, ts TEXT NOT NULL, "
                 "PRIMARY KEY (dispute_id, package_sha256))")
    conn.execute("INSERT INTO send_attempts (dispute_id, package_sha256, state, ts) VALUES ('D1', 'sha1', 'confirmed', 't')")
    conn.commit()
    conn.close()
    store = SQLiteAttemptStore(db)
    try:
        assert store.read("D1", "sha1").get("readback") is None
        store.write("D1", "sha1", {"state": "confirmed", "readback": READBACK})
        assert store.read("D1", "sha1")["readback"] == READBACK
    finally:
        store.close()


# --------------------------------------------------------------------------------------- the three writes
def test_reconcile_after_send_stores_the_readback_with_the_confirmed_state(tmp_path, monkeypatch):
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    store = _store(tmp_path)
    try:
        assert store.claim("D1", "sha1")
        store.write("D1", "sha1", {"state": "sent_unconfirmed", "request_id": "r1", "request_input": REQ})
        out = reconcile_after_send(Scripted(_raw(["a.pdf", "b.pdf"])), store, "D1", "sha1", REQ, attachment_filenames=[])
        assert out["confirmed"] is True
        row = store.read("D1", "sha1")
        assert row["state"] == "confirmed"
        readback = row["readback"]
        assert set(readback) == {"evidence_type", "documents_present", "paypal_date", "read_at"}
        assert readback["evidence_type"] == "PROOF_OF_FULFILLMENT"
        assert readback["documents_present"] == ["a.pdf", "b.pdf"]
        assert readback["paypal_date"] == "2026-10-05T10:00:00Z"
        assert _is_iso_with_offset(readback["read_at"])
    finally:
        store.close()


def test_recover_dispute_stores_the_readback_when_it_confirms(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.claim("D1", "sha1")
        store.write("D1", "sha1", {"state": "sent_unconfirmed", "request_id": "r1", "request_input": REQ,
                                   "attachment_filenames": ["a.pdf"]})
        assert recover_dispute(Scripted(_raw(["a.pdf", "c.pdf"])), store, "D1", tmp_path / "x.db") == []
        row = store.read("D1", "sha1")
        assert row["state"] == "confirmed"
        assert row["readback"]["documents_present"] == ["a.pdf", "c.pdf"]
        assert row["readback"]["evidence_type"] == "PROOF_OF_FULFILLMENT" and _is_iso_with_offset(row["readback"]["read_at"])
    finally:
        store.close()


def test_a_partial_state_stores_no_readback(tmp_path, monkeypatch):
    """Guard: only a confirmed write carries the list; the partial state keeps its reason and nothing else."""
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    store = _store(tmp_path)
    try:
        assert store.claim("D1", "sha1")
        store.write("D1", "sha1", {"state": "sent_unconfirmed", "request_id": "r1", "request_input": REQ})
        out = reconcile_after_send(Scripted(_raw(["a.pdf"])), store, "D1", "sha1", REQ,
                                   attachment_filenames=["a.pdf", "b.pdf"])
        assert out["confirmed"] is False
        assert store.read("D1", "sha1").get("readback") is None
    finally:
        store.close()


def test_the_claim_conflict_branch_of_the_send_stores_the_readback(env):
    """Site send_service claim-conflict branch (the write of 'confirmed' when PayPal already has the content)."""
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")
    fake.pre_send_get_body = {**send_tests.WAITING, **confirmed_body(db, pkg, ["label.pdf", "from-paypal.pdf"])}
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    assert result_page(c, r).status_code == 200 and posts(fake) == []
    store = SQLiteAttemptStore(db)
    try:
        row = store.read(DID, sha256_json(request_input(db, pkg)))
    finally:
        store.close()
    assert row["state"] == "confirmed"
    assert row["readback"]["documents_present"] == ["from-paypal.pdf", "label.pdf"]
    assert row["readback"]["evidence_type"] == "PROOF_OF_FULFILLMENT" and _is_iso_with_offset(row["readback"]["read_at"])


# ------------------------------------------------------------------------------------------------ status
def test_the_send_record_carries_the_readback_only_when_confirmed(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")
    package = repo.get_package(db, pkg)
    assert status.send_record(db, DID, package).readback is None
    store = SQLiteAttemptStore(db)
    try:
        sha = sha256_json(request_input(db, pkg))
        store.write(DID, sha, {"state": "confirmed", "readback": READBACK})
    finally:
        store.close()
    assert status.send_record(db, DID, repo.get_package(db, pkg)).readback == READBACK


# ----------------------------------------------------------------------------------------------- the page
def _send(env, bodies):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, bodies)]
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    return pkg, result_page(c, r), fake


def test_the_sent_page_lists_the_documents_paypal_lists(env):
    pkg, page, fake = _send(env, ["label.pdf", "second-from-paypal.pdf"])
    assert page.status_code == 200 and len(posts(fake)) == 1
    dom = parse(page.text)
    block = state(dom, "readback-record")
    assert block is not None
    text = block.text()
    assert "label.pdf" in text and "second-from-paypal.pdf" in text
    assert "Proof the order was shipped or fulfilled".lower() in text.lower()
    assert state(dom, "readback-confirmed") is not None                   # guard: the existing sentence stays
    assert state(dom, "readback-record-missing") is None


def test_the_sent_page_says_when_paypal_lists_no_document(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)                                  # a package with no attached file
    approve_passive(c, token, pkg)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, [])]
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    page = result_page(c, r)
    block = state(parse(page.text), "readback-record")
    assert block is not None and "lists no document" in block.text()


def test_a_legacy_row_without_a_readback_gets_the_fallback_sentence(env):
    pkg, page, fake = _send(env, ["label.pdf"])
    c, db, token, _ = env
    conn = sqlite3.connect(db)
    conn.execute("UPDATE send_attempts SET readback_json = NULL")         # a row written before the column existed
    conn.commit()
    conn.close()
    rid = page.url.path.rsplit("/", 1)[-1] if hasattr(page.url, "path") else str(page.url).rsplit("/", 1)[-1]
    again = c.get(f"/disputes/{DID}/packages/{pkg}/result/{rid}")
    dom = parse(again.text)
    assert state(dom, "readback-record") is None
    missing = state(dom, "readback-record-missing")
    assert missing is not None and "before Proofie kept" in missing.text()
