"""The review fixes and the three copy fixes, checked on the DOM and on the effect at the
PayPal boundary. Fake clients only; nothing here can reach PayPal. (CSS-level rules - semantics of colour, excluded
rows, secondary buttons - live in test_design_contrast.py; the browser-level checks in demo/ui_check.py.)
"""
from _hosted import authenticated_client, reset_csrf
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_routes_disputes as disputes_tests
import test_routes_package as package_tests
import test_routes_send as send_tests
import test_ux_states_fixes as fixes
from _dom import actions_in, parse, state
from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.disputes import PayPalError
from copilot.db import repo
from copilot.web import routes_disputes, timefmt
from copilot.web.security import CSRF_COOKIE_NAME
import pytest

DID = send_tests.DID
NOT_YET = send_tests.NOT_YET_BODY
# The checked-at line uses timefmt.display ("16 Oct 2026, 10:10 pm (Mexico City time)"); the due dates and
# the other stamps move to it in the flow packages, and keep the stored fmt() form until then.
DISPLAY_TIME = re.compile(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d{2} (?:am|pm) \(Mexico City time\)")
FIXED_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [A-Z]{2,4} \(UTC[+-]\d+(?::\d{2})?\)")
FOOTER = "Independent project. Not affiliated with or endorsed by PayPal."


@pytest.fixture(autouse=True)
def _fresh_last_read():
    routes_disputes._LAST_PAYPAL_READ.clear()
    yield
    routes_disputes._LAST_PAYPAL_READ.clear()


def _fresh_db(path: Path) -> None:
    """A clean database and uploads folder for one scenario (the autouse conftest fixture restores the defaults)."""
    path.mkdir(parents=True, exist_ok=True)
    config.set_db_path_override(path / "scenario.db")
    config.set_uploads_dir_override(path / "uploads")


def _check(c, pkg_id, token):
    return c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})


# ---- a recovery get with no match says still not confirmed ----------------------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_recovery_get_with_no_match_says_still_not_confirmed_and_the_get_really_ran(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id = fixes._uncertain(tmp_path, monkeypatch)
    gets_before = fake.calls_log.count("recovery_get")
    r = _check(c, pkg_id, token)
    dom = parse(r.text)
    assert dom.find("h1").text() == "Still not confirmed by PayPal"
    box = state(dom, "result-uncertain")
    assert box is not None and "banner-warn" in box.attrs["class"]
    assert fake.calls_log.count("recovery_get") > gets_before                    # the effect: PayPal WAS asked (GET)
    assert len(fake.provide_evidence_calls) == 1                                  # and nothing was posted again


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_confirming_get_says_received_by_paypal_and_is_recovered(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id = fixes._uncertain(tmp_path, monkeypatch)
    fake.recovery_get_body = send_tests._confirmed_body()
    r = _check(c, pkg_id, token)
    dom = parse(r.text)
    assert dom.find("h1").text() == "Received by PayPal"
    box = state(dom, "recovered")
    assert box is not None and "banner-ok" in box.attrs["class"]
    assert len(fake.provide_evidence_calls) == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_cannot_check_page_has_no_check_again_form(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    sha = sha256_json(build_request_input(repo.list_claims(db_path, pkg_id), tn, carrier))
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, sha)
    store.write(DID, sha, {"state": "sent_unconfirmed", "request_id": "req-old"})     # no stored request
    store.close()
    dom = parse(_check(c, pkg_id, token).text)
    assert state(dom, "cannot-check") is not None
    assert not dom.find_all("form")                                                   # nothing to "check again"
    assert dom.find("h1").text() == "Nothing to compare with PayPal"                  # and no "just checked" wording


# ---- another packages pending attempt does not make this page claim a paypal ----------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_another_packages_pending_attempt_does_not_make_this_page_claim_a_paypal_check(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    other_req = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                "evidence_info": {"tracking_info": [{"tracking_number": "OTHER"}]}, "notes": "o"}]}
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "some-other-packages-sha")
    store.write(DID, "some-other-packages-sha", {"state": "sent_unconfirmed", "request_id": "req-o",
                                                 "request_input": other_req})
    store.close()
    dom = parse(_check(c, pkg_id, token).text)
    assert state(dom, "nothing-pending") is not None
    assert dom.find("h1").text() == "Nothing pending to check with PayPal"
    assert "Still not confirmed" not in dom.text() and "Received by PayPal" not in dom.text()


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_an_attempt_that_is_still_in_flight_has_its_own_wording(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    sha = sha256_json(build_request_input(repo.list_claims(db_path, pkg_id), tn, carrier))
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, sha)                                                              # state: pending (POST in flight)
    store.close()
    gets_before = list(fake.calls_log)
    dom = parse(_check(c, pkg_id, token).text)
    box = state(dom, "send-in-progress")
    assert box is not None and "A send is in progress; wait and check again" in box.text()
    assert "no stored request" not in dom.text().lower() and "Still not confirmed" not in dom.text()
    assert any(f.attrs["action"].endswith("/check") for f in box.find_all("form"))     # waiting: it can check again later
    assert fake.calls_log == gets_before or "provide_evidence" not in fake.calls_log[len(gets_before):]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_on_content_already_confirmed_does_not_claim_a_fresh_check(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch, post_send_get_bodies=[send_tests._confirmed_body()])
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    token2 = c.get(f"/disputes/{DID}/packages/{pkg_id}").cookies.get(CSRF_COOKIE_NAME)
    gets_before = len(fake.calls_log)
    dom = parse(_check(c, pkg_id, token2).text)
    assert dom.find("h1").text() == "Already recorded as received"
    assert state(dom, "confirmed") is not None and state(dom, "recovered") is None
    assert len(fake.calls_log) == gets_before                                          # this click asked PayPal nothing


# ---- the pending banner says checked again just now when a get really -----------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_pending_banner_says_checked_again_just_now_when_a_get_really_ran(tmp_path, monkeypatch):
    c, db_path, token, fake, pkg_id = fixes._uncertain(tmp_path, monkeypatch)
    gets_before = fake.calls_log.count("recovery_get")
    page = c.get(f"/disputes/{DID}")
    assert state(parse(page.text), "send-unconfirmed") is not None and "PayPal was read at" in state(parse(page.text), "send-unconfirmed").text()
    assert fake.calls_log.count("recovery_get") > gets_before                          # the effect: PayPal was asked


def test_the_pending_banner_does_not_say_checked_when_no_get_could_run(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    store = SQLiteAttemptStore(db_path)                                                # pending, but nothing to compare
    store.claim(DID, "an-attempt-without-a-request")
    store.write(DID, "an-attempt-without-a-request", {"state": "sent_unconfirmed", "request_id": "req-z"})
    store.close()
    page = c.get(f"/disputes/{DID}")
    dom = parse(page.text)
    box = state(dom, "recheck-not-possible")
    assert box is not None and "Proofie cannot tell whether this attempt reached PayPal" in box.text()
    assert "Checked again just now" not in page.text and state(dom, "send-unconfirmed") is None
    assert "recovery_get" not in fake.calls_log                                        # the effect: PayPal was not asked


# ----------------------------------------------------------------------------------------- copy: times (a)
def test_the_time_formatter_uses_one_fixed_english_format_with_an_explicit_offset():
    moment = datetime(2026, 10, 5, 21, 45, tzinfo=timezone.utc)
    assert timefmt.fmt(moment) == "2026-10-05 15:45 CST (UTC-6)"
    assert timefmt.fmt_iso("2026-10-05T15:18:03-0600") == "2026-10-05 15:18 CST (UTC-6)"
    assert timefmt.localize_text("19d 4h remaining - due 2026-10-24 20:13 CST (PayPal)") == \
        "19d 4h remaining - due 2026-10-24 20:13 CST (UTC-6) (PayPal)"
    assert timefmt.localize_text("Received by PayPal on 2026-10-05 15:18 CDMX") == \
        "Received by PayPal on 2026-10-05 15:18 CST (UTC-6)"
    once = timefmt.localize_text("due 2026-10-24 20:13 CST")
    assert timefmt.localize_text(once) == once                                          # idempotent
    assert FIXED_TIME.fullmatch(timefmt.now_label())


def test_the_checked_line_and_the_due_date_agree_on_one_format_on_every_page(tmp_path, monkeypatch):
    c = disputes_tests._client(disputes_tests._app_with_fake(
        disputes_tests.FakeClient(list_body=disputes_tests.LIST_PAGE1, detail_body=disputes_tests.WAITING)))
    inbox = parse(c.get("/disputes").text)
    detail = parse(c.get(f"/disputes/{DID}").text)
    for dom in (inbox, detail):
        assert DISPLAY_TIME.fullmatch(state(dom, "checked-at").find("time").text())
        assert "Hora" not in dom.text() and "estándar" not in dom.text()          # never the OS zone name
    due_cell = inbox.find("td", data_label="Respond by")                               # The inbox uses the display format
    detail_due = detail.find("dl", class_="facts").text()                               # the case strip
    assert "(Mexico City time)" in due_cell.text() and due_cell.find("time").attrs["datetime"].endswith("-06:00")
    assert "Mexico City time" in detail_due and "Respond by" in detail_due
    assert state(inbox, "checked-at").find("time").attrs["datetime"].endswith("-06:00")   # the same zone as the due date


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_times_on_the_review_page_use_the_same_format(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch, post_send_get_bodies=[send_tests._confirmed_body()])
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    dom = parse(r.text)
    assert DISPLAY_TIME.search(state(dom, "confirmed").text())                         # "Received by PayPal on <time>" (display format)
    assert DISPLAY_TIME.search(state(dom, "approved").text())                          # "Approved <time>"
    assert "CDMX" not in r.text and "T15:" not in state(dom, "approved").text()        # no raw ISO stamp, no old label


# --------------------------------------------------------------------------------- copy: (b) and (c)
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_blocked_help_summary_reads_why_is_sending_blocked(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    summaries = [n.text() for n in parse(page.text).find_all("summary")]
    assert "Sending is paused: an earlier send is not confirmed yet" not in summaries  # no pending row, no invented blocker
    pending=tmp_path/"pending";pending.mkdir()
    other,_,other_token,_,pending_id=fixes._uncertain(pending,monkeypatch)
    own_page=other.get(f"/disputes/{DID}/packages/{pending_id}")                    # its own pending attempt: Check again, no block
    assert "Sending is paused: an earlier send is not confirmed yet" not in [n.text() for n in parse(own_page.text).find_all("summary")]
    r=other.post(f"/disputes/{DID}/packages?manual=1",data={"csrf_token":own_page.cookies.get(CSRF_COOKIE_NAME)},follow_redirects=False)
    new_id=int(r.headers["location"].rsplit("/",1)[-1])
    pending_page=other.get(f"/disputes/{DID}/packages/{new_id}")                      # another version: the block
    assert "Sending is paused: an earlier send is not confirmed yet" in [n.text() for n in parse(pending_page.text).find_all("summary")]
    assert parse(pending_page.text).find("details",id="why-blocked") is not None
    assert "Sending this blocked because" not in page.text and "Sending this blocked because" not in pending_page.text


def test_the_branch_pickup_check_is_worded_for_the_seller_everywhere_it_appears(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    row = next(tr for tr in dom.find_all("tr") if tr.attrs.get("data-check") == "branch_pickup")
    assert row.find("td", data_label="Check").text() == "Delivered to a carrier branch for pickup"
    group_text = " ".join(li.text() for li in dom.find_all("li") if "data-check-group" in li.attrs)
    assert "Delivered to a carrier branch for pickup" in group_text
    assert "(ocurre)" not in row.find("td", data_label="Check").text() and "(ocurre)" not in group_text


# ---------------------------------------------------------------------------------------------- footer
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_every_main_page_carries_the_independence_note_in_the_shared_footer(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch, post_send_get_bodies=[NOT_YET] * 3)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    review = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    # a page GET rotates the CSRF token, so the POST goes right after the GET whose token it uses
    send_result = c.post(f"/disputes/{DID}/packages/{pkg_id}/send",
                         data={"csrf_token": review.cookies.get(CSRF_COOKIE_NAME)})
    pages = {
        "inbox": disputes_tests._client(disputes_tests._app_with_fake(
            disputes_tests.FakeClient(list_body=disputes_tests.LIST_PAGE1))).get("/disputes"),
        "empty inbox": disputes_tests._client(disputes_tests._app_with_fake(
            disputes_tests.FakeClient(list_body={"items": []}))).get("/disputes"),
        "dispute": c.get(f"/disputes/{DID}"),
        "review": review,
        "package not found": c.get(f"/disputes/{DID}/packages/9999"),
        "send result": send_result,
        "not found": disputes_tests._client(disputes_tests._app_with_fake(disputes_tests.FakeClient(
            detail_raises=PayPalError(404, "RESOURCE_NOT_FOUND", "d", {})))).get("/disputes/PP-R-NOPE-0001"),
        "unreachable": disputes_tests._client(disputes_tests._app_with_fake(disputes_tests.FakeClient(
            detail_raises=PayPalError(503, "X", "d", {})))).get(f"/disputes/{DID}"),
    }
    for name, response in pages.items():
        footer = state(parse(response.text), "footer")
        expected_footer = FOOTER + (" " + config.HOSTED_DEMO_SENTENCE if config.hosted_demo() else "")
        assert footer is not None and footer.tag == "footer" and footer.text() == expected_footer, name


# --------------------------------------------------------------------- colour semantics, on rendered pages
def _banners(html: str) -> list:
    return [(n.attrs["data-state"], n.attrs["class"].split()) for n in parse(html).find_all("div")
            if "banner" in n.attrs.get("class", "").split() and "data-state" in n.attrs]


def _scenario_pages(tmp_path, monkeypatch) -> dict:
    pages = {}
    # sending scenarios (send_tests' fake client)
    for name, kwargs, do_send in (
            ("uncertain", {"post_send_get_bodies": [NOT_YET] * 3}, True),
            ("confirmed", {"post_send_get_bodies": [send_tests._confirmed_body()]}, True),
            ("rejected", {"provide_evidence_error": PayPalError(422, "DISPUTE_ALREADY_RESOLVED", "d", {})}, True)):
        _fresh_db(tmp_path / name)
        c, db_path, token, fake = send_tests._client(tmp_path / name, monkeypatch, **kwargs)
        pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
        r = c.post(f"/disputes/{DID}/packages/{pkg_id}/send", data={"csrf_token": token})
        pages[f"{name} result"] = r.text
        review = c.get(f"/disputes/{DID}/packages/{pkg_id}")
        pages[f"{name} review"] = review.text
        detail = c.get(f"/disputes/{DID}")
        pages[f"{name} detail"] = detail.text
        if name == "uncertain":
            fake.recovery_get_body = send_tests._confirmed_body()
            pages["recovered"] = _check(c, pkg_id, detail.cookies.get(CSRF_COOKIE_NAME)).text
    _fresh_db(tmp_path / "blocked")
    c, db_path, token, fake = send_tests._client(tmp_path / "blocked", monkeypatch)
    pkg = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    pages["blocked"] = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}).text
    pages["package not found"] = c.get(f"/disputes/{DID}/packages/9999").text
    # A genuinely abandoned request must render red, not merely be in an allowed-state set.
    _fresh_db(tmp_path / "abandoned")
    abandoned_client, abandoned_db, abandoned_token, abandoned_fake = send_tests._client(tmp_path / "abandoned", monkeypatch)
    abandoned_package = send_tests._approved_package_with_tracking(abandoned_client, abandoned_db, abandoned_token)
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    from copilot.core.checklist_input import extracted_docs_for
    tracking, carrier = pick_tracking(extracted_docs_for(abandoned_db, DID))
    abandoned_sha = sha256_json(build_request_input(repo.list_claims(abandoned_db, abandoned_package), tracking, carrier))
    abandoned_store = SQLiteAttemptStore(abandoned_db)
    abandoned_store.claim(DID, abandoned_sha)
    abandoned_store.write(DID, abandoned_sha, {"state": "abandoned", "request_id": "req-synthetic"})
    abandoned_response = abandoned_client.post(f"/disputes/{DID}/packages/{abandoned_package}/send", data={"csrf_token": abandoned_token})
    assert abandoned_response.status_code == 409
    assert abandoned_fake.provide_evidence_calls == []
    assert abandoned_store.read(DID, abandoned_sha)["state"] == "abandoned"
    abandoned_store.close()
    pages["abandoned result"] = abandoned_response.text
    pages["not found"] = disputes_tests._client(disputes_tests._app_with_fake(disputes_tests.FakeClient(
        detail_raises=PayPalError(404, "RESOURCE_NOT_FOUND", "d", {})))).get("/disputes/PP-R-NOPE-0001").text
    pages["unreachable"] = disputes_tests._client(disputes_tests._app_with_fake(disputes_tests.FakeClient(
        detail_raises=PayPalError(503, "X", "d", {})))).get(f"/disputes/{DID}").text
    late_req = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                               "evidence_info": {"tracking_info": [{"tracking_number": "LATE"}]}, "notes": "late"}]}
    _fresh_db(tmp_path / "late")
    c, db_path, token, fake = send_tests._client(tmp_path / "late", monkeypatch,
                                                 recovery_get_body=send_tests._confirmed_body("LATE", notes="late"))
    from copilot.core.send import sha256_json
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, sha256_json(late_req))
    store.write(DID, sha256_json(late_req), {"state": "abandoned", "request_id": "r", "request_input": late_req})
    store.close()
    pages["late confirmed detail"] = c.get(f"/disputes/{DID}").text
    return pages


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_on_rendered_pages_green_means_received_or_recovered_and_red_means_refused_or_unreachable(tmp_path, monkeypatch):
    seen = {"ok": set(), "error": set()}
    for name, html in _scenario_pages(tmp_path, monkeypatch).items():
        for data_state, classes in _banners(html):
            if "banner-ok" in classes:
                assert data_state in ("confirmed", "recovered", "late-confirmed"), f"{name}: green on {data_state}"
                seen["ok"].add(data_state)
            if "banner-error" in classes:
                assert data_state in ("rejected", "paypal-unreachable", "blocked", "not-found", "package-not-found", "abandoned"), f"{name}: red on {data_state}"
                seen["error"].add(data_state)
    assert seen["ok"] == {"confirmed", "recovered", "late-confirmed"}      # PayPal's own GET confirmed the content
    assert seen["error"] == {"rejected", "paypal-unreachable", "blocked", "not-found", "package-not-found", "abandoned"}


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_not_found_and_local_refusals_are_neutral_and_conflicts_are_amber_rows(tmp_path, monkeypatch):
    pages = _scenario_pages(tmp_path, monkeypatch)
    for name, wanted in (("not found", "not-found"), ("package not found", "package-not-found"),
                         ("blocked", "blocked")):
        found = [classes for data_state, classes in _banners(pages[name]) if data_state == wanted]
        assert found and "banner-error" in found[0], name
    dom, _doc_id = fixes._conflict_review(tmp_path / "conflict", monkeypatch)
    conflict_rows = [tr for tr in dom.find_all("tr") if tr.attrs.get("data-check-state") == "conflict"]
    assert conflict_rows
    assert not [b for b in _banners_from_dom(dom) if "banner-error" in b[1] or "banner-ok" in b[1]]


def _banners_from_dom(dom) -> list:
    return [(n.attrs["data-state"], n.attrs["class"].split()) for n in dom.find_all("div")
            if "banner" in n.attrs.get("class", "").split() and "data-state" in n.attrs]


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_dr1b_confirm_not_received_has_own_required_mark_and_convention(tmp_path, monkeypatch):
    web, db, token, fake, package = fixes._uncertain(tmp_path, monkeypatch)
    posts = list(fake.provide_evidence_calls)
    # The block belongs to ANOTHER version's unconfirmed send; the version with the pending attempt itself
    # offers Check again instead.
    own = web.get(f"/disputes/{DID}/packages/{package}")
    assert parse(own.text).find("form", action=f"/disputes/{DID}/packages/{package}/confirm-not-received") is None
    token = own.cookies.get(CSRF_COOKIE_NAME)
    r = web.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    other = int(r.headers["location"].rsplit("/", 1)[-1])
    page = web.get(f"/disputes/{DID}/packages/{other}")
    assert page.status_code == 200
    form = parse(page.text).find("form", action=f"/disputes/{DID}/packages/{other}/confirm-not-received")
    assert form is not None and "Mark as not received" in form.text()
    checkbox = form.find("input", name="confirmed")
    assert checkbox is not None and "required" in checkbox.attrs
    assert checkbox.inside("label") and "Required" in checkbox.parent.text()
    # "PayPal's Resolution Center" is a link now (its text adds a hidden new-tab note): the sentence is pinned in its parts
    label = checkbox.parent.text()
    assert "I checked" in label and "and the earlier send is not there." in label
    assert [x.text() for x in checkbox.parent.find_all("a")] == ["PayPal's Resolution Center (opens in a new tab)"]
    assert fake.provide_evidence_calls == posts
