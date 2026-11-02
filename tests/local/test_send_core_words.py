"""The words of the send core and of its callers follow one rule: a message names a way out only when a send is possible,
says the rule while PayPal's fresh read lists evidence from the seller, and says PayPal is not asking when it offers no
provide-evidence action. Offline only: fake PayPal clients, a temp database per test, answers read back from the result
page or the returned outcome. No test here changes a guard, an order or a write of the send path."""
import json

import pytest
from _dom import parse
from _hosted import set_test_csrf

from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json
from copilot.db import repo
from copilot.web import next_step, routes_send
from copilot.core.disputes import PayPalError

import test_routes_send as send_tests
from test_mcp_server import _package, fake  # noqa: F401
from test_evidence_once_per_dispute import list_other_evidence
from test_rc42_package import V2_CHANGED
from test_send_rule_matrix import (RULE, body_for, set_read, st_abandoned_plain, st_confirmed_other_files,
                                   st_other_unconf_other, st_other_unconf_same)
from test_ux_rc41_fix_state import STATEMENT, posts, request_input
from test_ux_rc41_package import (DID, add_claims, approve_passive, approved, draft, env, mark_attempt,  # noqa: F401
                                  new_package)

pytestmark = [pytest.mark.usefixtures("web_mode")]

RAW = "evidence already submitted by the seller: ['PROOF_OF_FULFILLMENT']"


# ---------------------------------------------------------------------------------------------- one definition of offered
def test_one_helper_decides_whether_provide_evidence_is_offered():
    link = {"provide-evidence": {"href": "https://api-m.sandbox.paypal.com/x", "method": "POST"}}
    cases = [({"can_provide_evidence": True}, True), ({"action_links": link}, True),
             ({"can_provide_evidence": False, "action_links": {}}, False), ({}, False), (None, False)]
    for norm, expected in cases:
        assert send_service.provide_evidence_offered(norm) is expected, norm
        assert next_step.can_provide(norm) is expected, norm


def test_no_way_out_names_an_unusable_link_in_its_own_words():
    unusable = "PayPal's provide-evidence link is not one Proofie can use."
    assert send_service.UNUSABLE_LINK_NOTE == unusable
    assert send_service.no_way_out(False, True, usable=False) == unusable
    assert send_service.no_way_out(False, True) is None and send_service.no_way_out(True, True) == RULE
    assert send_service.no_way_out(False, False) == send_service.NOT_ASKING_NOTE
    assert send_service.no_way_out(False, False, usable=False) == send_service.NOT_ASKING_NOTE
    assert send_service.no_way_out(True, False) == RULE and "pages use" not in send_service.no_way_out.__doc__.lower()


def test_an_unexpected_provide_evidence_link_is_not_called_not_asking(env):
    c, db, token, fake = env
    info = st_abandoned_plain(env)
    body = json.loads(json.dumps(send_tests.WAITING))
    for entry in body["links"]:
        if entry["rel"] == "provide_evidence":
            entry["href"] = "https://evil.example/v1/customer/disputes/X/provide-evidence"
    set_read(fake, body)
    out = send_service.send_package(fake, db, DID, info["page"])
    assert send_service.UNUSABLE_LINK_NOTE in out.message and send_service.NOT_ASKING_NOTE not in out.message
    assert posts(fake) == []


# ------------------------------------------------------------------------------------- no failure after the abandon writes
def _malformed_read(fake):
    body = json.loads(json.dumps(send_tests.WAITING))
    body["disputed_transactions"] = ["not a transaction"]          # normalize cannot read this
    set_read(fake, body)


def test_a_malformed_read_never_fails_the_mark_after_the_rows_were_abandoned(env):
    c, db, token, fake = env
    info = st_other_unconf_other(env)
    _malformed_read(fake)
    sha = sha256_json(request_input(db, info["page"]))
    store = SQLiteAttemptStore(db)
    try:
        result = send_service.confirm_other_attempts_abandoned(fake, store, db, DID, sha)
    finally:
        store.close()
    assert result["abandoned"] == 1 and result["closed"] is None and result["read"] is False


def test_the_mark_route_answers_a_malformed_read_with_the_count(env):
    c, db, token, fake = env
    info = st_other_unconf_other(env)
    _malformed_read(fake)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{info['page']}/confirm-not-received",
               data={"csrf_token": token, "confirmed": "on"}, follow_redirects=False)
    assert r.status_code == 303
    text = parse(c.get(r.headers["location"]).text).find("main").text()
    assert "1 earlier unconfirmed attempt(s) for this dispute marked abandoned." in text
    assert routes_send.MARKED_NO_READ in text and "You can try sending this package now" not in text


# -------------------------------------------------------------------------------------------------------------- past tense
def test_the_confirmed_collision_answer_says_already_holds_only_when_listed(env):
    c, db, token, fake = env
    info = st_confirmed_other_files(env)
    kw = dict(own_pkg=info["own"], own_files=info["own_files"])
    set_read(fake, body_for(db, "none", True, **kw))
    out = send_service.send_package(fake, db, DID, info["page"])
    assert out.title == send_service.SAME_STATEMENTS_TITLE
    assert "When Proofie checked, PayPal held these statements" in out.message
    assert "already holds" not in out.message and "Change a statement to make a new submission." in out.message
    set_read(fake, body_for(db, "other", True, **kw))
    listed = send_service.send_package(fake, db, DID, info["page"])
    assert "PayPal already holds these statements" in listed.message and RULE in listed.message
    assert "Change a statement" not in listed.message and posts(fake) == []


# --------------------------------------------------------------------------------------------- the mark, same statements
def test_the_mark_answer_for_the_same_statements_names_no_way_out_without_a_read(env, monkeypatch):
    c, db, token, fake = env
    info = st_other_unconf_same(env)
    real = fake.call

    def unreachable(method, path, op, **kw):
        if op == "recovery_get":
            raise PayPalError(503, None, None, {})
        return real(method, path, op, **kw)

    monkeypatch.setattr(fake, "call", unreachable)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{info['page']}/confirm-not-received",
               data={"csrf_token": token, "confirmed": "on"}, follow_redirects=False)
    text = " ".join(parse(c.get(r.headers["location"]).text).find("main").text().split())
    assert f"{routes_send.SAME_STATEMENTS_MARK_BASE} {routes_send.MARKED_NO_READ}" in text
    assert "Change a statement" not in text


def test_the_mark_answer_for_the_same_statements_keeps_its_way_out_after_a_read(env):
    c, db, token, fake = env
    info = st_other_unconf_same(env)
    set_read(fake, body_for(db, "none", True, own_pkg=info["own"], own_files=info["own_files"]))
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{info['page']}/confirm-not-received",
               data={"csrf_token": token, "confirmed": "on"}, follow_redirects=False)
    text = " ".join(parse(c.get(r.headers["location"]).text).find("main").text().split())
    assert routes_send.SAME_STATEMENTS_MARK_MESSAGE in text


# ------------------------------------------------------------------------------------------------- a fresh pending row
def test_a_fresh_pending_row_of_another_package_promises_no_send(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    approve_passive(c, token, v2)
    sha1, sha2 = sha256_json(request_input(db, v1)), sha256_json(request_input(db, v2))
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(DID, sha1)                                         # pending, fresh, another package's
        before = send_service._other_attempts_block(store, db, DID, v2, sha2)
        open_ = send_service._other_attempts_block(store, db, DID, v2, sha2, None, read=True)
        listed = send_service._other_attempts_block(store, db, DID, v2, sha2, RULE, read=True)
        asking_nothing = send_service._other_attempts_block(store, db, DID, v2, sha2, send_service.NOT_ASKING_NOTE, read=True)
    finally:
        store.close()
    assert before.message == "Another send for this dispute is still in progress. Wait a moment and use Check again."
    assert "before sending a different package" in open_.message
    assert listed.message.endswith(RULE) and "before sending" not in listed.message
    assert asking_nothing.message.endswith(send_service.NOT_ASKING_NOTE) and "before sending" not in asking_nothing.message
    answer = send_service.send_package(fake, db, DID, v2)                       # the real path: the first call, no read
    assert answer.message == before.message and posts(fake) == []


# ------------------------------------------------------------------------------------------------------------------ MCP
def test_the_mcp_send_tool_answers_not_approved_without_an_approve_way_out(fake, monkeypatch):
    from copilot import mcp_server
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    _package(approved=False)
    out = mcp_server.send_approved_package(DID)
    assert out["message"] == ("Refused: no package for this dispute has been approved by a human; review_url shows what "
                              "can be done.")
    assert "Approve it" not in out["message"] and fake.posts == []


def test_the_mcp_send_tool_answers_a_changed_package_without_an_approve_way_out(fake, monkeypatch):
    from copilot import config, mcp_server
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    pid = _package(approved=True)
    repo.insert_claims(config.db_path(), pid, [{"evidence_index": 0, "claim_index": 1, "text": "Edited later.",
                                                "source_ids": [], "basis": "seller_statement"}])
    out = mcp_server.send_approved_package(DID)
    assert out["message"] == ("Refused: the package changed since it was approved; review_url shows what can be done.")
    assert "approve it again" not in out["message"] and fake.posts == []


@pytest.mark.parametrize("listed, words", [(True, RULE), (False, "it lists none now."),
                                           (None, "PayPal listed evidence from you when Proofie last tried.")])
def test_the_mcp_draft_status_follows_the_fresh_read_when_there_was_one(fake, listed, words):
    from copilot import config, mcp_server
    pid = _package(approved=True)
    mark_attempt(config.db_path(), pid, "blocked", reason=RAW)
    kwargs = {} if listed is None else {"listed": listed}
    draft_json = mcp_server._draft_json(config.db_path(), DID, False, **kwargs)
    assert words in draft_json["send_status"] and "evidence already submitted by the seller" not in draft_json["send_status"]


# ------------------------------------------------------------------------------------------ uncertain POST, abandoned row
def test_after_an_uncertain_post_an_abandoned_row_offers_no_way_out(env, monkeypatch):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    fake.provide_evidence_transport_error = True
    monkeypatch.setattr(send_service, "reconcile_after_send",
                        lambda *a, **k: {"abandoned": True, "confirmed": False, "files_ok": None, "missing_files": [],
                                         "details": {}})
    out = send_service.send_package(fake, db, DID, pkg)
    assert "marked as not received" in out.message and "The send result is unknown; use Check again." in out.message
    assert "edit it" not in out.message and "creates a new version" not in out.message


# ------------------------------------------------------------------------------------------------- the seller's words
@pytest.mark.parametrize("listed, first", [(True, RULE), (False, "it lists none now."), (None, "when Proofie last tried.")])
def test_the_remaining_problems_start_a_new_sentence_in_capitals(listed, first):
    text = f"{RAW}; response deadline already passed; disputed transaction was not found"
    words = send_service.seller_words(text, listed)
    assert words.endswith(f"{first} Response deadline already passed; disputed transaction was not found"), words
    assert send_service.seller_words(RAW, listed).endswith(first)


class _Spy:
    marker = "attribute of the client"

    def __init__(self, answers):
        self.answers, self.log = answers, []

    def call(self, method, path, op, **kwargs):
        self.log.append((method, path, op, kwargs))
        answer = self.answers[(method, op)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_the_last_read_recorder_keeps_the_last_get_and_changes_nothing():
    boom = RuntimeError("PayPal said no")
    answers = {("GET", "a"): {"n": 1}, ("POST", "p"): {"posted": True}, ("GET", "bad"): boom, ("GET", "b"): {"n": 2}}
    spy, plain = _Spy(answers), _Spy(answers)
    sequence = [("GET", "/x", "a", {"log_extra": 1}), ("POST", "/y", "p", {"request_id": "r"}), ("GET", "/z", "bad", {}),
                ("GET", "/w", "b", {})]
    reader = send_service._LastRead(spy)
    assert reader.last is None and reader.marker == "attribute of the client"
    assert reader.call(*sequence[0][:3], **sequence[0][3]) == {"n": 1} and reader.last == {"n": 1}
    assert reader.call(*sequence[1][:3], **sequence[1][3]) == {"posted": True} and reader.last == {"n": 1}   # a POST is ignored
    with pytest.raises(RuntimeError) as err:
        reader.call(*sequence[2][:3], **sequence[2][3])
    assert err.value is boom and reader.last == {"n": 1}                           # the same exception, last unchanged
    assert reader.call(*sequence[3][:3], **sequence[3][3]) == {"n": 2} and reader.last == {"n": 2}
    for method, path, op, kwargs in sequence:                                       # the same calls, not wrapped
        try:
            plain.call(method, path, op, **kwargs)
        except RuntimeError:
            pass
    assert spy.log == plain.log


# ------------------------------------------------------------------------- the requested-evidence problem in plain words
NOT_AMONG = "PROOF_OF_FULFILLMENT is not among the evidence PayPal requested from the seller ([])"
PLAIN_NOT_ASKED = "PayPal did not ask for proof of shipment or delivery in this dispute."


@pytest.mark.parametrize("listed", [True, False, None])
@pytest.mark.parametrize("raw, plain", [
    (NOT_AMONG, PLAIN_NOT_ASKED),
    ("PROOF_OF_FULFILLMENT is not among the evidence PayPal requested from the seller (['PROOF_OF_REFUND', 'OTHER'])",
     PLAIN_NOT_ASKED),
    ("PROOF_OF_REFUND is not among the evidence PayPal requested from the seller (['OTHER'])",
     "PayPal did not ask for proof of a refund in this dispute."),
    ("NEW_KIND is not among the evidence PayPal requested from the seller ([])",
     "PayPal did not ask for this kind of evidence in this dispute.")])
def test_the_requested_evidence_problem_is_told_in_plain_words(raw, plain, listed):
    words = send_service.seller_words(raw, listed)
    assert words == plain
    assert "_" not in words and "[" not in words and "among the evidence" not in words
    both = send_service.seller_words(f"{raw}; {RAW}; response deadline already passed", listed)
    assert both.startswith(f"{plain} ") and both.endswith("Response deadline already passed")
    assert "[" not in both and "PROOF_OF" not in both and "NEW_KIND" not in both
    assert send_service.seller_words("Nothing to change here.", listed) == "Nothing to change here."


def test_the_package_banner_says_the_requested_evidence_problem_in_plain_words(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "blocked", reason=NOT_AMONG)
    main = " ".join(parse(c.get(f"/disputes/{DID}/packages/{pkg}").text).find("main").text().split())
    assert PLAIN_NOT_ASKED in main and "among the evidence" not in main and "([])" not in main


def test_the_result_page_says_the_requested_evidence_problem_in_plain_words(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    list_other_evidence(fake)                                     # the read lists evidence and requests none
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    main = " ".join(parse(c.get(r.headers["location"]).text).find("main").text().split())
    assert PLAIN_NOT_ASKED in main and "among the evidence" not in main and "PROOF_OF_FULFILLMENT" not in main
    assert posts(fake) == []


def test_the_mcp_status_says_the_requested_evidence_problem_in_plain_words(fake):
    from copilot import config, mcp_server
    pid = _package(approved=True)
    mark_attempt(config.db_path(), pid, "blocked", reason=NOT_AMONG)
    status = mcp_server._draft_json(config.db_path(), DID, False)["send_status"]
    assert PLAIN_NOT_ASKED in status and "among the evidence" not in status and "[" not in status
