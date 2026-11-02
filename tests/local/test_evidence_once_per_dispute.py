"""Proofie sends evidence once per dispute: it never sends while PayPal lists evidence submitted by the seller (the
send path refuses it before any POST). The package and dispute pages must say that, instead of promising a send that the
rule then refuses. Offline only: fake PayPal clients, a temp database per test, DOM and stored-row checks."""
import re

import pytest
from _dom import parse
from _hosted import set_test_csrf

from copilot.web import links, next_step

import test_routes_send as send_tests
from test_rc42_package import V2_CHANGED, forms_to, v1_partial
from test_ux_rc41_fix_state import (LABEL_A, OTHER, STATEMENT, add_file, confirmed_body, posts, seed_confirmed,
                                    two_versions_same_statements)
from test_ux_rc41_fix_state_status import panel, seed_attempt
from test_ux_rc41_package import (DID, add_claims, approve_passive, approved, draft, env, mark_attempt, mark_sent,  # noqa: F401
                                  new_package, page)

pytestmark = [pytest.mark.usefixtures("web_mode")]

RULE = ("PayPal already lists evidence from you for this dispute. Proofie sends evidence once per dispute and will not "
        "send again.")


def list_seller_evidence(fake, db, pkg, names):
    """PayPal now lists evidence submitted by the seller (the statements of `pkg`, with the named files only)."""
    body = {**send_tests.WAITING, **confirmed_body(db, pkg, names)}
    fake.detail_body = fake.pre_send_get_body = fake.recovery_get_body = body


def resolution_links(html):
    return [a for a in parse(html).find_all("a") if a.attrs.get("href") == links.RESOLUTION_CENTER_URL]


def no_way_to_send(html, fake):
    dom = parse(html)
    assert not forms_to(dom, "/send") and not forms_to(dom, "/approve")
    assert not forms_to(dom, "/confirm-not-received")
    assert "Start a new submission" not in html and "then send this package" not in html
    assert posts(fake) == []


# --------------------------------------------------------------------------------------------- the helper and the rule
def test_the_helper_reads_the_normalised_key_and_tolerates_a_missing_one():
    assert next_step.seller_evidence_listed({"evidence_submitted_by_seller": ["PROOF_OF_FULFILLMENT"]}) is True
    assert next_step.seller_evidence_listed({"evidence_submitted_by_seller": []}) is False
    assert next_step.seller_evidence_listed({}) is False and next_step.seller_evidence_listed(None) is False
    assert next_step.ONCE_PER_DISPUTE == RULE
    assert all(len(sentence.split()) < 25 for sentence in re.split(r"(?<=\.) ", RULE))


# ----------------------------------------------------------------------------------------------- 1. the partial send
@pytest.mark.parametrize("v2_text", [STATEMENT, V2_CHANGED], ids=["same-statements", "changed-statement"])
def test_a_partial_earlier_send_says_the_rule_and_offers_no_new_submission(env, v2_text):
    c, db, token, fake = env
    v1, v2 = v1_partial(env, v2_text=v2_text)
    list_seller_evidence(fake, db, v1, ["a.pdf"])                              # PayPal lists the statements, not b.pdf
    for html in (page(c, v2).text, c.get(f"/disputes/{DID}").text):
        text = parse(html).find("main").text()
        assert RULE in text
        assert "b.pdf" in text
        assert resolution_links(html)
        no_way_to_send(html, fake)
        assert "I want to send this package" not in html and "as a new submission" not in text
    # the page keeps a Check again that only reads
    assert any(f.attrs["action"].endswith("/check") for f in parse(page(c, v2).text).find_all("form"))


def test_the_backend_mark_still_records_when_posted_directly(env):
    from test_ux_rc41_fix3_state3 import attempts, form_token
    c, db, token, fake = env
    v1, v2 = v1_partial(env, v2_text=V2_CHANGED)
    list_seller_evidence(fake, db, v1, ["a.pdf"])
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v2}/confirm-not-received", data={"csrf_token": token, "confirmed": "on"})
    assert r.status_code == 200 and [a["state"] for a in attempts(db)] == ["abandoned"]   # the route is untouched
    assert posts(fake) == []


def _abandoned_partial_pages(env, listed):
    from copilot.core import send_service
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "abandoned", reason=send_service.ABANDONED_PARTIAL_REASON)
    if listed:
        list_seller_evidence(fake, db, v1, [])
    return [panel(page(c, v1).text)[1], panel(c.get(f"/disputes/{DID}").text)[1]], fake


def test_an_abandoned_true_partial_send_says_the_rule_on_both_pages_when_paypal_lists_evidence(env):
    texts, fake = _abandoned_partial_pages(env, True)
    for text in texts:
        assert RULE in text and "change it first" not in text and "makes a new version" not in text
    assert posts(fake) == []


def test_an_abandoned_true_partial_send_keeps_the_way_out_when_paypal_lists_nothing(env):
    texts, fake = _abandoned_partial_pages(env, False)
    for text in texts:
        assert "change it first" in text and "Proofie sends evidence once per dispute" not in text
    assert posts(fake) == []


# --------------------------------------------------------------------------------------- 2. the confirmed collisions
@pytest.mark.parametrize("v2_files", [[("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)]],
                         ids=["same-files", "other-files"])
def test_a_confirmed_collision_says_the_rule_and_has_no_new_submission(env, v2_files):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], v2_files)
    list_seller_evidence(fake, db, v1, ["a.pdf"])
    for html in (page(c, v2).text, c.get(f"/disputes/{DID}").text):
        node, text = panel(html)
        assert node.attrs["data-next-step"] in ("same_statements", "evidence_listed")
        assert RULE in text and "new submission" not in text and "makes a new" not in text
        assert not forms_to(parse(html), "/send")
    assert "View the dispute and PayPal's answer" in panel(page(c, v2).text)[1] or resolution_links(page(c, v2).text)
    assert posts(fake) == []


# ----------------------------------------------------------------- 3. confirmed and later-file: no new version offered
def test_a_confirmed_send_offers_a_new_version_only_when_paypal_lists_no_seller_evidence(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_sent(db, pkg)
    # synthetic: provide-evidence is offered and no seller evidence is listed -> unchanged
    html = page(c, pkg).text
    assert "Prepare a new version" in html and "A new version is a new submission to PayPal" in html
    # PayPal lists seller evidence (always the case after a real send) -> the rule, no new version
    list_seller_evidence(fake, db, pkg, [])
    for html in (page(c, pkg).text, c.get(f"/disputes/{DID}").text):
        assert "Prepare a new version" not in html and "new submission" not in html
        assert RULE in parse(html).find("main").text()
    assert posts(fake) == []


def test_a_later_file_state_keeps_not_asking_when_paypal_does_not_ask_and_the_rule_when_it_does(env):
    from test_ux_rc41_fix_state_status import add_late_file
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_sent(db, pkg)
    add_late_file(db, pkg)
    list_seller_evidence(fake, db, pkg, [])
    html = page(c, pkg).text
    assert panel(html)[0].attrs["data-next-step"] == "later_file"
    assert "Prepare a new version" not in html and RULE in panel(html)[1]
    # PayPal offers nothing and still lists the evidence: the rule is the answer
    fake.detail_body = {**fake.detail_body, "links": [x for x in fake.detail_body["links"] if x["rel"] != "provide_evidence"]}
    assert RULE in panel(page(c, pkg).text)[1]
    # PayPal offers nothing and lists nothing: it is not asking
    fake.detail_body = {**send_tests.WAITING, "links": [x for x in send_tests.WAITING["links"] if x["rel"] != "provide_evidence"]}
    assert next_step.NOT_ASKING_NOTE in panel(page(c, pkg).text)[1]


# ------------------------------------------------------------------ 4. a newer version while PayPal lists seller evidence
def test_a_newer_version_is_a_stop_step_on_both_pages_and_the_task_rows_agree(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    set_test_csrf(c, token)
    approve_passive(c, token, v2)
    list_seller_evidence(fake, db, v1, [])
    pkg_html = page(c, v2).text
    node, text = panel(pkg_html)
    assert node.attrs["data-next-step"] == "evidence_listed" and RULE in text
    assert resolution_links(pkg_html)
    no_way_to_send(pkg_html, fake)
    assert "Review each statement and approve" not in text and "Send evidence to PayPal" not in pkg_html
    dispute_html = c.get(f"/disputes/{DID}").text
    dnode, dtext = panel(dispute_html)
    assert dnode.attrs["data-next-step"] == "evidence_listed" and RULE in dtext
    assert resolution_links(dispute_html)
    rows = parse(dispute_html).find_all("tr", class_="task-row")
    send_row = next(r for r in rows if r.attrs.get("data-task") == "send")
    assert send_row.attrs["data-status"] == "blocked" and RULE in send_row.text()
    approve_row = next(r for r in rows if r.attrs.get("data-task") == "approve")
    assert approve_row.attrs["data-status"] == "blocked"
    assert posts(fake) == []


def test_without_seller_evidence_a_newer_version_still_reaches_review_and_send(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    set_test_csrf(c, token)
    approve_passive(c, token, v2)
    node, text = panel(page(c, v2).text)
    assert node.attrs["data-next-step"] != "evidence_listed" and RULE not in text


# --------------------------------------------------------------------------------------- the two sentences in the pages
def _earlier_send_pages(env, listed):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    if listed:
        list_seller_evidence(fake, db, v1, [])
    return parse(page(c, v2).text).find("main").text(), c.get(f"/disputes/{DID}").text


def test_the_earlier_send_banner_and_the_confirmed_list_say_the_rule_once_when_paypal_lists_evidence(env):
    pkg_text, dispute = _earlier_send_pages(env, True)
    assert "Sending this version would be a new submission" not in pkg_text
    assert pkg_text.count(RULE) >= 1
    assert "a new version is a new submission" not in dispute
    node = parse(dispute).find("p", data_state="evidence-listed-once")
    assert node is not None and node.text() == RULE and dispute.count('data-state="evidence-listed-once"') == 1


def test_the_earlier_send_banner_and_the_confirmed_list_end_at_the_content_sentence_when_nothing_is_listed(env):
    pkg_text, dispute = _earlier_send_pages(env, False)
    assert "That confirmation covers only the content sent then, not this version." in pkg_text
    assert "once per dispute" not in pkg_text.split("An earlier version of this package")[1].split("Where this package stands")[0]
    assert "A confirmation covers only the content sent then." in dispute
    assert "evidence-listed-once" not in dispute


# ------------------------------------------------------------------- the strings of the send core that answer after a send
def test_the_send_core_messages_are_true_whether_or_not_the_read_lists_evidence():
    from copilot.core import send_service
    assert send_service.ONCE_PER_DISPUTE == RULE and next_step.ONCE_PER_DISPUTE is send_service.ONCE_PER_DISPUTE
    assert RULE.endswith(send_service.ONCE_PER_DISPUTE_RULE)
    assert send_service.ALREADY_RECEIVED_MESSAGE == ("PayPal previously confirmed this exact package as received. "
                                                     "Nothing new was sent. Proofie does not send the same package twice.")
    for text in (send_service.ALREADY_RECEIVED_MESSAGE, send_service._ABANDONED_OUTCOME.message):
        assert "new submission" not in text and "new review" not in text and "PayPal already lists" not in text
    assert send_service.ONCE_PER_DISPUTE in send_service._ABANDONED_PARTIAL_LISTED_OUTCOME.message
    assert "change it first" in send_service._ABANDONED_PARTIAL_OUTCOME.message


def test_a_repeat_send_of_a_confirmed_package_answers_by_the_read_and_the_package(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)])
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = c.get(r.headers["location"])
    assert res.status_code == 409 and "cannot send them again with another file" in res.text
    assert "Change a statement to make a new submission" in res.text and "PayPal already lists" not in res.text   # nothing listed
    list_seller_evidence(fake, db, v1, ["a.pdf"])                                    # now the read lists it: the rule
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = c.get(r.headers["location"])
    assert RULE in res.text and "make a new submission" not in res.text
    assert posts(fake) == []
    pkg = approved(c, db, token, "A statement that PayPal confirmed.")
    mark_sent(db, pkg)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    res = c.get(r.headers["location"])
    assert "Package already received" in res.text and "Proofie does not send the same package twice" in res.text
    assert "new review" not in res.text and posts(fake) == []


# ----------------------------------------------------------------- one title for a send that PayPal has not confirmed
@pytest.mark.parametrize("stored", ["pending", "sent_unconfirmed"])
def test_the_package_page_titles_an_unconfirmed_send_like_the_result_page(env, stored):
    from copilot.web import routes_send
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    seed_attempt(db, pkg, stored)
    dom = parse(page(c, pkg).text)
    banner = next(n for n in dom.find_all("div") if "banner" in n.attrs.get("class", "").split()
                  and n.attrs.get("data-state") == "result-uncertain")
    assert routes_send.UNCERTAIN_HEADLINE == "Send attempted - not confirmed by PayPal"
    assert banner.find("strong").text().endswith(routes_send.UNCERTAIN_HEADLINE) or routes_send.UNCERTAIN_HEADLINE in banner.text()
    assert "Result pending" not in page(c, pkg).text


# ---------------------------------------------------------------------- a claim under review: no invitation in the checks
NEUTRAL = "PayPal is reviewing this claim; nothing can be added now."


def _why_cells(html):
    return {r.attrs["data-check"]: r.find("td", data_label="Why").text()
            for r in parse(html).find_all("tr") if "data-check" in r.attrs}


def test_a_claim_under_review_replaces_the_invitations_of_the_checks_with_one_neutral_phrase(env, monkeypatch):
    c, db, token, fake = env
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    pkg = new_package(c, token)                                              # no tracking number, no carrier, no documents
    add_claims(db, pkg, STATEMENT)
    ordinary = _why_cells(page(c, pkg).text)
    assert any("Add the carrier's proof" in w for w in ordinary.values())    # the ordinary page keeps its invitations
    links_ = [x for x in send_tests.WAITING["links"] if x["rel"] != "provide_evidence"]
    fake.detail_body = {**send_tests.WAITING, "status": "UNDER_REVIEW", "dispute_life_cycle_stage": "CHARGEBACK",
                        "links": links_}
    reviewing = _why_cells(page(c, pkg).text)
    assert reviewing.keys() == ordinary.keys()
    changed = {k for k in reviewing if reviewing[k] != ordinary[k]}
    assert changed and all(reviewing[k] == NEUTRAL for k in changed)
    assert not any("Add the carrier's proof" in w or "You can still reply honestly" in w for w in reviewing.values())
    assert posts(fake) == []
    # without the outcome feature the state is not "reviewing": the invitations stay
    monkeypatch.delenv("COPILOT_FEATURE_OUTCOME")
    assert any("Add the carrier's proof" in w for w in _why_cells(page(c, pkg).text).values())


# ------------------------------------------------------------------ nothing listed: the earlier way out stays
def _text_of_both(c, v2):
    return [panel(page(c, v2).text)[1], panel(c.get(f"/disputes/{DID}").text)[1]]


def test_a_partial_earlier_send_keeps_the_way_out_when_paypal_lists_no_seller_evidence(env):
    c, db, token, fake = env
    v1, v2 = v1_partial(env)
    html = page(c, v2).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "same_statements" and "PayPal already lists" not in text
    assert "Change a statement" in text and "press Start a new submission, then press Send evidence to PayPal" in text
    assert "the file b.pdf" in text
    block = parse(html).find("details", id="why-blocked")
    form = forms_to(block, "/confirm-not-received")[0]
    assert [b.text() for b in form.find_all("button")] == ["Start a new submission"]
    assert "I want to send this package as a new submission" in form.text()
    assert "Press Send evidence to PayPal" in block.text() or "press Send evidence to PayPal" in block.text()
    assert "PayPal already lists" not in html
    assert posts(fake) == []


def test_a_confirmed_collision_keeps_change_a_statement_when_paypal_lists_no_seller_evidence(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A)])
    for text in _text_of_both(c, v2):
        assert "Change a statement" in text and "A changed statement makes a new submission" in text
        assert "PayPal already lists" not in text
    send_row = parse(c.get(f"/disputes/{DID}").text).find("tr", data_task="send")
    assert "Change a statement" in send_row.text() and "once per dispute" not in send_row.text()


@pytest.mark.parametrize("v2_files", [[("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)]],
                         ids=["same-files", "other-files"])
def test_both_pages_choose_the_same_next_step_for_a_confirmed_collision_when_paypal_lists_evidence(env, v2_files):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], v2_files)
    list_seller_evidence(fake, db, v1, ["a.pdf"])
    package_node, package_text = panel(page(c, v2).text)
    dispute_node, dispute_text = panel(c.get(f"/disputes/{DID}").text)
    assert package_node.attrs["data-next-step"] == dispute_node.attrs["data-next-step"] == "same_statements"
    assert package_text.split(".")[0] == dispute_text.split(".")[0]


# ------------------------------------------------------------------ unconfirmed (non-partial) earlier send
def test_an_unconfirmed_earlier_send_ends_at_the_rule_and_check_again_when_paypal_lists_evidence(env):
    from test_rc42_package import twin_unconfirmed
    c, db, token, fake = env
    v1, v2 = twin_unconfirmed(env)
    list_seller_evidence(fake, db, v1, ["a.pdf"])
    package_html = page(c, v2).text
    assert any(f.attrs["action"].endswith("/check") for f in parse(package_html).find_all("form"))
    for html in (package_html, c.get(f"/disputes/{DID}").text):
        node, text = panel(html)
        assert RULE in text and "Change a statement" not in text and "then send this package" not in html
        assert "mark the earlier send as not received" not in text
        assert not forms_to(parse(html), "/confirm-not-received") and not forms_to(parse(html), "/send")
        assert resolution_links(html)
    assert posts(fake) == []


def test_an_unconfirmed_earlier_send_of_other_statements_keeps_the_mark_when_nothing_is_listed(env):
    from test_ux_rc41_fix3_state3 import age_attempts
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    age_attempts(db)
    html = page(c, v2).text
    assert forms_to(parse(html), "/confirm-not-received") and "PayPal already lists" not in html


def test_other_pending_with_seller_evidence_listed_has_no_mark_form(env):
    from test_ux_rc41_fix3_state3 import age_attempts
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    age_attempts(db)
    list_seller_evidence(fake, db, v1, [])
    html = page(c, v2).text
    block = parse(html).find("details", id="why-blocked")
    assert RULE in block.text() and not forms_to(block, "/confirm-not-received")
    assert parse(html).find("p", data_state="evidence-listed-link") is not None


# ------------------------------------------------------------------ the banners after the rule's own refusal
def test_the_blocked_banner_after_the_rules_own_refusal_never_says_send_again(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "blocked", reason="evidence already submitted by the seller: ['PROOF_OF_FULFILLMENT']")
    list_seller_evidence(fake, db, pkg, [])
    html = page(c, pkg).text
    main = parse(html).find("main").text()
    for phrase in ("then send again", "approve and send again", "edit it first", "Edit it (that makes a new version)"):
        assert phrase not in main, phrase
    assert RULE in main
    assert posts(fake) == []


def test_the_failed_and_abandoned_banners_keep_their_way_out_when_nothing_is_listed(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "blocked", reason="no link")
    main = parse(page(c, pkg).text).find("main").text()
    assert "Fix what the message says, then send again." in main and "PayPal already lists" not in main


# ------------------------------------------------------------------ the package page of a partial send, listed
def test_the_package_page_of_a_version_whose_own_send_was_partial_says_what_the_dispute_page_says(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    add_file(c, db, token, v1, "b.pdf", OTHER)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    list_seller_evidence(fake, db, v1, ["a.pdf"])
    pkg_html = page(c, v1).text
    dispute_html = c.get(f"/disputes/{DID}").text
    for html in (pkg_html, dispute_html):
        node, text = panel(html)
        assert node.attrs["data-state"] == "evidence-listed" and RULE in text
        assert "the file b.pdf" in text and resolution_links(html)
    assert RULE in parse(pkg_html).find("div", data_state="partial").text()


# ------------------------------------------------------------------ the dispute page: new-version control and summary
def test_the_new_package_version_control_is_absent_when_listed_and_present_when_not(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_sent(db, pkg)
    open_page = parse(c.get(f"/disputes/{DID}/workspace/prepare").text)
    details = open_page.find("details", data_state="new-version")
    assert details is not None and "Prepare a new package version" in details.text()
    list_seller_evidence(fake, db, pkg, [])
    listed_page = parse(c.get(f"/disputes/{DID}/workspace/prepare").text)
    assert listed_page.find("details", data_state="new-version") is None


def test_the_no_evidence_case_still_reaches_the_send_control(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    node, text = panel(page(c, pkg).text)
    assert node.attrs["data-next-step"] == "send" and forms_to(parse(page(c, pkg).text), "/send")


def test_the_stop_step_has_one_body_on_both_pages_and_the_dispute_page_links_the_package(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    list_seller_evidence(fake, db, v1, [])
    package_node, package_text = panel(page(c, v2).text)
    dispute_node, dispute_text = panel(c.get(f"/disputes/{DID}").text)
    assert package_node.attrs["data-next-step"] == dispute_node.attrs["data-next-step"] == "evidence_listed"
    for text in (package_text, dispute_text):
        assert next_step.STOP_BODY in text
    assert any(a.attrs["href"].endswith(f"/packages/{v2}") for a in dispute_node.find_all("a"))
    assert not any(a.attrs["href"] == f"/disputes/{DID}" for a in dispute_node.find_all("a"))


def test_the_dispute_summary_does_not_invite_a_new_package_when_listed(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_sent(db, pkg)
    list_seller_evidence(fake, db, pkg, [])
    html = c.get(f"/disputes/{DID}").text
    assert "Nothing to do unless you prepare a new package" not in html
    assert "No evidence package yet" not in html


def test_task_rows_of_a_rule_block_read_not_available(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    list_seller_evidence(fake, db, v1, [])
    rows = parse(c.get(f"/disputes/{DID}").text).find_all("tr", class_="task-row")
    blocked = [r for r in rows if r.attrs["data-status"] == "blocked"]
    assert blocked, "no blocked row"
    # the tag reads "Not available" (the row's own blocked prefix stays, as the matrix of rows expects it)
    assert all(r.find("span", class_="tag").text().endswith("Not available") for r in blocked)


def test_task_rows_apply_the_rule_when_every_waiting_attempt_is_partial(env):
    c, db, token, fake = env
    v1, v2 = v1_partial(env, v2_text=V2_CHANGED)
    list_seller_evidence(fake, db, v1, ["a.pdf"])
    rows = parse(c.get(f"/disputes/{DID}").text).find_all("tr", class_="task-row")
    send_row = next(r for r in rows if r.attrs["data-task"] == "send")
    assert send_row.attrs["data-status"] == "blocked" and "Proofie sends evidence once per dispute" in send_row.text()


# ------------------------------------------------------------------ a claim under review, package page
def test_a_claim_under_review_checks_before_the_settling_wait_and_drops_the_add_links(env, monkeypatch):
    c, db, token, fake = env
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    pkg = new_package(c, token)
    add_claims(db, pkg, STATEMENT)
    fake.detail_body = {**send_tests.WAITING, "status": "UNDER_REVIEW", "dispute_life_cycle_stage": "CHARGEBACK",
                        "links": [x for x in send_tests.WAITING["links"] if x["rel"] != "provide_evidence"]}
    html = page(c, pkg).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "claim_under_review"
    dom = parse(html)
    inviting = [r for r in dom.find_all("tr") if "data-check" in r.attrs
                and r.find("td", data_label="Why").text() == next_step_neutral()]
    assert inviting
    for row in inviting:
        assert row.find("td", data_label="What you can do").find_all("a") == []


def next_step_neutral():
    from copilot.web.dispute_state import REVIEWING_NEUTRAL_TEXT
    return REVIEWING_NEUTRAL_TEXT


# ================================================================== the not-listed counterparts and the remaining edges
def list_other_evidence(fake):
    """PayPal lists evidence from the seller that is not this attempt (typed in PayPal), so a reconcile leaves it alone."""
    body = {**send_tests.WAITING, "evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                                                 "evidence_info": {"tracking_info": [{"tracking_number": "OTHER-TRK-1"}]},
                                                 "notes": "typed in PayPal", "date": "2026-10-05T00:00:00Z"}]}
    fake.detail_body = fake.pre_send_get_body = fake.recovery_get_body = body


def test_a_changed_statement_after_a_partial_send_keeps_the_tick_box_when_nothing_is_listed(env):
    c, db, token, fake = env
    v1, v2 = v1_partial(env, v2_text=V2_CHANGED)
    html = page(c, v2).text
    block = parse(html).find("details", id="why-blocked")
    text = block.text()
    assert "Sending is paused: an earlier send reached PayPal without every file" in text
    form = forms_to(block, "/confirm-not-received")[0]
    assert "The earlier send is incomplete there, and I want to send this package as a new submission." in form.text()
    assert [b.text() for b in form.find_all("button")] == ["Start a new submission"]
    assert "PayPal already lists" not in html
    node, step_text = panel(html)
    assert node.attrs["data-next-step"] == "other_pending"
    assert "Sending is paused: an earlier send reached PayPal without every file." in step_text
    dispute_node, dispute_text = panel(c.get(f"/disputes/{DID}").text)
    assert dispute_node.attrs["data-next-step"] == "other_pending" and "PayPal already lists" not in dispute_text
    assert posts(fake) == []


def test_a_partial_earlier_send_with_the_same_statements_keeps_the_way_out_on_both_pages(env):
    c, db, token, fake = env
    v1, v2 = v1_partial(env)
    for html in (page(c, v2).text, c.get(f"/disputes/{DID}").text):
        node, text = panel(html)
        assert node.attrs["data-next-step"] == "same_statements"
        assert "PayPal showed these statements without every file." in text and "Change a statement" in text
        assert "When Proofie checked, PayPal showed the statements of package v1 without the file b.pdf." in text
        assert "press Start a new submission, then press Send evidence to PayPal" in text
        assert "PayPal already lists" not in html
    assert posts(fake) == []


@pytest.mark.parametrize("listed", [True, False], ids=["listed", "not-listed"])
@pytest.mark.parametrize("state, way_out", [
    ("failed", "Fix what the message says, then approve and send again."),
    ("blocked", "Fix what the message says, then send again."),
    ("abandoned", "To send this content you need to edit it first"),
    ("abandoned_partial", "To send this content, change it first: that makes a new version to review and approve."),
])
def test_the_banners_after_a_refused_blocked_or_abandoned_send_follow_the_read(env, state, way_out, listed):
    from copilot.core import send_service
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    reason = {"abandoned": send_service.ABANDONED_REASON, "abandoned_partial": send_service.ABANDONED_PARTIAL_REASON,
              "blocked": "no link"}.get(state)
    seed_attempt(db, v1, "abandoned" if state.startswith("abandoned") else state, reason=reason)
    if listed:
        list_other_evidence(fake)
    main = parse(page(c, v1).text).find("main").text()
    if listed:
        assert RULE in main
        for phrase in ("Fix what the message says", "approve and send again", "then send again", "edit it first",
                       "change it first", "Edit it (that makes a new version)"):
            assert phrase not in main, phrase
    else:
        assert way_out in main and "PayPal already lists" not in main
    assert posts(fake) == []


def test_a_post_while_paypal_lists_evidence_shows_the_rule_not_the_raw_precondition_text(env):
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    list_other_evidence(fake)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    res = c.get(r.headers["location"])
    main = " ".join(parse(res.text).find("main").text().split())
    assert RULE in main and "evidence already submitted by the seller" not in main and "PROOF_OF_FULFILLMENT" not in main
    assert posts(fake) == []


def test_the_stored_refusal_reason_is_told_in_seller_words_on_the_package_page_and_by_the_helper(env):
    from copilot.core import send_service
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "blocked", reason="evidence already submitted by the seller: ['PROOF_OF_FULFILLMENT']")
    html = page(c, pkg).text
    assert "evidence already submitted by the seller" not in html
    assert "PayPal listed evidence from you when Proofie last tried; it lists none now." in html       # nothing is listed now
    assert send_service.seller_words("Not sent: evidence already submitted by the seller: ['X']") == (
        "Not sent: PayPal listed evidence from you when Proofie last tried.")
    assert send_service.seller_words("Nothing to change here.", True) == "Nothing to change here."


def test_the_earlier_send_banner_holds_the_rule_inside_its_own_node_and_the_dispute_list_says_it_once(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    set_test_csrf(c, token)
    approve_passive(c, token, v2)
    seed_confirmed(db, v2, request_id="req-v2")                             # two confirmed attempts on the dispute
    list_seller_evidence(fake, db, v1, [])
    v3 = new_package(c, token)
    add_claims(db, v3, "A third statement.")
    banner = parse(page(c, v3).text).find("div", data_state="earlier-send-confirmed")
    assert banner is not None and RULE in banner.text()
    dispute = c.get(f"/disputes/{DID}").text
    assert dispute.count('data-state="evidence-listed-once"') == 1 and dispute.count("Your send on") == 2


@pytest.mark.parametrize("offered", [True, False], ids=["offered", "self-link-only"])
def test_a_claim_under_review_is_the_same_step_on_both_pages_also_when_only_the_self_link_remains(env, monkeypatch, offered):
    c, db, token, fake = env
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    pkg = approved(c, db, token, STATEMENT)
    fake.detail_body = fake.pre_send_get_body = fake.recovery_get_body = {
        **send_tests.WAITING, "status": "UNDER_REVIEW", "dispute_life_cycle_stage": "CHARGEBACK",
        "links": [x for x in send_tests.WAITING["links"] if offered or x["rel"] == "self"]}
    package_node, package_text = panel(page(c, pkg).text)
    dispute_node, dispute_text = panel(c.get(f"/disputes/{DID}").text)
    assert package_node.attrs["data-next-step"] == dispute_node.attrs["data-next-step"] == "claim_under_review"
    assert "Proofie offers no seller action while the review lasts." in package_text
    assert package_text == dispute_text
    assert [a.text() for a in package_node.find_all("a")] == [a.text() for a in dispute_node.find_all("a")] == [
        "Read PayPal's answer"]


def test_the_dispute_summary_with_no_documents_and_no_package_follows_the_read(env):
    c, db, token, fake = env
    plain = c.get(f"/disputes/{DID}").text
    assert "No documents uploaded yet." in plain and "No evidence package yet." in plain
    assert "start a package to compare" in plain
    list_other_evidence(fake)
    listed_page = c.get(f"/disputes/{DID}").text
    assert "No evidence package yet." not in listed_page and "start a package to compare" not in listed_page
    assert RULE in listed_page and parse(listed_page).find("form", action=f"/disputes/{DID}/documents") is None


def test_the_drift_note_after_a_confirmed_send_follows_the_read():
    from copilot.core.checklist import Check
    from copilot.web import routes_disputes, status
    conflict = Check(id="destination", label_for_seller="Delivery place", state="conflict", basis="published_policy",
                     basis_ref="r", sources=[], action="review_difference", risk=False, seller_message="m")
    record = status.SendRecord(state="confirmed", confirmed_at="2026-10-05T00:00:00-0600")
    package = {"id": 1, "version": 1, "source": "human_edit"}
    infos = {1: {"record": record, "claims": [], "attached": [], "summary": {}, "approval_valid": False,
                 "has_tracking": True, "package": package}}
    attempts = [{"state": "confirmed", "ts": "2026-10-05T00:00:00-0600", "package_sha256": "a", "request_id": "r"}]
    base = {"dispute_id": DID, "can_provide_evidence": True, "evidence_requested_from_seller": []}
    for listed in (False, True):
        norm = {**base, "evidence_submitted_by_seller": ["PROOF_OF_FULFILLMENT"] if listed else []}
        summary = routes_disputes._detail_summary(norm, [], [package], attempts, checks=[conflict], infos=infos, db_path=None)
        assert ("prepare a new package" in summary["drift"]) is (not listed)
        assert (next_step.ONCE_PER_DISPUTE in summary["drift"]) is listed
