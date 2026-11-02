"""The send-core items, checked on effects.

EXPECTED (written before the first run; every test below is red on the code before the fix unless it says it is a guard):
  plural headline     : with two or more files missing the partial headline is plural ('files', 'attachments');
                        one missing file, or none named, keeps the singular. Guards: both titles map to ('warn', 'partial').
  name read-back      : the read-back counts names as a multiset: two wanted files of one name need two listed
                        documents of that name. A false 'partial' never re-sends; a false 'confirmed' is the harm.
  partial abandoned   : marking an earlier PARTIAL attempt abandoned stores ABANDONED_PARTIAL_REASON and audits
                        partial=True; an ordinary unconfirmed row keeps ABANDONED_REASON and an audit without it.
  PRG posts           : the mark answers 303 to a stored result page (no state change on GET, none on reload); a direct
                        POST on the package that owns the unconfirmed row says its own send is not confirmed.
  received-by format  : the banner prints the display format in Mexico City time, never a bare wall clock under a
                        fixed 'CDMX' label.
  first-person copy (routes_send line): 'Proofie could not check PayPal just now ...' (no 'We').
Every scenario asserts zero PayPal POSTs unless it is the one send of the scenario (a send is never repeated).
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _dom import parse, state

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config, mcp_server
from copilot.core import send_recovery, send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.disputes import PayPalError
from copilot.core.send import (PARTIAL_MESSAGE, PARTIAL_MESSAGE_MANY, sha256_json,
                               verify_after_with_attachments)
from copilot.db import repo
from copilot.web import routes_send

import test_routes_send as send_tests
from test_ux_rc41_fix3_state3 import age_attempts, attempts, partial_version, v1_unconfirmed_v2_other_files
from test_ux_rc41_fix_state import (LABEL_A, LABEL_B, OTHER, STATEMENT, add_file, banner, confirmed_body, posts,
                                    request_input, result_page, seed_confirmed)
from test_ux_rc41_fix_state_status import seed_attempt
from test_ux_rc41_package import DID, add_claims, approve_passive, draft, env, new_package, page  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]

DISPLAY_TIME = re.compile(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d{2} (?:am|pm) \(Mexico City time\)")


def headline(html):
    dom = parse(html)
    return dom.find("title").text(), dom.find("h1").text()


# ==== partial message is singular for none or one name and plural from ===
def test_partial_message_is_singular_for_none_or_one_name_and_plural_from_two():
    assert send_recovery.partial_message([]) == PARTIAL_MESSAGE
    assert send_recovery.partial_message(["a.pdf"]) == PARTIAL_MESSAGE
    assert send_recovery.partial_message(["a.pdf", "b.pdf"]) == PARTIAL_MESSAGE_MANY
    assert send_recovery.partial_message(("a.pdf", "b.pdf", "c.pdf")) == PARTIAL_MESSAGE_MANY
    assert PARTIAL_MESSAGE == "PayPal received the details but not the attached file — check the attachment."
    assert PARTIAL_MESSAGE_MANY == "PayPal received the details but not the attached files — check the attachments."
    assert send_recovery.PARTIAL_TITLES == (PARTIAL_MESSAGE, PARTIAL_MESSAGE_MANY)
    assert send_recovery.PARTIAL_MESSAGE == PARTIAL_MESSAGE      # one constant, imported, not copied


def test_both_partial_titles_map_to_the_partial_state():
    for title in send_recovery.PARTIAL_TITLES:
        assert routes_send._result_state(title, "x", 206) == ("warn", "partial")
        assert routes_send._result_state(title, "x", 200) == ("warn", "partial")


def two_file_package(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    return pkg


def test_gsh8_a_send_with_two_files_missing_has_the_plural_headline(env):
    c, db, token, fake = env
    pkg = two_file_package(env)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, [])]                # PayPal lists neither file
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert res.status_code == 206 and len(posts(fake)) == 1
    title, h1 = headline(res.text)
    assert title.startswith(PARTIAL_MESSAGE_MANY) and h1 == PARTIAL_MESSAGE_MANY
    assert "Received by PayPal" not in res.text
    dom, box = banner(res.text)
    assert box.attrs.get("data-state") == "partial"


def test_gsh8_a_send_with_one_file_missing_keeps_the_singular_headline(env):
    c, db, token, fake = env
    pkg = two_file_package(env)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, ["a.pdf"])]         # only b.pdf is missing
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    title, h1 = headline(res.text)
    assert h1 == PARTIAL_MESSAGE and title.startswith(PARTIAL_MESSAGE)
    assert len(posts(fake)) == 1


def test_gsh8_the_second_send_on_a_partial_attempt_answers_plural_without_a_second_post(env):
    """Site send_service claim-conflict branch: the attempt exists, PayPal lists the statements but no file."""
    c, db, token, fake = env
    pkg = partial_version(env)                                               # a.pdf and b.pdf, 'sent_unconfirmed'
    fake.pre_send_get_body = {**send_tests.WAITING, **confirmed_body(db, pkg, [])}
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert res.status_code == 206 and posts(fake) == []
    assert headline(res.text)[1] == PARTIAL_MESSAGE_MANY


def test_gsh8_check_again_on_a_partial_with_two_files_has_the_plural_headline(env):
    c, db, token, fake = env
    pkg = partial_version(env)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert headline(res.text)[1] == PARTIAL_MESSAGE_MANY and posts(fake) == []


def test_gsh8_check_again_on_a_partial_with_one_file_keeps_the_singular(env):
    c, db, token, fake = env
    pkg = partial_version(env, missing="b.pdf")
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    assert headline(result_page(c, r).text)[1] == PARTIAL_MESSAGE and posts(fake) == []


def test_gsh8_two_files_added_after_a_confirmed_send_have_the_plural_headline(env):
    """send_service branch 'the same package, files added after the confirmed attempt': a valid approval over the three
    real files exists (the files were attached past the route guard, as a legacy row would be)."""
    from test_ux_rc41_package import upload
    from copilot.core.send import approval_fingerprint_with_attachments
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    for name, data in (("b.pdf", LABEL_B), ("c.pdf", OTHER)):
        before = {d["id"] for d in repo.list_documents(db, DID)}
        assert upload(c, token, name, data).status_code == 303
        repo.attach_document(db, pkg, next(d["id"] for d in repo.list_documents(db, DID) if d["id"] not in before))
    fp = approval_fingerprint_with_attachments(DID, "provide_evidence", request_input(db, pkg),
                                               [a["sha256"] for a in repo.list_attachments(db, pkg)])
    repo.insert_approval(db, pkg, fp, "vendor")
    fake.pre_send_get_body = {**send_tests.WAITING, **confirmed_body(db, pkg, ["a.pdf"])}
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert posts(fake) == []
    assert res.status_code == 206 and headline(res.text)[1] == PARTIAL_MESSAGE_MANY


# ================================================================================ the file name read back from PayPal
REQ = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                      "evidence_info": {"tracking_info": [{"carrier_name": "MEX_ESTAFETA", "tracking_number": "DEMO1"}]},
                      "notes": "demo"}]}


def body(names):
    return {"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "demo",
                           "date": "2026-10-05", "documents": [{"name": n} for n in names]}]}


def test_two_same_name_files_are_not_confirmed_when_paypal_lists_the_name_once():
    result = verify_after_with_attachments(body(["label.pdf"]), REQ, ["label.pdf", "label.pdf"])
    assert result["content_ok"] is True and result["files_ok"] is False
    assert result["missing_files"] == ["label.pdf"]


def test_two_same_name_files_are_confirmed_when_paypal_lists_the_name_twice():
    result = verify_after_with_attachments(body(["label.pdf", "label.pdf"]), REQ, ["label.pdf", "label.pdf"])
    assert result["files_ok"] is True and result["missing_files"] == []


def test_one_file_is_unchanged_by_the_multiset_rule():
    assert verify_after_with_attachments(body(["label.pdf"]), REQ, ["label.pdf"])["files_ok"] is True
    # a name PayPal lists more often than we sent never makes a wanted file missing
    assert verify_after_with_attachments(body(["label.pdf", "label.pdf"]), REQ, ["label.pdf"])["files_ok"] is True
    gone = verify_after_with_attachments(body([]), REQ, ["label.pdf"])
    assert gone["files_ok"] is False and gone["missing_files"] == ["label.pdf"]


def test_mixed_names_report_only_the_surplus_occurrence_as_missing():
    result = verify_after_with_attachments(body(["label.pdf", "a.pdf"]), REQ, ["label.pdf", "a.pdf", "label.pdf"])
    assert result["files_ok"] is False and result["missing_files"] == ["label.pdf"]
    ok = verify_after_with_attachments(body(["a.pdf", "label.pdf", "label.pdf"]), REQ, ["label.pdf", "a.pdf", "label.pdf"])
    assert ok["files_ok"] is True and ok["missing_files"] == []
    assert ok["details"]["documents_present"] == ["a.pdf", "label.pdf", "label.pdf"]   # what PayPal listed, sorted


class _OneGet:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def call(self, method, path, op, **kw):
        self.calls.append((method, op))
        return self.raw


def _seed_two_same_name(store):
    req = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "demo",
                          "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}}]}
    sha = sha256_json(req)
    assert store.claim(DID, sha)
    store.write(DID, sha, {"state": "sent_unconfirmed", "request_id": "req-1", "request_input": req,
                           "attachment_filenames": ["label.pdf", "label.pdf"]})
    return sha


def test_recovery_never_confirms_two_same_name_files_from_one_listed_name():
    db = config.db_path()
    store = SQLiteAttemptStore(db)
    sha = _seed_two_same_name(store)
    client = _OneGet(body(["label.pdf"]))
    pending = send_recovery.recover_dispute(client, store, DID, db)
    assert [p["package_sha256"] for p in pending] == [sha]
    assert store.read(DID, sha)["state"] == "sent_unconfirmed"               # not 'confirmed'
    assert all(m == "GET" for m, _ in client.calls)
    client = _OneGet(body(["label.pdf", "label.pdf"]))
    assert send_recovery.recover_dispute(client, store, DID, db) == []
    assert store.read(DID, sha)["state"] == "confirmed"
    store.close()


# ==== marking an earlier partial attempt stores the partial reason and audits partial ===
def test_marking_an_earlier_partial_attempt_stores_the_partial_reason_and_audits_partial(env):
    c, db, token, fake = env
    v1 = partial_version(env, missing="b.pdf")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    age_attempts(db)
    r = c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received",
               data={"csrf_token": token, "confirmed": "on"}, follow_redirects=False)
    assert r.status_code == 303 and posts(fake) == []
    row = attempts(db)[0]
    assert row["state"] == "abandoned" and row["reason"] == send_service.ABANDONED_PARTIAL_REASON
    assert row["reason"] != send_service.ABANDONED_REASON
    assert "does not show it" not in row["reason"]                           # PayPal does show the statements
    entry = next(e for e in repo.list_audit(db, DID) if e["action"] == "abandoned_by_vendor")
    assert entry["actor"] == "vendor" and entry["detail"]["partial"] is True
    assert entry["detail"]["other_package_sha256"] == row["package_sha256"]


def test_marking_an_ordinary_unconfirmed_attempt_keeps_the_ordinary_reason_and_audit(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")                                 # PayPal never showed it: no reason
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    age_attempts(db)
    r = c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received",
               data={"csrf_token": token, "confirmed": "on"}, follow_redirects=False)
    assert r.status_code == 303 and posts(fake) == []
    row = attempts(db)[0]
    assert row["state"] == "abandoned" and row["reason"] == send_service.ABANDONED_REASON
    entry = next(e for e in repo.list_audit(db, DID) if e["action"] == "abandoned_by_vendor")
    assert "partial" not in entry["detail"]


def test_a_recent_partial_attempt_is_still_not_abandoned(env):
    c, db, token, fake = env
    v1 = partial_version(env, missing="b.pdf")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received", data={"csrf_token": token, "confirmed": "on"})
    assert attempts(db)[0]["state"] == "sent_unconfirmed"                    # too recent: eligibility unchanged
    assert posts(fake) == []


def test_the_same_statements_row_is_still_skipped_by_the_mark(env):
    c, db, token, fake = env
    v1, v2 = v1_unconfirmed_v2_other_files(env)
    age_attempts(db)
    c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received", data={"csrf_token": token, "confirmed": "on"})
    assert [a["state"] for a in attempts(db)] == ["sent_unconfirmed"] and posts(fake) == []


# ================================================================== marking an attempt and the redirect after the post
def mark(c, token, pkg, follow=False):
    return c.post(f"/disputes/{DID}/packages/{pkg}/confirm-not-received",
                  data={"csrf_token": token, "confirmed": "on"}, follow_redirects=follow)


def test_prg_the_mark_answers_303_to_a_stored_result_and_the_get_changes_nothing(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    approve_passive(c, token, pkg)
    r = mark(c, token, pkg)
    assert r.status_code == 303
    assert re.fullmatch(rf"/disputes/{DID}/packages/{pkg}/result/[A-Za-z0-9_-]{{8,64}}", r.headers["location"])
    before = (len(repo.list_audit(db, DID)), attempts(db))
    first = c.get(r.headers["location"])
    second = c.get(r.headers["location"])
    assert first.status_code == 200 and "Nothing was waiting" in first.text
    assert state(parse(first.text), "acknowledged") is not None
    assert first.text == second.text
    assert (len(repo.list_audit(db, DID)), attempts(db)) == before and posts(fake) == []
    other = c.get(r.headers["location"].rsplit("/", 1)[0] + "/not-a-stored-id")
    assert other.status_code == 404


def test_prg_the_abandon_still_happens_once_on_the_post_and_not_on_the_reload(env):
    c, db, token, fake = env
    v1 = partial_version(env, missing="b.pdf")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    age_attempts(db)
    r = mark(c, token, v2)
    assert r.status_code == 303 and attempts(db)[0]["state"] == "abandoned"
    audits = len([e for e in repo.list_audit(db, DID) if e["action"] == "abandoned_by_vendor"])
    page_1 = c.get(r.headers["location"])
    c.get(r.headers["location"])
    assert "1 earlier unconfirmed attempt(s) for this dispute marked abandoned" in page_1.text
    assert len([e for e in repo.list_audit(db, DID) if e["action"] == "abandoned_by_vendor"]) == audits == 1
    assert posts(fake) == []


def test_prg_refusals_are_unchanged_csrf_untick_and_unknown_package(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    approve_passive(c, token, pkg)
    bad = c.post(f"/disputes/{DID}/packages/{pkg}/confirm-not-received",
                 data={"csrf_token": "wrong", "confirmed": "on"}, follow_redirects=False)
    assert bad.status_code == 403
    untick = c.post(f"/disputes/{DID}/packages/{pkg}/confirm-not-received",
                    data={"csrf_token": token}, follow_redirects=False)
    assert untick.status_code == 400 and "must check the box" in untick.text
    missing = c.post(f"/disputes/{DID}/packages/9999/confirm-not-received",
                     data={"csrf_token": token, "confirmed": "on"}, follow_redirects=False)
    assert missing.status_code == 404
    assert posts(fake) == []


NEW_OWN_ROW = "This package's own send is not confirmed yet. There is no earlier send to mark. Use Check again."


def test_direct_mark_on_the_package_that_owns_the_unconfirmed_row_says_so(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")                                # this package's own send, never shown
    age_attempts(db)
    audits = len(repo.list_audit(db, DID))
    res = mark(c, token, pkg, follow=True)
    text = parse(res.text).text()
    assert NEW_OWN_ROW in text
    assert "Nothing was waiting" not in text and send_service.ABANDON_TOO_RECENT_MESSAGE not in text
    assert [a["state"] for a in attempts(db)] == ["sent_unconfirmed"]
    assert not any(e["action"] == "abandoned_by_vendor" for e in repo.list_audit(db, DID))
    assert len(repo.list_audit(db, DID)) == audits and posts(fake) == []


def test_direct_mark_with_an_unconfirmed_row_owned_by_another_package_keeps_its_own_sentence(env):
    c, db, token, fake = env
    v1, v2 = v1_unconfirmed_v2_other_files(env)
    age_attempts(db)
    text = parse(mark(c, token, v2, follow=True).text).text()
    assert routes_send.SAME_STATEMENTS_MARK_MESSAGE in text and NEW_OWN_ROW not in text


def test_direct_mark_with_a_confirmed_row_keeps_nothing_was_waiting(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    age_attempts(db)
    text = parse(mark(c, token, pkg, follow=True).text).text()
    assert "Nothing was waiting" in text and NEW_OWN_ROW not in text
    assert [a["state"] for a in attempts(db)] == ["confirmed"]


# ============================================================================ the received-by time format
def test_the_banner_prints_the_display_format_in_mexico_city_time_whatever_the_stamp_offset(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    approve_passive(c, token, pkg)
    req = request_input(db, pkg)
    sha = sha256_json(req)
    package = repo.get_package(db, pkg)
    claims = repo.list_claims(db, pkg)
    store = SQLiteAttemptStore(db)
    assert store.claim(DID, sha)
    store.close()
    for stamp in ("2026-10-17T04:10:00+0000", "2026-10-16T22:10:00-0600", "2026-10-17T10:10:00+0600"):
        store = SQLiteAttemptStore(db)
        store.write(DID, sha, {"state": "confirmed", "request_id": "req-1", "ts": stamp, "request_input": req,
                               "attachment_filenames": []})
        store.close()
        assert send_service.send_status_banner(db, DID, package, claims) ==             "Received by PayPal on 16 Oct 2026, 10:10 pm (Mexico City time)", stamp


def test_the_check_page_for_a_confirmed_send_uses_the_display_format(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")
    fake.recovery_get_body = {**send_tests.WAITING, **confirmed_body(db, pkg, [])}
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    text = parse(res.text).text()
    assert "Received by PayPal on" in text and DISPLAY_TIME.search(text)
    assert "CST (UTC-6)" not in text and "CDMX" not in text
    assert posts(fake) == []


def test_the_mcp_send_status_uses_the_same_display_format():
    from test_mcp_server import _package
    db = config.db_path()
    pid = _package()
    package = repo.get_package(db, pid)
    claims = repo.list_claims(db, pid)
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import build_request_input, pick_tracking
    req = build_request_input(claims, *pick_tracking(extracted_docs_for(db, DID)))
    store = SQLiteAttemptStore(db)
    store.claim(DID, sha256_json(req))
    store.write(DID, sha256_json(req), {"state": "confirmed", "request_id": "req-1", "request_input": req,
                                        "ts": "2026-10-17T04:10:00+0000", "attachment_filenames": []})
    store.close()
    out = mcp_server._draft_json(db, DID)
    assert out["send_status"] == "Received by PayPal on 16 Oct 2026, 10:10 pm (Mexico City time)"


def test_an_unreadable_stamp_never_prints_not_available_after_received():
    from copilot.web import timefmt
    assert timefmt.display("garbled") == "Not available"                     # the helper's own answer, guarded here
    assert send_service._received_banner({"ts": "garbled"}) == "Received by PayPal"
    assert send_service._received_banner({"ts": None}) == "Received by PayPal"
    assert send_service._received_banner({"ts": "2026-10-17T04:10:00+0000"}) ==         "Received by PayPal on 16 Oct 2026, 10:10 pm (Mexico City time)"


# ====================================================================== first-person copy
def test_could_not_check_speaks_as_proofie(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")

    def unreachable(method, path, op, **kw):
        raise PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-rc42", {})
    fake.call = unreachable
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    dom = parse(res.text)
    assert dom.find("h1").text() == "Could not check" and state(dom, "paypal-unreachable") is not None
    text = dom.text()
    assert ("Proofie could not check PayPal just now, so whether PayPal has this evidence is still not known. "
            "Nothing was changed and nothing was sent.") in text
    assert "We could not" not in text


# ================================================================== follow-ups after review (A-1, A-3)
def test_the_banner_of_a_partial_attempt_marked_as_not_received_matches_its_reason(env):
    """The abandoned row of a partial attempt says what the vendor confirmed, on the banner and on the page of that very
    package; an ordinary abandoned row keeps its sentence."""
    c, db, token, fake = env
    v1 = partial_version(env, missing="b.pdf")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    age_attempts(db)
    assert mark(c, token, v2).status_code == 303
    assert attempts(db)[0]["reason"] == send_service.ABANDONED_PARTIAL_REASON
    package, claims = repo.get_package(db, v1), repo.list_claims(db, v1)
    banner_text = send_service.send_status_banner(db, DID, package, claims)
    assert "showed the statements without the attached file" in banner_text
    assert "did not show it" not in banner_text and not banner_text.startswith("Not sent")
    text = parse(page(c, v1).text).text()
    assert "showed the statements without the attached file" in text and "did not show it" not in text


def test_the_banner_of_an_ordinary_abandoned_attempt_keeps_its_sentence(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    age_attempts(db)
    assert mark(c, token, v2).status_code == 303
    assert attempts(db)[0]["reason"] == send_service.ABANDONED_REASON
    banner_text = send_service.send_status_banner(db, DID, repo.get_package(db, v1), repo.list_claims(db, v1))
    assert banner_text == "Not sent: marked abandoned, PayPal's Resolution Center did not show it"


@pytest.mark.parametrize("missing", [["b.pdf"], ["a.pdf", "b.pdf"]])
def test_the_abandon_audit_names_the_files_the_partial_attempt_was_missing(env, missing):
    c, db, token, fake = env
    partial_version(env, missing=",".join(missing))
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    age_attempts(db)
    assert mark(c, token, v2).status_code == 303
    entry = next(e for e in repo.list_audit(db, DID) if e["action"] == "abandoned_by_vendor")
    assert entry["detail"]["partial"] is True and entry["detail"]["missing_files"] == missing


def test_the_abandon_audit_of_an_ordinary_attempt_has_no_missing_files(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    age_attempts(db)
    assert mark(c, token, v2).status_code == 303
    entry = next(e for e in repo.list_audit(db, DID) if e["action"] == "abandoned_by_vendor")
    assert "partial" not in entry["detail"] and "missing_files" not in entry["detail"]


HUGE = 10 ** 30


def test_a_package_id_beyond_the_database_range_is_a_404_on_every_lane_a_route(env):
    c, db, token, fake = env
    got = c.get(f"/disputes/{DID}/packages/{HUGE}/result/abcdefgh12345678")
    assert got.status_code == 404 and "Result not found" in got.text
    for action in ("send", "check", "confirm-not-received"):
        r = c.post(f"/disputes/{DID}/packages/{HUGE}/{action}", data={"csrf_token": token, "confirmed": "on"},
                   follow_redirects=False)
        assert r.status_code == 404, (action, r.status_code)
    zero = c.get(f"/disputes/{DID}/packages/0/result/abcdefgh12345678")
    assert zero.status_code == 404
    assert posts(fake) == []


# ============================================================ result page: banner without a status label (glyph tag)
def banner_title(html):
    dom = parse(html)
    box = next(n for n in dom.walk() if "banner" in n.attrs.get("class", "").split())
    return dom, box, box.find("p", class_="banner-title")


def test_a_result_banner_without_a_status_label_gets_the_glyph_tag_and_keeps_its_words_for_screen_readers(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    approve_passive(c, token, pkg)
    seed_attempt(db, pkg, "sent_unconfirmed")

    def unreachable(method, path, op, **kw):
        raise PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-rc42-mark", {})
    fake.call = unreachable
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    dom, box, title = banner_title(result_page(c, r).text)
    assert box.attrs.get("data-state") == "paypal-unreachable" and box.attrs.get("role") == "alert"
    assert title is not None
    tag = title.find("span", class_="tag")
    assert tag is not None and "tag--bad" in tag.attrs["class"]
    glyph = tag.find("span", aria_hidden="true")
    assert glyph is not None and glyph.text() == "✕"                                # the glyph is hidden from screen readers
    words = tag.find("span", class_="visually-hidden")
    assert words is not None and words.text() == "Error"                            # the words stay for them
    assert dom.find("h1").text() == "Could not check"                               # and the h1 is not repeated in the banner


def test_a_result_banner_with_a_status_label_keeps_that_label_and_no_second_title(env):
    c, db, token, fake = env
    pkg = partial_version(env)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token}, follow_redirects=False)
    dom, box, title = banner_title(result_page(c, r).text)
    assert len(box.find_all("p", class_="banner-title")) == 1
    assert "Statements in PayPal, file missing" in title.text() and title.find("span", class_="visually-hidden") is None
