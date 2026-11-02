"""The fixes from the review. Same method as
test_ux_states.py - DOM and effect at the PayPal boundary, fake clients only, nothing can reach PayPal.
"""
from _hosted import authenticated_client, reset_csrf
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_routes_package as package_tests
import test_routes_send as send_tests
from _dom import actions_in, parse, state
from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist import STATES as CHECK_STATES
from copilot.core.disputes import PayPalError
from copilot.db import repo
from copilot.web import routes_disputes
from copilot.web.security import CSRF_COOKIE_NAME
import pytest

DID = send_tests.DID
NOT_YET = send_tests.NOT_YET_BODY


@pytest.fixture(autouse=True)
def _fresh_last_read():
    routes_disputes._LAST_PAYPAL_READ.clear()
    yield
    routes_disputes._LAST_PAYPAL_READ.clear()


def _uncertain(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch, post_send_get_bodies=[NOT_YET] * 3)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 202
    return c, db_path, token, fake, pkg_id


def _make_paypal_get_fail(fake, op="recovery_get"):
    original = fake.call

    def call(method, path, call_op, **kw):
        if call_op == op:
            raise httpx.ConnectError("no route")
        return original(method, path, call_op, **kw)
    fake.call = call


# ---- dom class matching is by token and can see a class ----------------------------------------
def test_dom_class_matching_is_by_token_and_can_see_a_class():
    dom = parse('<div class="banner banner-warn"><a class="btn big" href="/x">go</a><a href="/y">no</a></div>')
    assert [n.attrs["href"] for n in dom.find_all("a", class_="btn")] == ["/x"]
    assert dom.find("a", class_="big") is not None
    assert dom.find("a", class_="bt") is None            # a token, not a substring
    assert dom.find("div", class_="banner-warn") is not None


# ---- check again with paypal down says it could not check never pending ------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_with_paypal_down_says_it_could_not_check_never_pending(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id = _uncertain(tmp_path, monkeypatch)
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")           # a successful read of this dispute: the "last read"
    stamp = state(parse(page.text), "checked-at").find("time").text()
    token = page.cookies.get(CSRF_COOKIE_NAME)                    # that page GET rotated the token
    _make_paypal_get_fail(fake)
    posts_before = len(fake.provide_evidence_calls)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    assert r.status_code == 502
    dom = parse(r.text)
    box = state(dom, "paypal-unreachable")
    assert box is not None and "banner-error" in box.attrs["class"]
    assert state(dom, "result-uncertain") is None and state(dom, "recovered") is None
    assert "still not known" in box.text() and "Nothing was changed" in box.text()
    # The checked-at line shows the new display time; the error page's "last read" line keeps the stored
    # label until the errors package moves it, so compare the same instant in either form.
    from copilot.web import timefmt
    read_at = state(parse(page.text), "checked-at").find("time").attrs["datetime"]
    assert stamp in state(dom, "last-read").text() or timefmt.fmt_iso(read_at) in state(dom, "last-read").text()
    retry = next(f for f in box.find_all("form") if f.attrs["action"].endswith("/check"))
    assert "Try again" in retry.find("button").text()           # Try again = the same GET-only check
    assert len(fake.provide_evidence_calls) == posts_before == 1  # effect: no POST at all


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_with_paypal_up_still_says_pending_when_it_asked_and_found_nothing(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id = _uncertain(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    assert r.status_code == 200 and state(parse(r.text), "result-uncertain") is not None


# ---- the dispute page does not say checked again just now --------------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_dispute_page_does_not_say_checked_again_just_now_when_the_recheck_failed(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id = _uncertain(tmp_path, monkeypatch)
    ok = c.get(f"/disputes/{DID}")
    pending_ok = state(parse(ok.text), "send-unconfirmed")
    assert pending_ok is not None
    assert "PayPal was read at" in pending_ok.text()               # (was "Checked again just now")
    _make_paypal_get_fail(fake)
    page = c.get(f"/disputes/{DID}")
    dom = parse(page.text)
    box = state(dom, "recheck-failed")
    assert box is not None and "PayPal did not answer at" in box.text() 
    # the panel never claims a successful re-check (the case strip's own read time is a different, real read)
    assert "PayPal was read at" not in box.text() and state(dom, "send-unconfirmed") is None
    assert ("a", f"/disputes/{DID}") in actions_in(box)


# ---- conflict review ---------------------------------------------------------------------------
def _conflict_review(tmp_path, monkeypatch):
    c, db_path, token = package_tests._c03_client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, package_tests.C03_DID, "other_order.pdf", "application/pdf", 10,
                                  "sha1", package_tests.C03_OTHER_ORDER_TEXT, True)
    pkg_id = repo.insert_package(db_path, package_tests.C03_DID, source="human_edit", validation_ok=True,
                                 review_flags=[])
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0,
                                          "text": "Shipped via Estafeta, tracking DEMO-EST-999999.",
                                          "source_ids": [f"doc_{doc_id}"], "basis": "verified"}])
    return parse(c.get(f"/disputes/{package_tests.C03_DID}/packages/{pkg_id}").text), doc_id


def test_a_conflict_row_has_a_readable_state_its_basis_and_real_actions(tmp_path, monkeypatch):
    dom, doc_id = _conflict_review(tmp_path, monkeypatch)
    rows = [r for r in dom.find_all("tr") if r.attrs.get("data-check-state") == "conflict"]
    assert rows, "the fixture must produce at least one conflict row"
    for row in rows:
        cells = row.find_all("td")
        assert "Does not match" in cells[1].text()
        assert cells[1].text().split()[0] not in CHECK_STATES          # never the raw enum token
        assert "Because:" in cells[1].find("small", data_basis="").text()
        acts = actions_in(cells[3])
        assert acts, "a conflict row must carry an action"
        assert ("form", f"/disputes/{package_tests.C03_DID}/documents/{doc_id}/exclude") in acts
        assert ("a", "#shipment-details") in acts and ("a", "#approval") in acts
        assert "review_difference" not in row.text()                     # no internal token as the action
    group = dom.find("li", data_check_group="Does not match")
    assert group is not None and any(kind == "a" and target.startswith("#check-")
                                     for kind, target in actions_in(group))


def test_a_missing_row_and_its_group_carry_actions_and_no_raw_tokens(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    rows = [r for r in dom.find_all("tr") if "data-check-state" in r.attrs]
    assert rows
    for row in rows:
        cells = row.find_all("td")
        assert cells[1].text().split()[0] not in CHECK_STATES
        assert not {"add_document", "fix_value", "check_status"} & set(cells[3].text().split())
        if row.attrs["data-check-state"] in ("missing", "present_unverifiable"):
            assert actions_in(cells[3]), row.attrs["data-check"]
    group = dom.find("li", data_check_group="Missing")
    targets = [t for _k, t in actions_in(group)]
    assert "#shipment-details" in targets and f"/disputes/{DID}/workspace/prepare#documents" in targets


# ---- manual approved ---------------------------------------------------------------------------
def _manual_approved(c, db_path, token):
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": str(pkg_id),
                                                   "tracking_number": "DEMO-MAN-1", "carrier": "FedEx"},
           follow_redirects=False)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
               data={"csrf_token": token, "sufficiency_override": "Typed by me, ready to send."},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return pkg_id


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_approval_that_no_longer_matches_the_content_is_shown_as_cleared_and_not_sendable(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = _manual_approved(c, db_path, token)
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    before = parse(page.text)
    token = page.cookies.get(CSRF_COOKIE_NAME)                    # the page GET rotated it
    assert any(f.attrs.get("action", "").endswith("/send") for f in before.find_all("form"))   # valid now
    assert state(before, "approval-cleared") is None
    # Editing shipment details invalidates the approval immediately.
    c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": str(pkg_id),
                                                   "tracking_number": "DEMO-MAN-2", "carrier": "FedEx"},
           follow_redirects=False)
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None            # invalidates on the edit itself
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    token = page.cookies.get(CSRF_COOKIE_NAME)
    dom = parse(page.text)
    box = state(dom, "approval-cleared")
    assert box is not None and "earlier version" in box.text()
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]
    assert dom.find(id="next-step") is not None                      # The panel names the next step (no statement yet)
    # ...and the server agrees with the page: sending is refused as out of date, nothing reaches PayPal.
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 400 and fake.provide_evidence_calls == []


# ---- the confirmed panel is not positive while the result is uncertain --------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_confirmed_panel_is_not_positive_while_the_result_is_uncertain_and_the_send_form_is_withheld(
        tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id = _uncertain(tmp_path, monkeypatch)
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    panel = state(dom, "where-it-stands")
    none = state(panel, "paypal-confirmed-none")
    assert none is not None and "has not confirmed" in none.text() and "Check again" in none.text()
    assert state(panel, "paypal-confirmed") is None
    assert state(dom, "send-withheld") is not None
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_after_a_known_rejection_the_send_form_is_back(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(
        tmp_path, monkeypatch, provide_evidence_error=PayPalError(422, "DISPUTE_ALREADY_RESOLVED", "dbg-9", {}))
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    assert state(dom, "rejected") is not None
    assert any(f.attrs.get("action", "").endswith("/send") for f in dom.find_all("form"))


# ---- the script restores a page that comes back from the browser cache ------------------------
def test_the_script_restores_a_page_that_comes_back_from_the_browser_cache(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    js = c.get("/static/app.js").text
    assert "pageshow" in js and "event.persisted" in js and "aria-disabled" in js and "location.reload" in js   # Unlock by aria-disabled, reload where data-sends
    assert "data-original-text" in js


# ---- a partial result is amber names the gap and offers check again ---------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_partial_result_is_amber_names_the_gap_and_offers_check_again(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(
        tmp_path, monkeypatch, post_send_get_bodies=[send_tests._confirmed_body(documents=[])])
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", send_tests.TRACKING_PDF, "application/pdf")},
               data={"csrf_token": token}, follow_redirects=False)
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token}, follow_redirects=False)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
           data={"csrf_token": token, "sufficiency_override": "ready"}, follow_redirects=False)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 206
    box = state(parse(r.text), "partial")
    # The partial outcome names the gap in the shared wording
    assert box is not None and "banner-warn" in box.attrs["class"] and "at least one attached file was not" in box.text()
    assert any(f.attrs["action"].endswith("/check") for f in box.find_all("form"))
    assert len(fake.provide_evidence_calls) == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_paypal_unreachable_before_sending_is_its_own_state_with_nothing_sent(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    _make_paypal_get_fail(fake, op="pre_send_get")
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    assert r.status_code == 503
    box = state(parse(r.text), "paypal-unreachable")
    assert box is not None and "nothing was sent" in box.text()
    assert not box.find_all("form")                      # nothing to check: it never left; Back to review is the way
    assert fake.provide_evidence_calls == []


def test_a_local_refusal_is_blocked_not_a_paypal_rejection(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})   # not approved
    dom = parse(r.text)
    box = state(dom, "blocked")
    assert r.status_code == 400 and box is not None and "banner-error" in box.attrs["class"]
    assert state(dom, "rejected") is None and "banner-error" in box.attrs["class"]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_an_abandoned_package_says_edit_it_in_its_own_state(tmp_path, monkeypatch):
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    sha = sha256_json(build_request_input(repo.list_claims(db_path, pkg_id), tn, carrier))
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, sha)
    store.write(DID, sha, {"state": "abandoned", "request_id": "req-x"})
    store.close()
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    box = state(parse(r.text), "abandoned")
    assert r.status_code == 409 and box is not None and "banner-error" in box.attrs["class"]
    review = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    assert state(review, "abandoned") is not None
    assert not [f for f in review.find_all("form") if f.attrs.get("action", "").endswith("/send")]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_confirm_not_received_ends_in_the_acknowledged_state(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone as tz
    from copilot.core import send_service
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    old_ts = (datetime.now(tz.utc) - timedelta(seconds=send_service.PENDING_RECLAIM_AFTER_SECONDS + 30)
              ).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "some-other-packages-sha")
    store.write(DID, "some-other-packages-sha", {"state": "sent_unconfirmed", "request_id": "req-x", "ts": old_ts})
    store.close()
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/confirm-not-received",
               data={"csrf_token": token, "confirmed": "on"})
    box = state(parse(r.text), "acknowledged")
    assert r.status_code == 200 and box is not None and "marked abandoned" in box.text()
    assert fake.provide_evidence_calls == []


def test_a_late_landing_confirmation_is_shown_on_the_dispute_page(tmp_path, monkeypatch):
    from copilot.core.send import sha256_json
    req_input = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                "evidence_info": {"tracking_info": [{"tracking_number": "LATE"}]},
                                "notes": "late"}]}
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch,
                                                 recovery_get_body=send_tests._confirmed_body("LATE", notes="late"))
    sha = sha256_json(req_input)
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, sha)
    store.write(DID, sha, {"state": "abandoned", "request_id": "req-late", "request_input": req_input})
    store.close()
    box = state(parse(c.get(f"/disputes/{DID}").text), "late-confirmed")
    # PayPal's own GET confirmed the content, so this is green like "recovered" (it had been info)
    assert box is not None and "banner-ok" in box.attrs["class"] and "banner-info" not in box.attrs["class"]
    assert fake.provide_evidence_calls == []


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
