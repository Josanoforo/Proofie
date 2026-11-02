"""D-101 S4 (Jonas: "One control"): with COPILOT_FEATURE_SELLER_SHORTCUTS the per-statement ticks become one control,
"I have read these N statements". All N statements and their sources stay in view above it; the review hash binds exactly
those N; a statement more, less or edited refuses the approval; the send stays a separate click."""
import json
import re
import sqlite3

import pytest
from _dom import parse

from copilot.db import repo
from copilot.features.active_approval import service, store

from .test_review import DID, choices, prepared_web  # noqa: F401

FLAG = "COPILOT_FEATURE_SELLER_SHORTCUTS"
NOTICE = "Synthetic notice: the delivery date is before the shipping date."


def _url(package):
    return f"/disputes/{DID}/packages/{package}/active-approval"


def _more_statements(db, package, count, start=1):
    for index in range(start, start + count):
        repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": index, "text": f"Synthetic statement number {index}.",
                                        "source_ids": [], "basis": "seller_statement"}])


def _one_control_form(web, fake, db, package, **extra):
    page = web.get(_url(package))
    review = service.read(fake, db, DID, package)
    form = {"csrf_token": web.cookies["csrf_token"], "review_sha256": review.review_sha256,
            "read_statements": str(len(review.claims))}
    form.update(extra)
    return page, review, form


@pytest.fixture
def one_control(prepared_web, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    web, fake, db, package, _ = prepared_web
    _more_statements(db, package, 6)                       # 7 statements in all, as a real package
    return web, fake, db, package


def test_the_page_has_no_ticks_and_one_control_that_names_the_count(one_control):
    web, fake, db, package = one_control
    page = web.get(_url(package)).text
    n = len(service.read(fake, db, DID, package).claims)
    assert n == 7
    assert not re.search(r'name="claim-', page) and 'type="checkbox"' not in page
    controls = re.findall(r"<button[^>]*>\s*(Approve this package: I have read these (\d+) statements?)\s*</button>", page)
    assert len(controls) == 1 and controls[0][1] == "7"
    assert re.search(r'<input type="hidden" name="read_statements" value="7">', page)


def test_every_statement_is_rendered_in_full_above_the_control_and_not_collapsed(one_control):
    web, fake, db, package = one_control
    page = web.get(_url(package)).text
    review = service.read(fake, db, DID, package)
    control = page.index("I have read these 7 statements")
    for claim in review.claims:
        assert page.count(claim.text) >= 1 and page.index(claim.text) < control          # in the DOM, above the control
    dom = parse(page)
    cards = dom.find_all("li", class_="claim-card")
    assert len(cards) == 7
    for card in cards:                                      # none of them sits inside a collapsed <details>
        node = card.parent
        while node is not None:
            assert node.tag != "details" or "open" in node.attrs, "a statement is collapsed"
            node = node.parent


def test_flag_off_the_ticks_are_still_there(prepared_web, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    web, fake, db, package, _ = prepared_web
    page = web.get(_url(package)).text
    assert 'name="claim-' in page and "read_statements" not in page and "I have read these" not in page


def test_approving_with_the_one_control_binds_the_hash_and_keeps_one_confirmation_per_statement(one_control):
    web, fake, db, package = one_control
    page, review, form = _one_control_form(web, fake, db, package)
    done = web.post(_url(package), data=form, follow_redirects=False)
    assert done.status_code == 303
    approval = repo.get_latest_valid_approval(db, package)
    assert approval["approval_sha256"] == review.fingerprint
    receipt = json.loads(store.receipt(db, approval["id"])["confirmations_json"])
    assert len(receipt) == 7 and {r["key"] for r in receipt} == {c.key for c in review.claims}
    assert receipt[0]["read_control"] == {"statements_read": 7}
    assert all(method == "GET" for method, _, _ in fake.calls)             # nothing was written to PayPal


def test_one_statement_more_or_edited_changes_the_hash_and_refuses_the_approval(one_control):
    web, fake, db, package = one_control
    base = service.read(fake, db, DID, package)
    _page, _review, form = _one_control_form(web, fake, db, package)
    _more_statements(db, package, 1, start=50)             # a statement more, after the page was loaded
    more = service.read(fake, db, DID, package)
    assert len(more.claims) == 8 and more.fingerprint != base.fingerprint and more.review_sha256 != base.review_sha256
    assert web.post(_url(package), data=form).status_code == 400
    assert repo.get_latest_valid_approval(db, package) is None
    page, review, fresh = _one_control_form(web, fake, db, package)      # a fresh page counts 8 and is accepted
    assert fresh["read_statements"] == "8"
    repo.mark_claim_edited(db, repo.list_claims(db, package)[0]["id"], "A statement the seller changed after the page loaded.")
    edited = service.read(fake, db, DID, package)
    assert edited.fingerprint != more.fingerprint
    assert web.post(_url(package), data=dict(fresh, csrf_token=web.cookies["csrf_token"])).status_code == 400
    assert repo.get_latest_valid_approval(db, package) is None


def test_one_statement_less_changes_the_hash_and_refuses_the_approval(one_control):
    web, fake, db, package = one_control
    base = service.read(fake, db, DID, package)
    _page, _review, form = _one_control_form(web, fake, db, package)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM claims WHERE id=?", (repo.list_claims(db, package)[-1]["id"],))
    less = service.read(fake, db, DID, package)
    assert len(less.claims) == 6 and less.fingerprint != base.fingerprint
    assert web.post(_url(package), data=form).status_code == 400
    assert repo.get_latest_valid_approval(db, package) is None


def test_a_count_that_does_not_match_the_statements_is_refused_even_with_a_fresh_hash(one_control):
    web, fake, db, package = one_control
    for wrong in ("6", "8", "0", "x", ""):
        _page, review, form = _one_control_form(web, fake, db, package)
        form["read_statements"] = wrong
        assert web.post(_url(package), data=form).status_code == 400
        assert repo.get_latest_valid_approval(db, package) is None


def test_mixing_the_control_with_per_statement_decisions_is_refused(one_control):
    web, fake, db, package = one_control
    _page, review, form = _one_control_form(web, fake, db, package)
    form.update({"claim-" + k: v for k, v in choices(review).items()})
    assert web.post(_url(package), data=form).status_code == 400


def test_the_note_is_still_needed_and_comes_proposed(one_control):
    web, fake, db, package = one_control
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE packages SET review_flags_json=? WHERE id=?", (json.dumps([NOTICE]), package))
    page, review, form = _one_control_form(web, fake, db, package)
    assert "Proposed from the open notices" in page.text
    assert web.post(_url(package), data=dict(form, sufficiency_override="")).status_code == 400
    assert repo.get_latest_valid_approval(db, package) is None
    page, review, form = _one_control_form(web, fake, db, package)
    form["sufficiency_override"] = "Open notices read against the sources: " + NOTICE.rstrip(".") + "."
    assert web.post(_url(package), data=form, follow_redirects=False).status_code == 303


def test_a_statement_without_a_document_still_says_it_is_the_sellers_own(one_control):
    web, fake, db, package = one_control
    page = web.get(_url(package)).text
    assert page.count("Your own statement. No document is cited.") >= 6      # the six statements added above


def test_reading_the_page_writes_nothing(one_control):
    web, fake, db, package = one_control
    web.get(_url(package))
    assert repo.get_latest_valid_approval(db, package) is None
