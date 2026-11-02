"""The way out of the "same statements, other files" refusal must work, a removed
file never leads to a "confirmed" write that claims files PayPal did not list, and the partial / later-file wording is
one wording on every page.

Offline only: fake PayPal clients, a temp database per test (conftest.py). Effects are read from stored rows, the requests
the fake saw and the rendered DOM. Helpers come from the earlier round modules.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from _dom import parse

from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.db import repo

import test_routes_send as send_tests
from test_ux_rc41_fix_state import (LABEL_A, LABEL_B, OTHER, STATEMENT, add_file, banner, confirmed_body,  # noqa: F401
                                    posts, result_page)
from test_ux_rc41_fix_state_status import add_late_file, panel, seed_attempt  # noqa: F401
from test_ux_rc41_package import DID, add_claims, approve_passive, draft, env, new_package, page  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


def age_attempts(db, seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 400):
    """Makes every stored attempt old enough that it cannot be a POST still in flight."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S%z")
    with sqlite3.connect(str(db)) as raw:
        raw.execute("UPDATE send_attempts SET ts = ? WHERE dispute_id = ?", (old, DID))


def form_token(form):
    return next(i.attrs["value"] for i in form.find_all("input") if i.attrs.get("name") == "csrf_token")


def attempts(db):
    store = SQLiteAttemptStore(db)
    try:
        return store.list_by_dispute(DID)
    finally:
        store.close()


def v1_unconfirmed_v2_other_files(env, v1_files=(("a.pdf", LABEL_A),), v2_files=(("a.pdf", LABEL_A), ("b.pdf", OTHER))):
    """v1 was sent and its answer never came (unconfirmed); v2 has the same statements with other files, approved."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    for name, data in v1_files:
        add_file(c, db, token, v1, name, data)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    for name, data in v2_files:
        add_file(c, db, token, v2, name, data)
    approve_passive(c, token, v2)
    assert repo.get_latest_valid_approval(db, v2)
    return v1, v2


# ---- the mark is refused to abandon a confirmed same sha attempt ---
def test_ssr21_the_mark_is_refused_to_abandon_a_confirmed_same_sha_attempt(env):
    c, db, token, fake = env
    from test_ux_rc41_fix_state import two_versions_same_statements
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)])
    age_attempts(db)
    r = c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received", data={"csrf_token": token, "confirmed": "on"})
    assert "Nothing was waiting" in r.text
    assert [a["state"] for a in attempts(db)] == ["confirmed"]


def test_ssr21_an_exact_same_files_package_is_still_not_sendable_after_the_mark(env):
    """A second version with the same statements AND the same files is the same submission: abandoning the earlier row
    must not turn it into a new send (the existing 'edit it' answer stays)."""
    c, db, token, fake = env
    v1, v2 = v1_unconfirmed_v2_other_files(env, v2_files=(("a.pdf", LABEL_A),))
    age_attempts(db)
    c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received", data={"csrf_token": token, "confirmed": "on"})
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    assert posts(fake) == []


# ---- removed file version ----------------------------------
def removed_file_version(env):
    """v1 carried a.pdf and b.pdf and its POST timed out (unconfirmed); b.pdf is detached and v1 approved again."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    doc_b = add_file(c, db, token, v1, "b.pdf", LABEL_B)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    assert c.post(f"/disputes/{DID}/packages/{v1}/detach/{doc_b}", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303
    approve_passive(c, token, v1)
    assert repo.get_latest_valid_approval(db, v1)
    return v1


def test_gsh2_a_removed_file_that_paypal_does_not_list_is_never_written_as_confirmed(env):
    c, db, token, fake = env
    v1 = removed_file_version(env)
    fake.pre_send_get_body = {**send_tests.WAITING, **confirmed_body(db, v1, ["a.pdf"])}   # PayPal lists a.pdf only
    r = c.post(f"/disputes/{DID}/packages/{v1}/send", data={"csrf_token": token}, follow_redirects=False)
    assert posts(fake) == []                                                    # nothing is sent again
    row = attempts(db)[0]
    assert row["state"] == "sent_unconfirmed" and row["reason"] == "missing_attachment:b.pdf"
    assert row["attachment_filenames"] == ["a.pdf", "b.pdf"] and len(row["attachment_sha256"]) == 2   # not overwritten
    html = page(c, v1).text
    text = parse(html).text()
    assert "b.pdf is in PayPal" not in text and "Received by PayPal" not in text
    assert "b.pdf was sent and is no longer attached here." in text
    from copilot.web import status
    record = status.send_record(db, DID, repo.get_package(db, v1))
    assert record.state == "partial" and "b.pdf" in record.missing_files and "b.pdf" in record.removed_unlisted
    dispute = c.get(f"/disputes/{DID}").text
    rows = {x["file"]: x["in_paypal"] for x in __import__("test_ux_rc41_fix_state").document_rows(dispute)}
    assert not rows.get("b.pdf", "").startswith("Sent to PayPal")


def test_gsh2_a_removed_file_that_paypal_lists_confirms_without_rewriting_the_attempt_files(env):
    c, db, token, fake = env
    v1 = removed_file_version(env)
    fake.pre_send_get_body = {**send_tests.WAITING, **confirmed_body(db, v1, ["a.pdf", "b.pdf"])}
    r = c.post(f"/disputes/{DID}/packages/{v1}/send", data={"csrf_token": token}, follow_redirects=False)
    assert posts(fake) == []
    row = attempts(db)[0]
    assert row["state"] == "confirmed" and row["attachment_filenames"] == ["a.pdf", "b.pdf"]
    text = parse(page(c, v1).text).text()
    assert "b.pdf is in PayPal and no longer attached here." in text


# ---- the print fixture of the state tests --------
from test_ux_rc41_fix_state import env_print  # noqa: E402,F401


def test_shf9_a_file_the_partial_send_carried_but_paypal_does_not_list_reads_as_not_in_paypal_in_print(env_print):
    c, db, token, fake = env_print
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    doc_b = add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    assert c.post(f"/disputes/{DID}/documents/{doc_b}/exclude", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303                     # attached, left out of the checks
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed", reason="missing_attachment:b.pdf")  # PayPal lists a.pdf only
    html = c.get(f"/disputes/{DID}/packages/{pkg}/print").text
    notes = [n.text() for n in parse(html).find_all(data_state="sent-not-listed")]
    assert notes == ["Sent with the response; PayPal does not list it."]
    assert "it is sent with it" not in html and "sent to PayPal, but left out of the checks" not in html


# ---- a legacy name only attempt never claims a removed file ---
def test_gsh7_a_legacy_name_only_attempt_never_claims_a_removed_file_is_in_paypal(env):
    c, db, token, fake = env
    from test_ux_rc41_fix_state import seed_confirmed
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    doc_b = add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg, with_shas=False)                       # an attempt that predates the sha records
    repo.detach_document(db, pkg, doc_b)                           # a detach while it was unconfirmed
    text = parse(page(c, pkg).text).text()
    assert "b.pdf is in PayPal" not in text
    assert "b.pdf was sent and is no longer attached here." in text
    dispute = parse(c.get(f"/disputes/{DID}").text).text()
    assert "b.pdf is in PayPal" not in dispute


def test_gsh3_two_files_of_one_name_are_told_apart_in_the_word_helpers():
    from copilot.web import next_step
    from copilot.web.status import SendRecord
    record = SendRecord(state="confirmed", attachments_match=False, missing_files=("label.pdf",),
                        removed_files=("label.pdf",))
    assert next_step.later_file_text(record) == "The label.pdf attached now was added afterwards and is not in PayPal"
    assert next_step.removed_text(record) == "The earlier label.pdf is in PayPal and no longer attached here."
    plain = SendRecord(state="confirmed", attachments_match=False, missing_files=("b.pdf",), removed_files=("a.pdf",))
    assert next_step.later_file_text(plain) == "b.pdf was added afterwards and is not in PayPal"
    assert next_step.removed_text(plain) == "a.pdf is in PayPal and no longer attached here."


# ---- partial version ---------------------------------------
def partial_version(env, missing="a.pdf,b.pdf"):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed", reason="missing_attachment:" + missing)
    return pkg


def test_gsh8_the_partial_sentences_are_count_aware_on_every_page(env):
    c, db, token, fake = env
    pkg = partial_version(env)
    node, text = panel(page(c, pkg).text)
    assert "Statements in PayPal; files are missing." in text
    assert "When Proofie checked, PayPal showed your statements without a.pdf, b.pdf. Proofie will not send them again on its own." in text
    assert "a file is missing" not in text
    full = parse(page(c, pkg).text).text()
    assert "Proofie will not send it again" not in full
    node, text = panel(c.get(f"/disputes/{DID}").text)
    assert "Statements in PayPal; files are missing." in text
    assert "without the files a.pdf, b.pdf. Proofie will not send them again on its own." in text


def test_gsh8_one_missing_file_keeps_the_singular(env):
    c, db, token, fake = env
    pkg = partial_version(env, missing="b.pdf")
    node, text = panel(page(c, pkg).text)
    assert "Statements in PayPal; a file is missing." in text
    assert "When Proofie checked, PayPal showed your statements without b.pdf. Proofie will not send it again on its own." in text


def test_gsh9_a_newer_version_keeps_the_partial_wording_on_the_dispute_page(env):
    c, db, token, fake = env
    pkg = partial_version(env, missing="b.pdf")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    html = c.get(f"/disputes/{DID}").text
    node, text = panel(html)
    # an earlier version's partial send pauses the newer one: the same paused step as the package page
    assert node.attrs["data-next-step"] == "other_pending"
    assert "An earlier send reached PayPal without every file" in text and "Send attempted - PayPal has not confirmed" not in text
    summary = parse(html).text()
    assert "When Proofie checked, PayPal showed your statements without b.pdf." in summary
    assert "Package v1: send attempted" in summary and "PayPal has not confirmed it yet" not in summary


def test_gsh10_a_later_file_version_with_a_newer_version_has_the_newer_row_and_the_checked_after_note(env):
    c, db, token, fake = env
    from test_ux_rc41_fix2_state2 import sent_with_late_file
    pkg = sent_with_late_file(env)
    new_package(c, token)
    html = c.get(f"/disputes/{DID}").text
    assert "Package v2 (newer): Not sent." in parse(html).text()


# ---- check again on a true partial answers with the partial state ---
def test_gsh6_check_again_on_a_true_partial_answers_with_the_partial_state_and_names_the_files(env):
    c, db, token, fake = env
    pkg = partial_version(env)                                    # a.pdf and b.pdf are not listed by PayPal
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    dom, box = banner(res.text)
    assert box.attrs.get("data-state") == "partial"
    text = dom.text()
    assert "Statements in PayPal, file missing" in text
    assert "When Proofie checked, PayPal showed your statements without a.pdf, b.pdf. Proofie will not send them again on its own." in text
    assert "Still not confirmed by PayPal" not in text and "Result pending" not in text
    assert posts(fake) == []


# ---- the collision is known before approval and the send row is blocked ---
def test_gsh5_the_collision_is_known_before_approval_and_the_send_row_is_blocked(env):
    c, db, token, fake = env
    from test_ux_rc41_fix_state import seed_confirmed
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", OTHER)                      # NOT approved
    assert repo.get_latest_valid_approval(db, v2) is None
    node, text = panel(page(c, v2).text)
    assert node.attrs["data-next-step"] == "same_statements" and "Change a statement" in text   # nothing listed: a send is possible
    assert "PayPal already lists evidence" not in text
    node, text = panel(c.get(f"/disputes/{DID}").text)
    assert node.attrs["data-next-step"] == "same_statements"
    html = c.get(f"/disputes/{DID}").text
    row = parse(html).find("tr", data_task="send")
    assert row is not None and "Change a statement" in row.text() and "Not started" not in row.text()
    # approved: the same
    from _hosted import set_test_csrf
    set_test_csrf(c, token)                                          # the GETs above rotated the cookie
    approve_passive(c, token, v2)
    row = parse(c.get(f"/disputes/{DID}").text).find("tr", data_task="send")
    assert "Change a statement" in row.text() and "Not started" not in row.text()
    assert posts(fake) == []
