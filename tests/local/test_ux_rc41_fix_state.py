"""Send records, files by sha256, status consistency, next step, print and pages.

Offline only: fake PayPal clients, a temp database per test (conftest.py). Effects are read from the stored rows, the
requests the fake saw and the rendered DOM, never from words alone. Helpers come from test_ux_rc41_package.py.
"""
import re

import pytest
from _dom import parse, state

from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.send import build_request_input, pick_tracking, sha256_json
from copilot.db import repo
from copilot.web import status

import test_routes_send as send_tests
from test_ux_rc41_package import (DID, add_claims, approve_passive, approved, draft, env, location,  # noqa: F401
                                  new_package, page, save_shipment, upload)

pytestmark = [pytest.mark.usefixtures("web_mode")]

LABEL_A = b"%PDF-1.7\nlabel, first version\n"
LABEL_B = b"%PDF-1.7\nlabel, corrected version, other bytes\n"
OTHER = b"%PDF-1.7\nanother document\n"
STATEMENT = "We shipped the parcel with FedEx; the label is attached."


# --------------------------------------------------------------------------------------------------- helpers
def add_file(c, db, token, pkg, name, data):
    """Uploads a document and attaches it to the package; returns the document id."""
    before = {d["id"] for d in repo.list_documents(db, DID)}
    assert upload(c, token, name, data).status_code == 303
    doc = next(d["id"] for d in repo.list_documents(db, DID) if d["id"] not in before)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/attach/{doc}", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return doc


def request_input(db, pkg):
    tracking, carrier = pick_tracking(extracted_docs_for(db, DID))
    return build_request_input(repo.list_claims(db, pkg), tracking, carrier)


def seed_confirmed(db, pkg, request_id="req-v1", with_shas=True):
    """A confirmed attempt for the package's current content and attached files, registered the way the send path
    registers it (audit entry with the files by name and by sha256; the attempt row with the same)."""
    rows = repo.list_attachments(db, pkg)
    names, shas = [r["filename"] for r in rows], [r["sha256"] for r in rows]
    req = request_input(db, pkg)
    approval = repo.get_latest_valid_approval(db, pkg)
    detail = {"package_id": pkg, "approval_sha256": approval["approval_sha256"] if approval else "x" * 64,
              "request_id": request_id, "attachments": names}
    record = {"state": "confirmed", "request_id": request_id, "request_input": req, "attachment_filenames": names}
    if with_shas:
        detail["attachment_sha256"], record["attachment_sha256"] = shas, shas
    repo.log_action(db, "system", "send_attempt_registered", DID, detail)
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(DID, sha256_json(req))
        store.write(DID, sha256_json(req), record)
    finally:
        store.close()
    return sha256_json(req)


def confirmed_body(db, pkg, names):
    req = request_input(db, pkg)
    return send_tests._confirmed_body(documents=[{"name": n} for n in names],
                                      notes=req["evidences"][0]["notes"])


def banner(html):
    dom = parse(html)
    found = [n for n in dom.walk() if "banner" in n.attrs.get("class", "").split()]
    return dom, (found[0] if found else None)


def result_page(c, response):
    assert response.status_code == 303, response.status_code
    r = c.get(response.headers["location"])
    return r


def two_versions_same_statements(env, v1_files, v2_files):
    """v1 confirmed with v1_files; v2 has the same statement and tracking with v2_files (name, bytes), approved."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    for name, data in v1_files:
        add_file(c, db, token, v1, name, data)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    for name, data in v2_files:
        add_file(c, db, token, v2, name, data)
    approve_passive(c, token, v2)
    assert repo.get_latest_valid_approval(db, v2)
    return v1, v2


def posts(fake):
    return fake.provide_evidence_calls


# ------------------------------------------- the send records the files by sha256
def test_a_real_send_records_the_attached_files_by_sha256_in_the_audit_entry_and_the_attempt(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, ["label.pdf"])]
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and len(posts(fake)) == 1
    want = [a["sha256"] for a in repo.list_attachments(db, pkg)]
    entry = next(e for e in repo.list_audit(db, DID) if e["action"] == "send_attempt_registered")
    assert entry["detail"]["attachment_sha256"] == want and entry["detail"]["attachments"] == ["label.pdf"]
    store = SQLiteAttemptStore(db)
    try:
        attempt = store.list_by_dispute(DID)[0]
    finally:
        store.close()
    assert attempt["state"] == "confirmed" and attempt["attachment_sha256"] == want


# --- same statements, other files
def test_same_statements_with_another_file_are_refused_naming_the_unsent_file_and_nothing_is_posted(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)])
    assert len(repo.list_attachments(db, v2)) == 2
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert res.status_code == 409
    assert posts(fake) == []                                                  # no write to PayPal
    dom, box = banner(res.text)
    text = dom.find("main").text() if dom.find("main") else dom.text()
    assert "b.pdf" in text and "Same statements, other files" in text
    assert "PayPal now shows this evidence as received" not in text and "Package already received" not in text
    assert "received the details" not in text
    assert box.attrs.get("data-state") == "blocked"
    # what is stored: v1's confirmed row is untouched
    store = SQLiteAttemptStore(db)
    try:
        rows = store.list_by_dispute(DID)
    finally:
        store.close()
    assert len(rows) == 1 and rows[0]["state"] == "confirmed" and rows[0]["attachment_filenames"] == ["a.pdf"]


def test_check_again_on_the_new_version_does_not_claim_it_was_received(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)])
    r = c.post(f"/disputes/{DID}/packages/{v2}/check", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    dom, box = banner(res.text)
    assert "Already recorded as received" not in dom.text() and box.attrs.get("data-state") != "confirmed"
    assert posts(fake) == []


# --- same name, different bytes
def test_a_corrected_file_with_the_same_name_is_not_taken_for_the_file_already_sent(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("label.pdf", LABEL_A)], [("label.pdf", LABEL_B)])
    a, b = repo.list_attachments(db, v1)[0], repo.list_attachments(db, v2)[0]
    assert a["filename"] == b["filename"] and a["sha256"] != b["sha256"]
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    dom, _ = banner(res.text)
    assert "Package already received" not in dom.text() and "exact package" not in dom.text()
    assert "Same statements, other files" in dom.text() and "label.pdf" in dom.text()
    assert posts(fake) == []
    # the record of v2 does not hold v1's send
    assert status.send_record(db, DID, repo.get_package(db, v2)).state == "none"
    assert status.send_record(db, DID, repo.get_package(db, v1)).state == "confirmed"


def test_the_same_bytes_under_another_name_count_as_the_file_already_sent(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("first-name.pdf", LABEL_A)], [("renamed.pdf", LABEL_A)])
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert res.status_code == 409 and "Package already received" in res.text
    assert posts(fake) == []


def test_the_legacy_attempt_without_shas_falls_back_to_the_names(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "label.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1, with_shas=False)
    record = status.send_record(db, DID, repo.get_package(db, v1))
    assert record.state == "confirmed" and record.attachments_match is True
    doc = repo.insert_document(db, DID, "late.pdf", "application/pdf", 10, "sha-l", "x", True)
    repo.attach_document(db, v1, doc)
    assert status.send_record(db, DID, repo.get_package(db, v1)).attachments_match is False


# --- the audit-mapped record answers
def edit_tracking_dispute_wide(c, token, number="DEMO-FDX-000777"):
    r = c.post(f"/disputes/{DID}/manual-fields",
               data={"csrf_token": token, "tracking_number": number, "carrier": "FEDEX"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def test_check_again_on_a_sent_version_after_a_dispute_wide_edit_still_says_it_was_received(env):
    c, db, token, fake = env
    v1 = approved(c, db, token, STATEMENT)
    seed_confirmed(db, v1)
    edit_tracking_dispute_wide(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v1}/check", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    dom, box = banner(res.text)
    assert box.attrs.get("data-state") == "confirmed", dom.text()
    assert "Nothing pending" not in dom.text() and "Prepared" not in dom.text()
    assert posts(fake) == []


def test_a_repeated_send_of_a_sent_version_after_a_dispute_wide_edit_is_package_already_received(env):
    c, db, token, fake = env
    v1 = approved(c, db, token, STATEMENT)
    seed_confirmed(db, v1)
    edit_tracking_dispute_wide(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v1}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert res.status_code == 409 and "Package already received" in res.text and "Not approved" not in res.text
    assert posts(fake) == [] and "pre_send_get" not in fake.calls_log      # refused before any PayPal call


# --- the stored result keeps its read-back time
def test_the_stored_result_page_keeps_the_readback_time_after_a_dispute_wide_edit(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, ["label.pdf"])]
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    url = r.headers["location"]
    first = c.get(url)
    assert state(parse(first.text), "readback-confirmed") is not None
    stored = [row for row in _stored_rows(db)]
    assert stored and stored[0]["confirmed_at"]                                # the time is stored with the outcome
    edit_tracking_dispute_wide(c, token)
    again = c.get(url)
    dom = parse(again.text)
    assert state(dom, "readback-confirmed") is not None and state(dom, "readback-time-missing") is None
    assert "not available" not in again.text


def _stored_rows(db):
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM send_results ORDER BY created_at")]
    finally:
        conn.close()


# --- "In PayPal" by document, never by name
def document_rows(html):
    dom = parse(html)
    out = []
    for row in dom.find_all("tr"):
        cells = {c.attrs.get("data-label"): c for c in row.find_all("td") if c.attrs.get("data-label")}
        if "In PayPal" in cells:
            out.append({"file": cells["File"].text().split(" · ")[0], "in_paypal": cells["In PayPal"].text(),
                        "forms": [f.attrs.get("action", "") for f in row.find_all("form")]})
    return out


def test_two_documents_with_the_same_name_are_not_both_shown_as_sent(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    sent_doc = add_file(c, db, token, pkg, "image.jpg", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    before = {d["id"] for d in repo.list_documents(db, DID)}
    assert upload(c, token, "image.jpg", LABEL_B).status_code == 303                  # never attached, never sent
    other_doc = next(d["id"] for d in repo.list_documents(db, DID) if d["id"] not in before)
    rows = document_rows(c.get(f"/disputes/{DID}/workspace/prepare").text)
    assert len(rows) == 2
    sent_row = next(r for r in rows if r["in_paypal"].startswith("Sent to PayPal"))
    other_row = next(r for r in rows if r["in_paypal"] == "Not sent")
    assert not sent_row["forms"] and any(f"/documents/{other_doc}/exclude" in f for f in other_row["forms"])
    assert sent_doc != other_doc and len(rows) == 2


def test_a_legacy_attempt_without_shas_marks_only_the_documents_attached_to_the_sent_package(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "image.jpg", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg, with_shas=False)
    assert upload(c, token, "image.jpg", LABEL_B).status_code == 303
    rows = document_rows(c.get(f"/disputes/{DID}/workspace/prepare").text)
    assert sorted(r["in_paypal"].startswith("Sent to PayPal") for r in rows) == [False, True]


# --- the print view says "sent" only for a confirmed send
@pytest.fixture
def env_print(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_FEATURE_PACKAGE_PRINT", "true")
    return send_tests._client(None, monkeypatch)


def excluded_attached(c, db, token):
    pkg = draft(c, db, token, STATEMENT)
    doc = add_file(c, db, token, pkg, "photo.pdf", LABEL_A)
    r = c.post(f"/disputes/{DID}/documents/{doc}/exclude", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and repo.get_document(db, doc)["excluded"] is True
    return pkg, doc


def exhibit_note(html):
    return state(parse(html), "excluded-sent")


def test_the_print_view_of_a_never_sent_package_does_not_say_the_file_was_sent(env_print):
    c, db, token, fake = env_print
    pkg, _ = excluded_attached(c, db, token)
    assert not repo.list_audit(db, DID) or not any(e["action"] == "send_attempt_registered" for e in repo.list_audit(db, DID))
    r = c.get(f"/disputes/{DID}/packages/{pkg}/print")
    assert r.status_code == 200
    note = exhibit_note(r.text)
    assert note is not None
    assert note.text() == "Attached to the response (it is sent with it), but left out of the checks."
    # the header says "Not sent to PayPal"; no other sentence may claim a send
    assert r.text.count("sent to PayPal") == r.text.count("Not sent to PayPal")


def test_the_print_view_says_sent_when_a_confirmed_send_carried_that_exact_file(env_print):
    c, db, token, fake = env_print
    pkg, doc = excluded_attached(c, db, token)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    r = c.get(f"/disputes/{DID}/packages/{pkg}/print")
    assert exhibit_note(r.text).text() == "Attached to the response and sent to PayPal, but left out of the checks."


def test_the_print_view_does_not_say_sent_for_another_file_with_the_same_name(env_print):
    c, db, token, fake = env_print
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "photo.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)                                       # v1 confirmed with photo.pdf (bytes A)
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    doc2 = add_file(c, db, token, v2, "photo.pdf", LABEL_B)       # same name, other bytes, never sent
    c.post(f"/disputes/{DID}/documents/{doc2}/exclude", data={"csrf_token": token}, follow_redirects=False)
    r = c.get(f"/disputes/{DID}/packages/{v2}/print")
    assert exhibit_note(r.text).text() == "Attached to the response (it is sent with it), but left out of the checks."


# --- message label needs message_listed
import json as _json


def _message_row(readback=None, action="message"):
    return {"status": "sent_confirmed", "action": action,
            "readback_json": _json.dumps(readback) if readback is not None else None}


def test_a_confirmed_message_row_says_paypal_lists_it_only_when_the_readback_says_so():
    listed = status.message_status(_message_row({"message_listed": True}))
    assert listed.label == "PayPal lists your message" and listed.kind == "ok"
    for readback in ({}, {"message_listed": False}, {"other": 1}, None):
        view = status.message_status(_message_row(readback))
        assert view.label == "Confirmed by reading the dispute back (message not checked)", readback
        assert view.kind == "info" and view.code == "sent_confirmed"
    assert status.message_status({"status": "sent_confirmed", "action": "message", "readback_json": "{broken"}).kind == "info"


def test_the_offer_row_label_does_not_depend_on_message_listed():
    view = status.message_status(_message_row({}, action="offer"))
    assert view.label == "PayPal no longer shows the refund-offer option" and view.kind == "ok"
