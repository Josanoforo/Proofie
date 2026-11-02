"""The UX states of the internal UX criteria, checked on the DOM and on the effect at the PayPal
boundary - an alert exists as an element with its action inside it, and the action's effect is the
right one (Check again only ever GETs; a double click is guarded in the page itself). Fake clients only:
nothing here can reach PayPal.
"""
from _hosted import authenticated_client, reset_csrf
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_routes_disputes as disputes_tests
import test_routes_send as send_tests
from _dom import actions_in, parse, state
from _ux_lint import breadcrumb_items
from copilot import config
from copilot.core import auto_prep
from copilot.core.disputes import PayPalError, normalize
from copilot.db import repo
from copilot.web import routes_disputes
from copilot.web.security import CSRF_COOKIE_NAME

DID = disputes_tests.DID
NOT_YET = send_tests.NOT_YET_BODY


@pytest.fixture(autouse=True)
def _fresh_last_read():
    routes_disputes._LAST_PAYPAL_READ.clear()
    yield
    routes_disputes._LAST_PAYPAL_READ.clear()


def _dispute_client(**kw):
    fake = disputes_tests.FakeClient(**kw)
    return disputes_tests._client(disputes_tests._app_with_fake(fake)), fake


def _send_client(tmp_path, monkeypatch, **kw):
    return send_tests._client(tmp_path, monkeypatch, **kw)


def _uncertain_send(tmp_path, monkeypatch, **kw):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch, post_send_get_bodies=[NOT_YET] * 3, **kw)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    return c, db_path, token, fake, pkg_id, r


# ------------------------------------------------------------------------------------- inbox states
def test_empty_inbox_is_its_own_state_with_an_action_and_no_table():
    c, _fake = _dispute_client(list_body={"items": []})
    r = c.get("/disputes")
    dom = parse(r.text)
    box = state(dom, "empty")
    assert box is not None and "No disputes need your attention right now" in box.text()
    assert ("a", "/disputes") in actions_in(box)             # its action: check PayPal again
    assert dom.find("table") is None                          # an empty list is not an empty table
    assert state(dom, "checked-at") is not None               # and it still says when PayPal was read


@pytest.mark.local_spec
def test_preparing_draft_shows_a_status_banner_and_a_reload_action():
    c, _fake = _dispute_client(list_body=disputes_tests.LIST_PAGE1)
    norm = normalize(disputes_tests.LIST_PAGE1["items"][0], __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))
    db_path = config.db_path()
    # A claim already held for this exact version: the inbox must show it as "preparing", not start another.
    assert repo.claim_auto_prep(db_path, norm["dispute_id"], auto_prep.compute_version_hash(db_path, norm))
    dom = parse(c.get("/disputes").text)
    box = state(dom, "preparing")
    assert box is not None and ("a", "/disputes") in actions_in(box)
    cell = dom.find("td", data_draft_state="preparing")
    assert cell is not None and "Preparing" in cell.text()
    assert "Nothing is approved or sent automatically" in box.text()


@pytest.mark.local_spec
def test_failed_and_needs_documents_drafts_each_carry_their_next_action():
    c, _fake = _dispute_client(list_body=disputes_tests.LIST_PAGE1)
    norm = normalize(disputes_tests.LIST_PAGE1["items"][0], __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))
    db_path = config.db_path()
    version = auto_prep.compute_version_hash(db_path, norm)
    token = repo.claim_auto_prep(db_path, norm["dispute_id"], version)
    repo.set_auto_prep_result(db_path, norm["dispute_id"], version, token, "needs_documents",
                              detail="proof of shipment")
    cell = parse(c.get("/disputes").text).find("td", data_draft_state="needs_documents")
    assert cell is not None and ("a", f"/disputes/{DID}/workspace/prepare#documents") in actions_in(cell)

    # new version -> new claim -> failed
    repo.set_auto_prep_result(db_path, norm["dispute_id"], version, token, "failed", detail="boom")
    cell = parse(c.get("/disputes").text).find("td", data_draft_state="failed")
    assert cell is not None and ("a", f"/disputes/{DID}/workspace/prepare#package") in actions_in(cell)


# ------------------------------------------------------------------- stale data / PayPal unreachable
def test_every_pay_pal_page_states_when_it_was_last_read_and_offers_a_refresh():
    c, _fake = _dispute_client(list_body=disputes_tests.LIST_PAGE1, detail_body=disputes_tests.WAITING)
    for path in ("/disputes", f"/disputes/{DID}"):
        bar = state(parse(c.get(path).text), "checked-at")
        assert bar is not None, path
        assert re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d{2} (?:am|pm) \(Mexico City time\)", bar.text()), bar.text()   # display format
        assert any(kind == "a" and target.startswith("/disputes") for kind, target in actions_in(bar))


def test_pay_pal_unreachable_says_nothing_changed_shows_the_last_read_and_offers_try_again():
    c, fake = _dispute_client(list_body=disputes_tests.LIST_PAGE1, detail_body=disputes_tests.WAITING)
    ok = c.get(f"/disputes/{DID}")                             # a successful read of THIS dispute first
    stamp = state(parse(ok.text), "checked-at").find("time").text()
    fake.detail_raises = PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-2", {})
    r = c.get(f"/disputes/{DID}")
    assert r.status_code == 502
    dom = parse(r.text)
    box = state(dom, "paypal-unreachable")
    assert box is not None and "nothing was changed" in box.text()
    # The checked-at line shows the new display time; the error page's "last read" line keeps the stored
    # label until the errors package moves it, so compare the same instant in either form.
    from copilot.web import timefmt
    read_at = state(parse(ok.text), "checked-at").find("time").attrs["datetime"]
    assert stamp in state(dom, "last-read").text() or timefmt.fmt_iso(read_at) in state(dom, "last-read").text()
    assert ("a", f"/disputes/{DID}") in actions_in(box)         # Try again (GET: safe to repeat)
    assert ("All disputes", "/disputes") in breadcrumb_items(r.text)   # The way out is the breadcrumb
    assert "SERVICE_UNAVAILABLE" not in r.text and "dbg-2" not in r.text
    assert [call[0] for call in fake.calls].count("POST") == 0


def test_a_transport_failure_is_the_same_plain_page_not_a_crash():
    class _Boom(disputes_tests.FakeClient):
        def call(self, method, path, op, **kw):
            raise httpx.ConnectError("no route")
    fake = _Boom()
    c = disputes_tests._client(disputes_tests._app_with_fake(fake))
    r = c.get("/disputes")
    assert r.status_code == 502
    dom = parse(r.text)
    assert state(dom, "paypal-unreachable") is not None
    assert "No successful PayPal read yet" in state(dom, "last-read").text()   # nothing stale to show


def test_the_last_read_is_per_dispute_so_another_disputes_read_is_never_shown_as_this_ones():
    c, fake = _dispute_client(list_body=disputes_tests.LIST_PAGE1, detail_body=disputes_tests.WAITING)
    c.get("/disputes")                                          # the inbox was read fine...
    c.get("/disputes/PP-R-OTHER-0001")                          # ...and so was ANOTHER dispute
    fake.detail_raises = PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-5", {})
    r = c.get(f"/disputes/{DID}")                               # this one never was
    assert "No successful PayPal read yet" in state(parse(r.text), "last-read").text()


def test_a_failed_post_page_never_offers_a_try_again_link_that_would_repeat_it(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    fake.detail_body = None

    def boom(method, path, op, **kw):
        raise PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-3", {})
    fake.call = boom
    r = c.post(f"/disputes/{DID}/packages", data={"csrf_token": token})   # generate: reads PayPal first
    assert r.status_code == 502
    box = state(parse(r.text), "paypal-unreachable")
    assert box is not None
    # no "Try again" for a POST; the one button is a GET back to the dispute, the breadcrumb leads out
    assert not [a for a in box.find_all("a") if a.text() == "Try again"]
    assert [a.attrs["href"] for a in box.find_all("a", class_="btn")] == [f"/disputes/{DID}"]
    assert ("All disputes", "/disputes") in breadcrumb_items(r.text)
    # a POST may have written something before PayPal went away - never claim "nothing was changed"
    assert "nothing was changed" not in box.text().lower()
    assert "could not confirm what happened" in box.text()
    # the assertion above must be able to FAIL - inject the very thing it forbids and it must see it
    at = r.text.index("</div>", r.text.index('data-state="paypal-unreachable"'))        # the box's own closing tag (the shell has divs above it)
    injected = parse(r.text[:at] + '<a class="btn" href="/retry">Try again</a>' + r.text[at:])
    assert [a for a in state(injected, "paypal-unreachable").find_all("a") if a.text() == "Try again"]


# --------------------------------------------------------------------------------- detail page states
def test_unknown_dispute_is_a_not_found_state_with_a_way_out():
    c, _fake = _dispute_client(detail_raises=PayPalError(404, "RESOURCE_NOT_FOUND", "dbg-1", {}))
    r = c.get("/disputes/PP-R-DOES-NOT-EXIST")
    box = state(parse(r.text), "not-found")
    assert r.status_code == 404 and box is not None and ("All disputes", "/disputes") in breadcrumb_items(r.text)


def test_no_action_available_hides_the_generate_buttons_and_explains():
    c, _fake = _dispute_client(detail_body=send_tests.NO_LINK)
    dom = parse(c.get(f"/disputes/{DID}").text)
    box = state(dom, "no-action-available")
    assert box is not None and "not asking for evidence" in box.text()
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/packages")
                or "/packages?manual" in f.attrs.get("action", "")]


def test_detail_summary_names_missing_unreadable_and_unchecked_things_each_with_an_action():
    c, _fake = _dispute_client(detail_body=disputes_tests.WAITING)
    dom = parse(c.get(f"/disputes/{DID}").text)
    missing = state(dom, "missing")
    assert missing is not None and ("a", f"/disputes/{DID}/workspace/prepare#documents") in actions_in(missing) \
        and ("a", f"/disputes/{DID}/workspace/prepare#package") in actions_in(missing)
    assert "Not checked yet" in state(dom, "mismatch-unchecked").text()   # never implies "no mismatches"
    assert "has not confirmed" in state(dom, "paypal-confirmed-none").text()

    repo.insert_document(config.db_path(), DID, "scan.pdf", "application/pdf", 10, "sha1", None, False)
    prepare = parse(c.get(f"/disputes/{DID}/workspace/prepare").text)
    dom = parse(c.get(f"/disputes/{DID}").text)
    unreadable = state(prepare, "unverifiable")
    assert unreadable is not None and "scan.pdf" in unreadable.text()
    assert ("a", f"/disputes/{DID}/workspace/prepare#documents") in actions_in(unreadable)
    assert state(dom, "missing").find("li") is not None
    assert "No documents uploaded yet" not in state(dom, "missing").text()
    # the document row itself says extraction failed, with the manual-entry way forward
    assert "No. Type the details yourself in the package." in prepare.text()


def test_raw_technical_detail_is_not_shown_on_the_detail_page():
    # the folded raw dict dump was removed from the judge-facing page; the readable details stay.
    c, _fake = _dispute_client(detail_body=disputes_tests.WAITING)
    dom = parse(c.get(f"/disputes/{DID}").text)
    assert dom.find("pre") is None


# ------------------------------------------------------------------ package review states
def test_a_missing_package_is_a_state_with_a_way_back(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    dom = parse(c.get(f"/disputes/{DID}/packages/9999").text)
    box = state(dom, "package-not-found")
    assert box is not None and ("a", f"/disputes/{DID}") in actions_in(box)


def test_review_page_groups_what_is_known_missing_and_unverifiable_and_says_what_will_be_sent(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    section = state(dom, "where-it-stands")
    assert section is not None
    assert section.find("li", data_check_group="Missing") is not None
    will_send = state(dom, "will-send")
    assert "0 statements" in will_send.text()
    assert state(dom, "no-tracking") is not None and ("a", "#field-tracking_number") in actions_in(will_send)   # one target for this link text
    assert "Attachments: none" in will_send.text()
    assert state(dom, "checked-at") is not None                 # the review page dates its PayPal read too
    # every check row carries its own state in the DOM, with the text next to it
    rows = dom.find_all("tr")
    assert any(row.attrs.get("data-check-state") in ("missing", "present_unverifiable", "verified")
               for row in rows)


def test_extraction_failure_on_the_review_page_offers_manual_entry(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "scan.pdf", "application/pdf", 10, "sha1", None, False)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    box = state(parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text), "extraction-failed")
    assert box is not None and "scan.pdf" in box.text() and ("a", "#shipment-details") in actions_in(box)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_technical_details_stay_folded_on_the_review_page(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    pres = [p for p in dom.find_all("pre") if "validation_ok" in p.text()]
    assert pres and all(p.inside("details") for p in pres)


# ---------------------------------------------------------------- approval and sending states
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_send_control_is_guarded_against_a_double_click_in_the_page_itself(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    dom = parse(page.text)
    send_form = next(f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send"))
    assert "data-sends" in send_form.attrs                                  # The shell guard covers every POST form
    button = send_form.find("button")
    assert button.attrs.get("data-sending-text") == "Sending to PayPal…" and "Send evidence to PayPal" in button.text()
    script = dom.find("script", src="/static/app.js")
    assert script is not None
    js = c.get("/static/app.js")                                 # the guard really ships with the app
    assert js.status_code == 200 and "data-guard-submit" in js.text and "aria-disabled" in js.text and "disabled = true" not in js.text   # Aria-disabled, never disabled
    assert fake.provide_evidence_calls == []                      # looking at the page sent nothing


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_editing_after_approval_shows_that_approval_was_cleared_and_hides_the_send_form(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    fresh = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    token2 = fresh.cookies.get(CSRF_COOKIE_NAME)
    added = c.post(f"/disputes/{DID}/packages/{pkg_id}/claims",
                   data={"csrf_token": token2, "text": "An extra statement."}, follow_redirects=False)
    assert added.status_code == 303
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    box = state(dom, "approval-cleared")
    assert box is not None and "changed" in box.text()
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]
    assert any(f.attrs.get("action", "").endswith("/approve") for f in dom.find_all("form"))


def test_a_new_package_that_was_never_approved_does_not_claim_its_approval_was_cleared(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    assert state(parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text), "approval-cleared") is None


# ----------------------------------------------------------------------- send results
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_uncertain_result_is_amber_offers_check_again_and_that_only_ever_gets(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id, r = _uncertain_send(tmp_path, monkeypatch)
    assert r.status_code == 202
    dom = parse(r.text)
    box = state(dom, "result-uncertain")
    assert box is not None and "banner-warn" in box.attrs["class"]       # not red, not green
    check = next(f for f in box.find_all("form") if f.attrs["action"].endswith("/check"))
    assert check.find("input", name="csrf_token").attrs["value"] == token   # same cookie token: Back still works
    assert state(dom, "rejected") is None and state(dom, "confirmed") is None
    posts_before = len(fake.provide_evidence_calls)
    again = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    assert again.status_code == 200
    assert len(fake.provide_evidence_calls) == posts_before == 1          # the effect: no second POST, ever
    still = state(parse(again.text), "result-uncertain")
    assert still is not None and any(f.attrs["action"].endswith("/check") for f in still.find_all("form"))


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_recovery_after_an_uncertain_result_turns_green_only_when_paypal_shows_it(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id, _r = _uncertain_send(tmp_path, monkeypatch)
    fake.recovery_get_body = send_tests._confirmed_body()                 # PayPal's own GET now has it
    again = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    box = state(parse(again.text), "recovered")
    assert box is not None and "banner-ok" in box.attrs["class"]
    assert len(fake.provide_evidence_calls) == 1                           # recovered by GET, never a resend
    review = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    assert state(review, "confirmed") is not None
    assert state(review, "already-received") is not None                   # nothing new to send
    assert not [f for f in review.find_all("form") if f.attrs.get("action", "").endswith("/send")]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_paypal_rejection_is_an_error_state_without_check_again(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(
        tmp_path, monkeypatch, provide_evidence_error=PayPalError(422, "DISPUTE_ALREADY_RESOLVED", "dbg-9", {}))
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    dom = parse(r.text)
    box = state(dom, "rejected")
    assert box is not None and "banner-error" in box.attrs["class"]
    assert not box.find_all("form")                                        # a known rejection: nothing to "check"
    assert ("a", f"/disputes/{DID}/packages/{pkg_id}#next-step") in actions_in(box)   # Lands on the next step
    assert dom.find("pre").inside("details")                               # raw detail is folded
    assert len(fake.provide_evidence_calls) == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_success_is_green_explains_confirmation_and_keeps_check_again_secondary(tmp_path, monkeypatch):
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch, post_send_get_bodies=[send_tests._confirmed_body()])
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    box = state(parse(r.text), "confirmed")
    assert r.status_code == 200 and box is not None and "banner-ok" in box.attrs["class"]
    assert "PayPal now shows this evidence as received" in box.text()      # how it was confirmed
    # Check again stays secondary and sits outside the success panel
    assert not box.find_all("form")
    forms = [f for f in parse(r.text).find_all("form") if f.attrs["action"].endswith("/check")]
    assert len(forms) == 1 and "btn-secondary" in forms[0].find("button").attrs["class"]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_review_banner_for_an_uncertain_result_carries_check_again(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id, _r = _uncertain_send(tmp_path, monkeypatch)
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    box = state(dom, "result-uncertain")
    assert box is not None and "not an error and not a success" in box.text()
    # Exactly one Check again per state; in the pending state it is the next-step panel's primary
    checks = [f for f in dom.find_all("form") if f.attrs["action"].endswith("/check")]
    assert len(checks) == 1 and [n for n in dom.find(id="next-step").walk() if n is checks[0]]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_editing_after_a_confirmed_send_keeps_the_earlier_confirmation_identifiable_as_earlier(tmp_path, monkeypatch):
    """The earlier confirmation is never shown as if it covered the edited content."""
    c, db_path, token, fake = _send_client(tmp_path, monkeypatch, post_send_get_bodies=[send_tests._confirmed_body()])
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    token2 = c.get(f"/disputes/{DID}/packages/{pkg_id}").cookies.get(CSRF_COOKIE_NAME)
    # The sent version cannot be edited any more (409); the earlier confirmation is read on the NEW version.
    refused = c.post(f"/disputes/{DID}/packages/{pkg_id}/claims",
                     data={"csrf_token": token2, "text": "Edited after sending."}, follow_redirects=False)
    assert refused.status_code == 409
    token3 = c.get(f"/disputes/{DID}/packages/{pkg_id}").cookies.get(CSRF_COOKIE_NAME)
    r2 = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token3}, follow_redirects=False)
    new_id = int(r2.headers["location"].rsplit("/", 1)[-1])
    token4 = c.get(f"/disputes/{DID}/packages/{new_id}").cookies.get(CSRF_COOKIE_NAME)
    c.post(f"/disputes/{DID}/packages/{new_id}/claims",
           data={"csrf_token": token4, "text": "A different statement."}, follow_redirects=False)
    dom = parse(c.get(f"/disputes/{DID}/packages/{new_id}").text)
    earlier = state(dom, "earlier-send-confirmed")
    assert earlier is not None and "That confirmation covers only the content sent then" in earlier.text()
    assert state(dom, "confirmed") is None and state(dom, "prepared") is not None
    detail = parse(c.get(f"/disputes/{DID}").text)
    assert "covers only the content sent then" in state(detail, "paypal-confirmed").text()


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
