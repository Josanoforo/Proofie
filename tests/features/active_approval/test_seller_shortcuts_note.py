"""D-101 seller item 1: the approval page proposes the note for the open notices (COPILOT_FEATURE_SELLER_SHORTCUTS).
The ticks stay: every statement still needs its own confirmation, and the approval is bound to the same hash as before."""
import json
import re
import sqlite3

import pytest

from copilot.db import repo
from copilot.features.active_approval import service, store

from .test_review import DID, choices, prepared_web  # noqa: F401

FLAG = "COPILOT_FEATURE_SELLER_SHORTCUTS"
NOTICE = "Synthetic notice: the delivery date is before the shipping date."


def _flag_the_package(db, package, flags=(NOTICE,)):
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE packages SET review_flags_json=? WHERE id=?", (json.dumps(list(flags)), package))


def _override_value(html):
    found = re.search(r'<input id="approval-override"[^>]*value="([^"]*)"', html)
    return found.group(1) if found else None


def _url(package):
    return f"/disputes/{DID}/packages/{package}/active-approval"


def test_with_the_flag_on_the_note_comes_typed_from_the_open_notice_and_says_so(prepared_web, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    web, fake, db, package, _ = prepared_web
    _flag_the_package(db, package)
    page = web.get(_url(package)).text
    value = _override_value(page)
    assert value and "Synthetic notice" in value and value.startswith("Open notices read against the sources")
    assert 'data-state="proposed-note"' in page and "Edit it or replace it" in page


def test_with_the_flag_off_the_note_is_empty_as_before(prepared_web, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    web, fake, db, package, _ = prepared_web
    _flag_the_package(db, package)
    page = web.get(_url(package)).text
    assert _override_value(page) == "" and 'data-state="proposed-note"' not in page


def test_without_an_open_notice_there_is_no_note_field_and_no_hint(prepared_web, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    web, fake, db, package, _ = prepared_web
    page = web.get(_url(package)).text
    assert _override_value(page) is None and 'data-state="proposed-note"' not in page


def test_a_notice_with_private_text_proposes_nothing(prepared_web, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    web, fake, db, package, _ = prepared_web
    _flag_the_package(db, package, ["Write to buyer@example.com about the delivery date."])
    review = service.read(fake, db, DID, package)
    assert service.proposed_note(review) == ""
    assert _override_value(web.get(_url(package)).text) == ""


def test_the_proposed_note_is_capped(prepared_web, monkeypatch):
    web, fake, db, package, _ = prepared_web
    _flag_the_package(db, package, ["x" * 500])
    assert len(service.proposed_note(service.read(fake, db, DID, package))) <= 280


def test_submitting_the_proposed_note_still_needs_every_tick_and_keeps_the_hash(prepared_web, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    web, fake, db, package, _ = prepared_web
    _flag_the_package(db, package)
    page = web.get(_url(package))
    note = _override_value(page.text)
    review = service.read(fake, db, DID, package)
    base = {"csrf_token": web.cookies["csrf_token"], "review_sha256": review.review_sha256, "sufficiency_override": note}
    assert web.post(_url(package), data=base).status_code == 400          # no tick: refused, the note does not stand in for it
    assert repo.get_latest_valid_approval(db, package) is None
    page = web.get(_url(package))                                         # a page load rotates the token
    data = dict(base, csrf_token=web.cookies["csrf_token"])
    data.update({"claim-" + k: v for k, v in choices(review).items()})
    done = web.post(_url(package), data=data, follow_redirects=False)
    assert done.status_code == 303
    approval = repo.get_latest_valid_approval(db, package)
    assert approval and approval["approval_sha256"] == review.fingerprint      # the same hash the approval always bound
    assert approval["sufficiency_override"] == note                            # the note is kept as submitted
    receipt = json.loads(store.receipt(db, approval["id"])["confirmations_json"])
    assert len(receipt) == len(review.claims)                                  # one confirmation per statement
    assert all(method == "GET" for method, _, _ in fake.calls)                  # nothing was written to PayPal


def test_a_note_the_seller_typed_is_not_overwritten_when_the_page_comes_back_with_an_error(prepared_web, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    web, fake, db, package, _ = prepared_web
    _flag_the_package(db, package)
    web.get(_url(package))
    review = service.read(fake, db, DID, package)
    mine = "My own words about the dates."
    response = web.post(_url(package), data={"csrf_token": web.cookies["csrf_token"], "review_sha256": review.review_sha256,
                                             "sufficiency_override": mine})
    assert response.status_code == 400 and _override_value(response.text) == mine
