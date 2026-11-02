"""The later_file state (a confirmed send, then a file attached afterwards) and the
"same statements, other files" refusal must read the same on every page, route and print line.

Offline only: fake PayPal clients, a temp database per test (conftest.py). Effects are read from stored rows, the requests
the fake saw and the rendered DOM. Helpers come from the round-1 test modules.
"""
import pytest
from _dom import parse, state
from _hosted import set_test_csrf

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json
from copilot.db import repo
from copilot.web import status

import test_routes_send as send_tests
from test_ux_rc41_fix_state import (LABEL_A, LABEL_B, OTHER, STATEMENT, add_file, banner, confirmed_body, document_rows,  # noqa: F401
                                    env_print, posts, request_input, result_page, seed_confirmed,
                                    two_versions_same_statements)
from test_ux_rc41_fix_state_status import add_late_file, panel, seed_attempt
from test_ux_rc41_package import (DID, add_claims, approve_passive, approved, draft, env, new_package, page)  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


def sent_with_late_file(env, late="b.pdf"):
    """v1 confirmed with a.pdf; `late` is attached afterwards (past the route guard, as a legacy row would be)."""
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    add_late_file(db, pkg, late)
    return pkg


def task(html, key):
    return parse(html).find("tr", data_task=key)


# ---- the guided rows of a confirmed send with a late file ---
def test_shf1_the_guided_rows_of_a_confirmed_send_with_a_late_file_are_done_when_paypal_asks_for_nothing(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env)
    fake.detail_body = send_tests.NO_LINK
    html = c.get(f"/disputes/{DID}").text
    assert status.effective_state(status.send_record(db, DID, repo.get_package(db, pkg))) == "later_file"
    approve, send = task(html, "approve"), task(html, "send")
    assert approve.attrs["data-status"] == "done" and send.attrs["data-status"] == "done"
    assert "Not started" not in approve.text() and "Cannot start yet" not in send.text()
    assert f"/packages/{pkg}" in send.find("a", class_="task-link").attrs["href"]


def test_shf1_the_rows_reopen_for_a_new_version_only_when_paypal_lists_provide_evidence(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env)
    html = c.get(f"/disputes/{DID}").text                                     # the default read lists provide-evidence
    approve, send = task(html, "approve"), task(html, "send")
    assert send.attrs["data-status"] == "done" and "A new version needs its own send" in send.text()
    assert approve.attrs["data-status"] == "blocked" and "prepare a new package version" in approve.text()
    assert "/active-approval" not in html and f"/packages/{pkg}/approve" not in html     # no link back to v1's approval


def test_shf1_a_late_file_reads_exactly_like_a_plain_confirmed_send_in_the_rows(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env)
    late = [(task(c.get(f"/disputes/{DID}").text, k).attrs["data-status"]) for k in ("upload", "prepare", "approve", "send")]
    db_doc = [d for d in repo.list_attachments(db, pkg) if d["filename"] == "b.pdf"][0]
    repo.detach_document(db, pkg, db_doc["document_id"])
    plain = [(task(c.get(f"/disputes/{DID}").text, k).attrs["data-status"]) for k in ("upload", "prepare", "approve", "send")]
    assert late == plain


# ---- check again on a confirmed send with a late file names ---
def test_fss1_check_again_on_a_confirmed_send_with_a_late_file_names_the_file_and_never_says_received(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env)
    for _ in range(2):                                                          # first and second click read the same
        r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
        res = result_page(c, r)
        dom, box = banner(res.text)
        text = dom.find("main").text() if dom.find("main") else dom.text()
        assert box.attrs.get("data-state") == "later-file" and "banner-warn" in box.attrs.get("class", "")
        assert "b.pdf was added afterwards and is not in PayPal" in text
        assert "Received by PayPal" not in text and "Already recorded as received" not in text
        assert "What happens next" not in text
    assert posts(fake) == []


def test_fss1_a_replaced_file_after_a_timed_out_send_is_not_called_received(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    doc_a = add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")                                   # the POST timed out
    fake.recovery_get_body = confirmed_body(db, pkg, ["label.pdf"])              # PayPal lists label.pdf
    r = c.post(f"/disputes/{DID}/packages/{pkg}/detach/{doc_a}", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    add_file(c, db, token, pkg, "label.pdf", LABEL_B)                            # corrected bytes, same name
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    dom, box = banner(res.text)
    assert "Received by PayPal" not in dom.text() and "Already recorded as received" not in dom.text()
    assert box.attrs.get("data-state") == "later-file"
    # The two files of one name are told apart (was: "label.pdf was added afterwards" ... "label.pdf is in PayPal")
    assert "The label.pdf attached now was added afterwards and is not in PayPal" in dom.text()
    assert "The earlier label.pdf is in PayPal and no longer attached here." in dom.text()
    attempt = SQLiteAttemptStore(db).list_by_dispute(DID)[0]
    assert attempt["state"] == "confirmed"                                       # PayPal's read confirmed the old file
    assert posts(fake) == []


# ---- edit forms --------------------------------------------
def edit_forms(html, pkg):
    base = f"/disputes/{DID}/packages/{pkg}"
    return [f.attrs["action"] for f in parse(html).find_all("form")
            if f.attrs.get("action", "").startswith((f"{base}/claims", f"{base}/detach", f"{base}/attach"))]


def test_shf3_a_late_file_version_shows_no_edit_form_and_a_read_only_what_was_sent(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env)
    html = page(c, pkg).text
    assert edit_forms(html, pkg) == []
    section = parse(html).find(id="what-was-sent")
    assert section is not None
    text = section.text()
    assert "a.pdf" in text and "b.pdf" not in text                                  # what was sent, not what is attached now
    assert STATEMENT in text and "DEMO-FDX-000001" in text
    # the server agrees with the page: every one of those writes is refused
    cid = repo.list_claims(db, pkg)[0]["id"]
    set_test_csrf(c, token)
    assert c.post(f"/disputes/{DID}/packages/{pkg}/claims/{cid}", data={"csrf_token": token, "text": "x"},
                  follow_redirects=False).status_code == 409


def test_shf3_a_partial_version_shows_no_edit_form_and_a_read_only_what_was_sent(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    html = page(c, pkg).text
    assert edit_forms(html, pkg) == []
    section = parse(html).find(id="what-was-sent")
    assert section is not None and "b.pdf" in section.text() and STATEMENT in section.text()
    assert "Send attempted - PayPal has not confirmed it yet" not in html          # A true partial is not "unknown"
    assert parse(html).find(id="next-step").attrs["data-next-step"] == "pending"


# ---- a same name file in paypal does not confirm a package whose ---
def test_fss2_a_same_name_file_in_paypal_does_not_confirm_a_package_whose_file_has_other_bytes(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    doc_a = add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")                                   # the first POST timed out
    old_sha = repo.list_attachments(db, pkg)[0]["sha256"]
    assert c.post(f"/disputes/{DID}/packages/{pkg}/detach/{doc_a}", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303
    add_file(c, db, token, pkg, "label.pdf", LABEL_B)                            # corrected bytes, same name
    approve_passive(c, token, pkg)
    assert repo.get_latest_valid_approval(db, pkg)
    # PayPal's read at send time lists a file called label.pdf (the old bytes), and the recovery read shows nothing yet
    fake.pre_send_get_body = {**send_tests.WAITING, "evidences": confirmed_body(db, pkg, ["label.pdf"])["evidences"]}
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert posts(fake) == []                                                    # nothing was sent again
    store = SQLiteAttemptStore(db)
    try:
        attempt = store.list_by_dispute(DID)[0]
    finally:
        store.close()
    assert attempt["state"] == "sent_unconfirmed" and attempt["attachment_sha256"] == [old_sha]   # never confirmed
    dom, _ = banner(res.text)
    assert "PayPal already has this" not in dom.text() and "Received by PayPal" not in dom.text()
    assert "label.pdf" in dom.text()                                              # the unsent file is named


# ---- the same statements refusal for an unconfirmed earlier attempt says paypal may ---
def test_fss3_the_same_statements_refusal_for_an_unconfirmed_earlier_attempt_says_paypal_may_have_them(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", OTHER)
    approve_passive(c, token, v2)
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert res.status_code == 409 and posts(fake) == []
    dom, _ = banner(res.text)
    text = dom.text()
    assert "Same statements, other files" in text and "b.pdf" in text
    assert "PayPal may" in text and "Check again" in text
    assert "PayPal already holds these statements" not in text and "change a statement" not in text


def test_fss3_the_refusal_for_a_confirmed_earlier_attempt_says_it_cannot_send_with_another_file(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)])
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    dom, _ = banner(result_page(c, r).text)
    # the read lists no seller evidence: what PayPal held is told as what Proofie saw when it checked, and the way out stays
    assert "When Proofie checked, PayPal held these statements" in dom.text() and "cannot send them again with another file" in dom.text()
    assert "already holds" not in dom.text() and "Change a statement to make a new submission." in dom.text()
    assert "PayPal already lists" not in dom.text()


# --- print
def late_real_file(c, db, token, pkg, name, data):
    """A real uploaded file attached past the route guard (as a legacy row would be)."""
    before = {d["id"] for d in repo.list_documents(db, DID)}
    assert upload_doc(c, token, name, data).status_code == 303
    doc = next(d["id"] for d in repo.list_documents(db, DID) if d["id"] not in before)
    repo.attach_document(db, pkg, doc)
    return doc


def upload_doc(c, token, name, data):
    return c.post(f"/disputes/{DID}/documents", files={"file": (name, data, "application/octet-stream")},
                  data={"csrf_token": token}, follow_redirects=False)


def late_notes(html):
    return [n.text() for n in parse(html).find_all(data_state="added-after-send")]


def test_fss5_the_print_view_says_a_file_added_after_a_confirmed_send_is_not_in_paypal(env_print):
    c, db, token, fake = env_print
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    doc_b = late_real_file(c, db, token, pkg, "b.pdf", LABEL_B)
    assert c.post(f"/disputes/{DID}/documents/{doc_b}/exclude", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303                      # left out of the checks, still attached
    html = c.get(f"/disputes/{DID}/packages/{pkg}/print").text
    assert late_notes(html) == ["Added after the response was sent; not in PayPal."]
    assert "it is sent with it" not in html                                       # the future-tense line is false here
    assert "sent to PayPal, but left out of the checks" not in html


def test_shf9_a_file_added_after_a_partial_send_reads_the_same_in_print(env_print):
    c, db, token, fake = env_print
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    late_real_file(c, db, token, pkg, "c.pdf", OTHER)
    html = c.get(f"/disputes/{DID}/packages/{pkg}/print").text
    assert late_notes(html) == ["Added after the response was sent; not in PayPal."]    # c.pdf only, not b.pdf


def test_the_print_view_of_a_version_with_no_send_has_no_added_after_note(env_print):
    c, db, token, fake = env_print
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    assert late_notes(c.get(f"/disputes/{DID}/packages/{pkg}/print").text) == []


# ---- a conflict note goes last after statements were removed ---
def test_fss6_a_conflict_note_goes_last_after_statements_were_removed(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, "A.", "B.", "C.", "D.")
    base = f"/disputes/{DID}/packages/{pkg}"
    for cid in [x["id"] for x in repo.list_claims(db, pkg)][:2]:
        assert c.post(f"{base}/claims/{cid}/remove", data={"csrf_token": token}, follow_redirects=False).status_code == 303
    r = c.post(f"{base}/approve", data={"csrf_token": token, "conflict_note": "NOTE.",
                                         "sufficiency_override": "Reviewed."}, follow_redirects=False)
    assert r.status_code == 303
    claims = repo.list_claims(db, pkg)
    assert [x["text"] for x in claims] == ["C.", "D.", "NOTE."]
    assert len({x["claim_index"] for x in claims}) == 3
    assert request_input(db, pkg)["evidences"][0]["notes"].endswith("C. D. NOTE.")


# ---- a version whose statements paypal holds has no send button on either ---
def test_shf4_a_version_whose_statements_paypal_holds_has_no_send_button_on_either_page(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)])
    dom = parse(page(c, v2).text)
    node = dom.find(id="next-step")
    assert node.attrs["data-next-step"] == "same_statements" and "b.pdf" in node.text()
    assert "Change a statement" in node.text()          # PayPal's read lists no seller evidence: a changed statement can be sent
    assert "PayPal already lists evidence" not in node.text()
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]
    dom = parse(c.get(f"/disputes/{DID}").text)
    node = dom.find(id="next-step")
    assert node.attrs["data-next-step"] == "same_statements" and "Nothing has reached PayPal yet" not in node.text()
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]
    assert posts(fake) == []


def test_shf4_the_collision_helper_reports_an_unconfirmed_earlier_attempt(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", OTHER)
    found = status.statement_collision(db, DID, repo.get_package(db, v2))
    assert found is not None and found.state == "attempted" and found.unsent == ("b.pdf",)
    assert status.statement_collision(db, DID, repo.get_package(db, v1)) is None


# ---- a file removed after the send is in paypal not added afterwards ---
def test_shf5_a_file_removed_after_the_send_is_in_paypal_not_added_afterwards(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    doc_b = add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")
    assert c.post(f"/disputes/{DID}/packages/{pkg}/detach/{doc_b}", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303
    fake.recovery_get_body = confirmed_body(db, pkg, ["a.pdf", "b.pdf"])          # PayPal lists both files
    dispute_html = c.get(f"/disputes/{DID}/workspace/prepare").text                # the dispute view reconciles
    record = status.send_record(db, DID, repo.get_package(db, pkg))
    assert record.state == "confirmed" and record.removed_files == ("b.pdf",) and record.missing_files == ()
    text = page(c, pkg).text
    assert "b.pdf is in PayPal and no longer attached here." in parse(text).text()
    assert "added afterwards" not in text
    rows = {r["file"]: r["in_paypal"] for r in document_rows(dispute_html)}
    assert rows["b.pdf"].startswith("Sent to PayPal") and rows["a.pdf"].startswith("Sent to PayPal")


def test_shf5_attach_and_detach_are_refused_on_a_sent_version(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env)
    other = repo.insert_document(db, DID, "c.pdf", "application/pdf", 10, "c" * 64, "x", True)
    a_doc = [r["document_id"] for r in repo.list_attachments(db, pkg) if r["filename"] == "a.pdf"][0]
    before = repo.list_attachments(db, pkg)
    base = f"/disputes/{DID}/packages/{pkg}"
    for url in (f"{base}/attach/{other}", f"{base}/detach/{a_doc}"):
        assert c.post(url, data={"csrf_token": token}, follow_redirects=False).status_code == 409
    assert repo.list_attachments(db, pkg) == before


# ---- dispute panel -----------------------------------------
def dispute_panel(c):
    return parse(c.get(f"/disputes/{DID}").text).find(id="next-step")


def test_shf7_the_prepare_a_new_version_note_only_when_paypal_lists_provide_evidence(env):
    c, db, token, fake = env
    sent_with_late_file(env)
    assert "Prepare a new version to send it" in dispute_panel(c).text()
    fake.detail_body = send_tests.NO_LINK
    node = dispute_panel(c)
    assert node.attrs["data-next-step"] == "later_file"
    assert "Prepare a new version" not in node.text() and "PayPal is not asking for evidence now." in node.text()


def test_mcs6_two_late_files_read_in_the_plural_on_every_page_and_never_say_again(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env, "b.pdf")
    add_late_file(db, pkg, "c.pdf")
    fake.detail_body = send_tests.NO_LINK
    pages = {"dispute": c.get(f"/disputes/{DID}").text, "package": page(c, pkg).text}
    for name, html in pages.items():
        text = parse(html).text()
        assert "b.pdf, c.pdf were added afterwards and are not in PayPal" in text, name
        assert "b.pdf, c.pdf was added" not in text and "will not send it again" not in text, name
        assert "Proofie will not send them on its own." in text, name


def test_mcs6_one_late_file_says_it(env):
    c, db, token, fake = env
    pkg = sent_with_late_file(env, "b.pdf")
    text = parse(page(c, pkg).text).text()
    assert "b.pdf was added afterwards and is not in PayPal. Proofie will not send it on its own." in text


# ---- every write to a confirmed version with a late file is refused ---
def test_di6_every_write_to_a_confirmed_version_with_a_late_file_is_refused_and_changes_nothing(env):
    from test_ux_rc41_package import snapshot
    c, db, token, fake = env
    pkg = sent_with_late_file(env)
    before = snapshot(db, pkg)
    base = f"/disputes/{DID}/packages/{pkg}"
    cid = repo.list_claims(db, pkg)[0]["id"]
    for url, data in ((f"{base}/claims", {"text": "More."}), (f"{base}/claims/{cid}", {"text": "Other."}),
                      (f"{base}/claims/{cid}/remove", {}), (f"{base}/approve", {"sufficiency_override": "ok"})):
        r = c.post(url, data={"csrf_token": token, **data}, follow_redirects=False)
        assert r.status_code == 409, (url, r.status_code)
    assert snapshot(db, pkg) == before


# ---- the policy required signature select is aria required ---
def test_mu10_the_policy_required_signature_select_is_aria_required(env):
    c, db, token, fake = env
    fake.detail_body = {**send_tests.WAITING, "dispute_amount": {"currency_code": "USD", "value": "9999.00"}}
    pkg = draft(c, db, token, STATEMENT)
    dom = parse(page(c, pkg).text)
    select = dom.find("select", id="field-signature")
    assert select is not None
    mark = select.parent.find("strong", class_="field-mark")
    assert mark is not None and "Required" in mark.text(), "the dispute did not make the signature required"
    assert select.attrs.get("aria-required") == "true"


# ------------------------------------------------------------------------------------------------ integration
RECEIVED_NOTE = "Shown exactly as PayPal received it. Your browser will not translate this part."


@pytest.mark.parametrize("late", [None, "b.pdf"])
def test_integration_what_was_sent_on_a_sent_package_says_paypal_received_it(env, late):
    """Integration: the read-only "What was sent" of a version PayPal holds (confirmed, and confirmed with a file added
    afterwards) uses exact_content(sent=true), so its note is in the past tense ."""
    c, db, token, fake = env
    if late:
        pkg = sent_with_late_file(env, late)
    else:
        pkg = draft(c, db, token, STATEMENT)
        add_file(c, db, token, pkg, "a.pdf", LABEL_A)
        approve_passive(c, token, pkg)
        seed_confirmed(db, pkg)
    section = parse(page(c, pkg).text).find(id="what-was-sent")
    assert section is not None and STATEMENT in section.text()
    assert section.find("p", class_="exact-note").text() == RECEIVED_NOTE
    assert "will receive" not in section.text()
