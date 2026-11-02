"""Status consistency and the
print page's way back. Helpers come from test_ux_rc41_fix_state.py. Offline; effects read from rows, requests and the DOM."""
import sqlite3

import pytest
from _dom import parse, state
from _hosted import set_test_csrf

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json
from copilot.db import repo
from copilot.web import status, timefmt

import test_routes_send as send_tests
from test_ux_rc41_fix_state import (LABEL_A, LABEL_B, STATEMENT, add_file, banner, confirmed_body, env_print,  # noqa: F401
                                    posts, request_input, seed_confirmed)
from test_ux_rc41_package import (DID, add_claims, approve_passive, approved, draft, env, new_package, page)  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


def seed_attempt(db, pkg, state_name, reason=None, request_id="req-v1"):
    rows = repo.list_attachments(db, pkg)
    names, shas = [r["filename"] for r in rows], [r["sha256"] for r in rows]
    req = request_input(db, pkg)
    repo.log_action(db, "system", "send_attempt_registered", DID,
                    {"package_id": pkg, "approval_sha256": "x" * 64, "request_id": request_id,
                     "attachments": names, "attachment_sha256": shas})
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(DID, sha256_json(req))
        store.write(DID, sha256_json(req), {"state": state_name, "request_id": request_id, "request_input": req,
                                            "attachment_filenames": names, "attachment_sha256": shas,
                                            **({"reason": reason} if reason else {})})
    finally:
        store.close()


def add_late_file(db, pkg, name="b.pdf"):
    doc = repo.insert_document(db, DID, name, "application/pdf", 10, "b" * 64, "x", True)
    repo.attach_document(db, pkg, doc)                    # past the route guard, as a legacy row would be
    return doc


def panel(html, ident="next-step"):
    node = parse(html).find(id=ident)
    return node, (node.text() if node is not None else "")


def test_a_confirmed_send_with_a_file_added_later_has_its_own_state_on_both_pages(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    add_late_file(db, pkg, "b.pdf")
    node, text = panel(page(c, pkg).text)
    assert node.attrs["data-next-step"] == "later_file"
    assert "b.pdf was added afterwards and is not in PayPal" in text
    assert "Send attempted" not in text and "has not confirmed it yet" not in text
    chip = parse(page(c, pkg).text).find(data_state="package-chip").text()
    assert "a file was added afterwards" in chip
    node, text = panel(c.get(f"/disputes/{DID}").text)
    assert node.attrs["data-next-step"] == "later_file"
    assert "b.pdf was added afterwards" in text and "cannot tell" not in text and "Send attempted" not in text
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    _, box = banner(c.get(r.headers["location"]).text)
    assert box.attrs.get("data-state") == "later-file"                         # The check words it from the same state
    assert posts(fake) == []


def test_a_true_partial_names_the_missing_file_and_is_not_called_unknown(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    html = page(c, pkg).text
    dom = parse(html)
    partial = dom.find(data_state="partial")
    assert partial is not None and "When Proofie checked, PayPal showed your statements without b.pdf." in partial.text()
    assert "We do not know whether PayPal has this evidence" not in html
    assert "When Proofie checked, PayPal showed your statements without b.pdf" in panel(html)[1]
    assert "When Proofie checked, PayPal showed your statements without the file b.pdf" in panel(c.get(f"/disputes/{DID}").text)[1]


def test_a_package_page_shows_no_send_form_when_paypal_does_not_list_provide_evidence(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    dom = parse(page(c, pkg).text)
    assert [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]     # control: offered now
    fake.detail_body = send_tests.NO_LINK
    html = page(c, pkg).text
    dom = parse(html)
    assert dom.find(id="next-step").attrs["data-next-step"] == "no_action"
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]
    assert "PayPal is not asking for evidence on this dispute now." in html


def test_the_used_for_the_send_time_is_the_send_time_and_the_change_note_comes_from_the_audit_flag(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    seed_confirmed(db, pkg)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE approvals SET approved_at='2026-01-02T03:04:05+00:00' WHERE package_id=?", (pkg,))
    conn.commit()
    conn.close()
    record = status.send_record(db, DID, repo.get_package(db, pkg))
    assert record.sent_at and record.changed_after_sending is False
    note = state(parse(page(c, pkg).text), "approval-used")
    assert note is not None and timefmt.display(record.sent_at) in note.text()
    assert "2 Jan 2026" not in note.text() and "A source changed after sending" not in note.text()
    repo.log_action(db, "system", "send_confirmed", DID, {"package_id": pkg, "changed_after_sending": True})
    note = state(parse(page(c, pkg).text), "approval-used")
    assert "A source changed after sending; nothing was re-sent." in note.text()


def test_statement_edits_are_refused_for_a_partial_version_and_change_nothing(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT, "Second statement.")
    add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    seed_attempt(db, pkg, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    before = [(x["id"], x["text"]) for x in repo.list_claims(db, pkg)]
    base = f"/disputes/{DID}/packages/{pkg}"
    cid = before[0][0]
    for url, data in ((f"{base}/claims/{cid}", {"text": "Different words now."}), (f"{base}/claims/{cid}/remove", {}),
                      (f"{base}/claims", {"text": "One more."})):
        r = c.post(url, data={"csrf_token": token, **data}, follow_redirects=False)
        assert r.status_code == 409, (url, r.status_code)
    assert [(x["id"], x["text"]) for x in repo.list_claims(db, pkg)] == before


def test_a_statement_added_after_removals_gets_a_new_index_and_keeps_its_place(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, "A.", "B.", "C.", "D.")
    base = f"/disputes/{DID}/packages/{pkg}"
    ids = [x["id"] for x in repo.list_claims(db, pkg)]
    for cid in ids[:2]:
        assert c.post(f"{base}/claims/{cid}/remove", data={"csrf_token": token}, follow_redirects=False).status_code == 303
    assert c.post(f"{base}/claims", data={"csrf_token": token, "text": "E."}, follow_redirects=False).status_code == 303
    claims = repo.list_claims(db, pkg)
    assert [x["text"] for x in claims] == ["C.", "D.", "E."]
    assert len({x["claim_index"] for x in claims}) == 3
    assert request_input(db, pkg)["evidences"][0]["notes"].endswith("C. D. E.")


def test_an_attached_but_excluded_file_is_in_the_posted_multipart_and_in_the_attempt(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    doc = add_file(c, db, token, pkg, "photo.pdf", LABEL_A)
    assert c.post(f"/disputes/{DID}/documents/{doc}/exclude", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303
    assert repo.get_document(db, doc)["excluded"] is True
    approve_passive(c, token, pkg)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, ["photo.pdf"])]
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and len(posts(fake)) == 1
    parts = posts(fake)[0]["multipart"]
    assert "photo.pdf" in [part[1][0] for part in parts if part[1][0]]               # the file really goes out
    store = SQLiteAttemptStore(db)
    try:
        attempt = store.list_by_dispute(DID)[0]
    finally:
        store.close()
    assert attempt["attachment_filenames"] == ["photo.pdf"] and attempt["state"] == "confirmed"


def test_the_statement_error_summary_links_to_the_textarea_itself(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    base = f"/disputes/{DID}/packages/{pkg}"
    cid = repo.list_claims(db, pkg)[0]["id"]
    for url, field in ((f"{base}/claims", "field-new-claim"), (f"{base}/claims/{cid}", f"field-claim-{cid}")):
        set_test_csrf(c, token)
        r = c.post(url, data={"csrf_token": token, "text": "  "}, follow_redirects=False)
        assert r.status_code == 400
        dom = parse(r.text)
        href = dom.find("section", class_="error-summary").find("a").attrs["href"]
        assert href == "#" + field
        target = dom.find(id=field)
        assert target is not None and target.tag == "textarea" and target.attrs.get("aria-invalid") == "true"
        assert len(dom.find_all(id=field)) == 1


def test_the_print_page_has_a_screen_only_link_back_to_the_package(env_print):
    c, db, token, fake = env_print
    pkg = draft(c, db, token, STATEMENT)
    html = c.get(f"/disputes/{DID}/packages/{pkg}/print").text
    dom = parse(html)
    link = dom.find("a", href=f"/disputes/{DID}/packages/{pkg}")
    assert link is not None and link.parent.attrs.get("class") == "screen-only"
    assert "@media print { .screen-only { display: none; } }" in html
