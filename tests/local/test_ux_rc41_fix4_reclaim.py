"""No send ever overwrites another package's attempt record.

An earlier version let a "same statements, other files" send reclaim the abandoned row of an earlier package. That row was the only
record of the earlier package's POST: v1 lost it, a late landing could no longer be promoted, v1 could be sent again and
v2 could read "Sent to PayPal" after a 422. The reclaim is gone; the way out is the path that already works: change a
statement in the new package (its content sha differs, so it has its own attempt row), mark the earlier send as not
received on its page, then send it.

Offline only: fake PayPal clients, a temp database per test (conftest.py). Effects are read from stored rows, the requests
the fake saw and the rendered DOM.
"""
import pytest
from _dom import parse
from _hosted import set_test_csrf

from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.disputes import PayPalError
from copilot.core.send import sha256_json
from copilot.db import repo
from copilot.web import status

from test_ux_rc41_fix3_state3 import age_attempts, attempts, form_token, v1_unconfirmed_v2_other_files
from test_ux_rc41_fix_state import banner, confirmed_body, posts, request_input, result_page
from test_ux_rc41_package import DID, add_claims, approve_passive, env, page  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]

ADVICE = ("Change a statement in this package (for example add one sentence), then mark the earlier send as not "
          "received on this package's page, then send this package.")
V2_EXTRA = "The order confirmation is attached."


def by_sha(db):
    return {a["package_sha256"]: a for a in attempts(db)}


def follow_the_advice(env, v1, v2):
    """What the seller does after the refusal: add a sentence to v2, approve it again, mark the earlier send as not
    received with the form v2's page offers. Returns the csrf token of the page that offered the form."""
    c, db, token, fake = env
    add_claims(db, v2, V2_EXTRA)
    set_test_csrf(c, token)
    approve_passive(c, token, v2)
    assert repo.get_latest_valid_approval(db, v2)
    dom = parse(page(c, v2).text)                       # one GET: every GET rotates the CSRF cookie
    forms = [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/confirm-not-received")]
    assert len(forms) == 1 and "Why is sending blocked?" in dom.text()
    tok = form_token(forms[0])
    r = c.post(forms[0].attrs["action"], data={"csrf_token": tok, "confirmed": "on"})
    assert r.status_code == 200 and "1 earlier unconfirmed attempt" in r.text
    return tok


def refuse_then_follow(env):
    c, db, token, fake = env
    v1, v2 = v1_unconfirmed_v2_other_files(env)
    v1_sha = sha256_json(request_input(db, v1))
    age_attempts(db)
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = result_page(c, r)
    assert res.status_code == 409 and posts(fake) == []
    assert ADVICE in banner(res.text)[1].text()
    tok = follow_the_advice(env, v1, v2)
    return v1, v2, v1_sha, tok


# ---- the advice of the refusal and of the collision step -----------------------------------------------------
def test_fix4_the_advice_of_the_refusal_and_of_the_collision_step_is_the_one_that_works():
    from copilot.web import next_step
    step = next_step._collision_step(status.Collision("attempted", 1, 7, ("b.pdf",)), package_url="/p")
    assert ADVICE in step.body
    assert "mark it as not received on the package page" not in step.body


def test_fix4_following_the_advice_end_to_end_makes_exactly_one_post_confirmed_only_by_the_read_back(env):
    c, db, token, fake = env
    v1, v2, v1_sha, tok = refuse_then_follow(env)
    assert posts(fake) == []                                    # refusing and marking send nothing
    assert by_sha(db)[v1_sha]["state"] == "abandoned"
    # PayPal's read-back does not list v2's statements yet: nothing is confirmed
    fake.post_send_get_bodies = [{"evidences": []}] * 3
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and len(posts(fake)) == 1
    v2_rows = [a for sha, a in by_sha(db).items() if sha != v1_sha]
    assert len(v2_rows) == 1 and v2_rows[0]["state"] == "sent_unconfirmed"
    assert "Sent to PayPal" not in c.get(r.headers["location"]).text


def test_fix4_the_new_version_is_confirmed_from_paypals_read_back(env):
    c, db, token, fake = env
    v1, v2, v1_sha, tok = refuse_then_follow(env)
    fake.post_send_get_bodies = [confirmed_body(db, v2, ["a.pdf", "b.pdf"])]
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and len(posts(fake)) == 1
    rows = by_sha(db)
    assert len(rows) == 2 and rows[v1_sha]["state"] == "abandoned"
    (v2_row,) = [a for sha, a in rows.items() if sha != v1_sha]
    assert v2_row["state"] == "confirmed" and v2_row["attachment_filenames"] == ["a.pdf", "b.pdf"]
    assert v2_row["request_id"] != rows[v1_sha]["request_id"]


# ---- a late landing of v1 promotes v1s own attempt and v1 reads ----------------------------------------------
def test_fix4_a_late_landing_of_v1_promotes_v1s_own_attempt_and_v1_reads_sent(env):
    c, db, token, fake = env
    v1, v2, v1_sha, tok = refuse_then_follow(env)
    fake.post_send_get_bodies = [confirmed_body(db, v2, ["a.pdf", "b.pdf"])]
    c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": tok}, follow_redirects=False)
    assert len(posts(fake)) == 1
    # PayPal now lists v1's statements and a.pdf (the POST that looked lost landed)
    fake.recovery_get_body = confirmed_body(db, v1, ["a.pdf"])
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v1}/check", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code in (200, 303)
    assert by_sha(db)[v1_sha]["state"] == "confirmed"
    assert any(e["action"] == "abandoned_attempt_later_confirmed_by_paypal" for e in repo.list_audit(db, DID))
    record = status.send_record(db, DID, repo.get_package(db, v1))
    assert record.state == "confirmed"
    text = parse(page(c, v1).text).text()
    assert "Approved, not sent" not in text and "Nothing has reached PayPal" not in text
    assert len(posts(fake)) == 1                                 # recovery only reads


# ---- keeps its attempt record on every path ------------------------------------------------------------------
def test_fix4_v1_keeps_its_attempt_record_on_every_path(env):
    c, db, token, fake = env
    v1, v2, v1_sha, tok = refuse_then_follow(env)
    before = by_sha(db)[v1_sha]
    assert before["request_id"] == "req-v1"
    registered = send_service.registered_attempts(db, DID)
    assert registered["req-v1"][0] == v1
    # v2's send is refused by PayPal (422): v1's row is untouched and still maps to v1
    fake.provide_evidence_error = PayPalError(422, "UNPROCESSABLE_ENTITY", "dbg-fix4", {})
    c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": tok}, follow_redirects=False)
    assert len(posts(fake)) == 1
    rows = by_sha(db)
    assert rows[v1_sha] == before
    assert [a["state"] for sha, a in rows.items() if sha != v1_sha] == ["failed"]
    assert [a["request_id"] for a in send_service.package_attempts(db, DID, repo.get_package(db, v1))] == ["req-v1"]
    assert status.send_record(db, DID, repo.get_package(db, v1)).state == "abandoned"
    # and v2 never reads as sent
    assert "Sent to PayPal" not in parse(page(c, v2).text).text()


def test_fix4_the_unadvised_path_never_overwrites_v1s_record_and_a_late_landing_still_promotes_it(env):
    """Marking without changing a statement (the round-3 reclaim path): the same-statements row is not abandoned, v2 is
    refused with 0 POSTs, v1 keeps its row and request id, v1 cannot be re-sent, and a late landing promotes v1's own
    attempt."""
    c, db, token, fake = env
    v1, v2 = v1_unconfirmed_v2_other_files(env)
    v1_sha = sha256_json(request_input(db, v1))
    age_attempts(db)
    r = c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received", data={"csrf_token": token, "confirmed": "on"})
    assert "Nothing was waiting" not in r.text                   # A row IS waiting; the answer says why
    assert ("The earlier send has the same statements as this package, so it cannot be marked from here yet. "
            "Change a statement in this package first, then mark it as not received.") in r.text
    assert by_sha(db)[v1_sha]["state"] == "sent_unconfirmed"
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    assert posts(fake) == []
    row = by_sha(db)[v1_sha]
    assert row["state"] == "sent_unconfirmed" and row["request_id"] == "req-v1"
    assert [a["request_id"] for a in send_service.package_attempts(db, DID, repo.get_package(db, v1))] == ["req-v1"]
    set_test_csrf(c, token)
    c.post(f"/disputes/{DID}/packages/{v1}/send", data={"csrf_token": token}, follow_redirects=False)
    assert posts(fake) == []                                     # v1 is never re-sent
    fake.recovery_get_body = confirmed_body(db, v1, ["a.pdf"])
    set_test_csrf(c, token)
    c.post(f"/disputes/{DID}/packages/{v1}/check", data={"csrf_token": token}, follow_redirects=False)
    assert by_sha(db)[v1_sha]["state"] == "confirmed"
    assert status.send_record(db, DID, repo.get_package(db, v1)).state == "confirmed"
    assert posts(fake) == []


# ---- can never be sent again after v2 is refused -------------------------------------------------------------
def test_fix4_v1_can_never_be_sent_again_after_v2_is_refused(env):
    c, db, token, fake = env
    v1, v2, v1_sha, tok = refuse_then_follow(env)
    fake.provide_evidence_error = PayPalError(422, "UNPROCESSABLE_ENTITY", "dbg-fix4", {})
    c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": tok}, follow_redirects=False)
    assert len(posts(fake)) == 1
    fake.provide_evidence_error = None
    n = len(posts(fake))
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v1}/send", data={"csrf_token": tok}, follow_redirects=False)
    assert len(posts(fake)) == n                                 # 0 POSTs for v1
    dom = parse(page(c, v1).text)
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith(f"/{v1}/send")]
    assert by_sha(db)[v1_sha]["state"] == "abandoned"
    # v1 stays unsendable on every further click too
    set_test_csrf(c, token)
    c.post(f"/disputes/{DID}/packages/{v1}/send", data={"csrf_token": tok}, follow_redirects=False)
    assert len(posts(fake)) == n


def test_fix4_the_normal_repeat_of_a_package_still_reclaims_its_own_failed_row(env):
    """The reclaim of a package's OWN failed row (a clean 4xx, nothing received) is not touched by this change."""
    c, db, token, fake = env
    from test_ux_rc41_fix_state import LABEL_A, STATEMENT, add_file
    from test_ux_rc41_package import draft
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    fake.provide_evidence_error = PayPalError(422, "UNPROCESSABLE_ENTITY", "dbg-fix4", {})
    c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    fake.provide_evidence_error = None
    fake.post_send_get_bodies = [confirmed_body(db, pkg, ["a.pdf"])]
    c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    assert len(posts(fake)) == 2 and [a["state"] for a in attempts(db)] == ["confirmed"]


def test_fix4_a_send_over_another_packages_failed_row_still_carries_its_own_request_id(env):
    """The one row of another package a send still takes over is a 'failed' one (PayPal refused it cleanly, nothing
    received); the new package's POST must not reuse the earlier attempt's idempotency id (attempt_request_id)."""
    c, db, token, fake = env
    from test_ux_rc41_fix_state import LABEL_A, OTHER, STATEMENT, add_file
    from test_ux_rc41_fix_state_status import seed_attempt
    from test_ux_rc41_package import draft, new_package
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    sha = sha256_json(request_input(db, v1))
    earlier = send_service.request_id_for(DID, sha)
    seed_attempt(db, v1, "failed", request_id=earlier)
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", OTHER)
    approve_passive(c, token, v2)
    age_attempts(db)
    fake.post_send_get_bodies = [confirmed_body(db, v2, ["a.pdf", "b.pdf"])]
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and len(posts(fake)) == 1
    assert posts(fake)[0]["request_id"] != earlier
