"""The way out of an unconfirmed same-statements send is shown on the pages that need it.

State: v1 = statements S + a.pdf, sent, never confirmed (aged); v2 = the same statements S + a.pdf + b.pdf, approved.
Before this round v2's package page and the dispute page showed the "paused / pending" step and a "Mark as not received" form
that cannot act on the same-statements row; the answer to it was a false "Nothing was waiting" and the advice (change a
statement, mark the earlier send, send) was never shown. The seller was stuck, safely (0 POSTs), but stuck.

Offline only: fake PayPal clients, a temp database per test (conftest.py). Effects are read from stored rows, the requests
the fake saw and the rendered DOM. The advised flow below uses UI routes only.
"""
import pytest
from _dom import parse
from _hosted import set_test_csrf

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json
from copilot.db import repo

import test_routes_send as send_tests
from test_ux_rc41_fix3_state3 import age_attempts, attempts, form_token, v1_unconfirmed_v2_other_files
from test_ux_rc41_fix_state import confirmed_body, posts, request_input
from test_ux_rc41_fix_state_status import panel, seed_attempt  # noqa: F401
from test_ux_rc41_package import DID, approve_passive, approved, env, new_package, page  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]

ADVICE = ("Change a statement in this package (for example add one sentence), then mark the earlier send as not "
          "received on this package's page, then send this package.")
EARLY_MARK = ("The earlier send has the same statements as this package, so it cannot be marked from here yet. "
              "Change a statement in this package first, then mark it as not received.")
NOTHING = "Nothing was waiting"
V2_EXTRA = "The order confirmation is attached."


def forms_to(dom, suffix):
    return [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith(suffix)]


def actions_of(node):
    """(text, href or form action, class list) of every link and button inside the panel."""
    out = [(a.text(), a.attrs.get("href"), a.attrs.get("class", "").split()) for a in node.find_all("a")]
    for form in node.find_all("form"):
        for b in form.find_all("button"):
            out.append((b.text(), form.attrs.get("action"), b.attrs.get("class", "").split()))
    return out


def stuck_state(env):
    c, db, token, fake = env
    v1, v2 = v1_unconfirmed_v2_other_files(env)
    age_attempts(db)
    return v1, v2


# ---------------------------------------------------------------------------------------------- (1) the pages
def test_the_package_page_of_v2_shows_the_advice_not_the_paused_step_by_get_only(env):
    c, db, token, fake = env
    v1, v2 = stuck_state(env)
    html = page(c, v2).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "same_statements"
    assert ADVICE in text
    assert "Nothing has reached PayPal yet" not in text
    assert not forms_to(parse(html), "/send")                                       # no Send button
    acts = actions_of(node)
    primary = [a for a in acts if "btn-secondary" not in a[2]]
    assert [(t, h) for t, h, _ in primary] == [("Change a statement", f"/disputes/{DID}/packages/{v2}#statements")]
    assert any(t == "Check again" and "btn-secondary" in k for t, h, k in acts)       # Check again stays, secondary
    assert posts(fake) == [] and [a["state"] for a in attempts(db)] == ["sent_unconfirmed"]


def test_the_dispute_page_shows_the_same_advice_with_a_link_to_the_package_statements(env):
    c, db, token, fake = env
    v1, v2 = stuck_state(env)
    html = c.get(f"/disputes/{DID}").text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "same_statements"
    assert ADVICE in text
    assert not forms_to(parse(html), "/send")
    acts = actions_of(node)
    primary = [a for a in acts if "btn-secondary" not in a[2]]
    assert [(t, h) for t, h, _ in primary] == [("Change a statement", f"/disputes/{DID}/packages/{v2}#statements")]
    assert any(t == "Check again" and "btn-secondary" in k for t, h, k in acts)
    assert posts(fake) == []


def test_the_why_blocked_panel_leads_with_changing_a_statement_in_that_state(env):
    c, db, token, fake = env
    v1, v2 = stuck_state(env)
    block = parse(page(c, v2).text).find("details", id="why-blocked")
    text = block.text()
    assert "1. Change a statement in this package (for example add one sentence)." in text
    # the words "PayPal's Resolution Center" are a link now (its text adds a hidden
    # new-tab note), so the sentence is pinned in its two halves around the link.
    assert "2. Then open this dispute in" in text
    assert "If the earlier send is not there, tick the box and press Mark as not received." in text
    assert [a.text() for a in block.find_all("a") if "sandbox.paypal.com" in a.attrs.get("href", "")][:1] ==         ["PayPal's Resolution Center (opens in a new tab)"]
    assert "3. Then send this package." in text
    assert "1. Press Check again." not in text
    assert len(forms_to(block, "/confirm-not-received")) == 1


# ------------------------------------------------------------------------------------ (3) the truthful answer
def test_the_mark_in_that_state_says_why_it_cannot_act_and_never_nothing_was_waiting(env):
    c, db, token, fake = env
    v1, v2 = stuck_state(env)
    dom = parse(page(c, v2).text)
    form = forms_to(dom, "/confirm-not-received")[0]
    before = attempts(db)
    r = c.post(form.attrs["action"], data={"csrf_token": form_token(form), "confirmed": "on"})
    assert r.status_code == 200
    assert EARLY_MARK in r.text and NOTHING not in r.text
    assert attempts(db) == before                                                    # nothing was marked
    assert posts(fake) == []
    assert not any(e["action"] == "abandoned_by_vendor" for e in repo.list_audit(db, DID))


def test_the_mark_answer_for_a_row_nobody_is_waiting_on_is_unchanged(env):
    """A package with no unconfirmed row anywhere still gets the old, true answer."""
    c, db, token, fake = env
    pkg = approved(c, db, token, "Plain statement.")
    r = c.post(f"/disputes/{DID}/packages/{pkg}/confirm-not-received", data={"csrf_token": token, "confirmed": "on"})
    assert NOTHING in r.text and EARLY_MARK not in r.text


# ---------------------------------------------------------------------------------- (4) the advised flow, UI only
def test_the_advised_flow_through_ui_routes_ends_in_one_post_confirmed_only_from_the_read_back(env):
    c, db, token, fake = env
    v1, v2 = stuck_state(env)
    base = f"/disputes/{DID}/packages/{v2}"
    v1_sha = sha256_json(request_input(db, v1))
    assert ADVICE in panel(page(c, v2).text)[1]                                      # 1. the page says what to do
    set_test_csrf(c, token)
    r = c.post(f"{base}/claims", data={"csrf_token": token, "text": V2_EXTRA}, follow_redirects=False)   # change a statement
    assert r.status_code == 303
    assert V2_EXTRA in [x["text"] for x in repo.list_claims(db, v2)]
    assert repo.get_latest_valid_approval(db, v2) is None                              # the change cleared the approval
    set_test_csrf(c, token)
    approve_passive(c, token, v2)
    assert repo.get_latest_valid_approval(db, v2)
    html = page(c, v2).text                                                          # 2. now the mark form acts
    assert panel(html)[0].attrs["data-next-step"] == "other_pending"
    form = forms_to(parse(html), "/confirm-not-received")[0]
    tok = form_token(form)
    r = c.post(form.attrs["action"], data={"csrf_token": tok, "confirmed": "on"})
    assert r.status_code == 200 and "1 earlier unconfirmed attempt" in r.text and NOTHING not in r.text
    by_sha = {a["package_sha256"]: a for a in attempts(db)}
    assert by_sha[v1_sha]["state"] == "abandoned"
    assert posts(fake) == []                                                           # nothing sent so far
    dom = parse(page(c, v2).text)                                                    # 3. now the page offers the send
    sends = forms_to(dom, "/send")
    assert len(sends) == 1 and not forms_to(dom, "/confirm-not-received")
    tok = form_token(sends[0])
    fake.post_send_get_bodies = [confirmed_body(db, v2, ["a.pdf", "b.pdf"])]           # PayPal's read-back lists it
    r = c.post(sends[0].attrs["action"], data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    assert len(posts(fake)) == 1                                                       # exactly one POST
    rows = attempts(db)
    new_row = next(a for a in rows if a["package_sha256"] != v1_sha)
    assert new_row["state"] == "confirmed"                                             # confirmed from the read-back
    assert by_sha[v1_sha] == next(a for a in rows if a["package_sha256"] == v1_sha)     # v1's own row untouched
    # and the same click again sends nothing
    set_test_csrf(c, token)
    c.post(sends[0].attrs["action"], data={"csrf_token": token}, follow_redirects=False)
    assert len(posts(fake)) == 1


def test_the_advised_flow_is_not_confirmed_when_the_read_back_does_not_list_the_send(env):
    """The state after the single POST comes from PayPal's read, never from the click."""
    c, db, token, fake = env
    v1, v2 = stuck_state(env)
    base = f"/disputes/{DID}/packages/{v2}"
    v1_sha = sha256_json(request_input(db, v1))
    set_test_csrf(c, token)
    c.post(f"{base}/claims", data={"csrf_token": token, "text": V2_EXTRA}, follow_redirects=False)
    set_test_csrf(c, token)
    approve_passive(c, token, v2)
    form = forms_to(parse(page(c, v2).text), "/confirm-not-received")[0]
    r = c.post(form.attrs["action"], data={"csrf_token": form_token(form), "confirmed": "on"})
    assert "1 earlier unconfirmed attempt" in r.text
    send = forms_to(parse(page(c, v2).text), "/send")[0]
    fake.post_send_get_bodies = [send_tests.NOT_YET_BODY] * 6                          # PayPal lists nothing yet
    c.post(send.attrs["action"], data={"csrf_token": form_token(send)}, follow_redirects=False)
    assert len(posts(fake)) == 1
    new_row = next(a for a in attempts(db) if a["package_sha256"] != v1_sha)
    assert new_row["state"] != "confirmed"


# ------------------------------------------------------------------------------------------------------ guard
def test_a_different_statements_unconfirmed_attempt_keeps_the_paused_step_and_the_old_steps(env):
    c, db, token, fake = env
    first = approved(c, db, token, "First package statement.")
    seed_attempt(db, first, "sent_unconfirmed")
    other = new_package(c, token)
    assert repo.list_claims(db, other) == []
    html = page(c, other).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "other_pending"
    assert ADVICE not in text
    block = parse(html).find("details", id="why-blocked")
    assert "1. Press Check again." in block.text() and "3. If the earlier send is not there" in block.text()
    assert "Change a statement in this package" not in block.text()
    form = forms_to(parse(html), "/confirm-not-received")[0]
    age_attempts(db)
    r = c.post(form.attrs["action"], data={"csrf_token": form_token(form), "confirmed": "on"})
    assert "1 earlier unconfirmed attempt" in r.text and EARLY_MARK not in r.text
    assert [a["state"] for a in attempts(db)] == ["abandoned"]
