"""Fulfilment flow: carrier first, tracking number from the receipt, the check page (a stored, separate
GET), honest outcomes for transport / 5xx / 429 failures, and the tracker follow-ups.

Everything runs on a fake client and a temp database; nothing reaches PayPal. Behaviour items were written red first."""
import json
import re
from urllib.parse import urlsplit

import httpx
import pytest

from _dom import parse
from _hosted import reset_csrf, set_test_csrf
from _ux_lint import assert_single_primary, back_links, primaries
from copilot import config
from copilot.core import write_guard
from copilot.core.disputes import PayPalError
from copilot.db import repo
from copilot.features.fulfilment import store
from copilot.features.fulfilment.tracker_request import body_from_form, request_sha256
from test_routes import CAP, FakeClient, TOKEN, _web

pytestmark = [pytest.mark.usefixtures("web_mode")]

FORM = {"csrf_token": TOKEN, "carrier": "FEDEX", "tracking_number": "771234567890", "status": "SHIPPED"}
UNKNOWN_HEADLINE = "Send attempted - not confirmed by PayPal"


def _preview(web, form=None, **kw):
    """POST the preview without following the redirect, then put the fixed test token back."""
    response = web.post(f"/fulfilment/{CAP}/preview", data=form if form is not None else FORM,
                        follow_redirects=False, **kw)
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    return response


def _token_of(response) -> str:
    match = re.fullmatch(rf"/fulfilment/{CAP}/check/([A-Za-z0-9_-]+)#preview", response.headers["location"])
    assert match, response.headers.get("location")
    return match.group(1)


def _get(web, path):
    response = web.get(path)
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    return response


def _prepared(web, form=None):
    """(token, sha256) of a prepared check."""
    response = _preview(web, form)
    assert response.status_code == 303, response.text[:400]
    token = _token_of(response)
    return token, store.get_check(CAP, token)["request_sha256"]


def _register(web, token, sha, **kw):
    return web.post(f"/fulfilment/{CAP}/register",
                    data={"csrf_token": TOKEN, "token": token, "request_sha256": sha}, follow_redirects=False, **kw)


# ---- form has the carrier select before the tracking input with exact labels ---
def test_form_has_the_carrier_select_before_the_tracking_input_with_exact_labels(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    html = web.get(f"/fulfilment/{CAP}").text
    dom = parse(html)
    order = [n for n in dom.walk() if n.attrs.get("id") in ("field-carrier", "field-tracking_number")]
    assert [(n.tag, n.attrs["id"]) for n in order] == [("select", "field-carrier"), ("input", "field-tracking_number")]
    labels = {n.attrs.get("for"): n.text() for n in dom.find_all("label")}
    assert labels["field-carrier"].startswith("Carrier (shipping company)")
    assert labels["field-tracking_number"].startswith("Tracking number (from the carrier's receipt)")


def test_a_carrier_name_in_the_tracking_field_is_refused_at_the_tracking_field(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    response = web.post(f"/fulfilment/{CAP}/preview", data={**FORM, "tracking_number": "Fedex"},
                        follow_redirects=False)
    assert response.status_code == 400
    dom = parse(response.text)
    assert dom.find("p", id="error-tracking_number") is not None
    assert "is a carrier name, not a tracking number" in dom.find("p", id="error-tracking_number").text()
    assert dom.find("p", id="error-carrier") is None
    assert dom.find("input", id="field-tracking_number").attrs["value"] == "Fedex"
    assert dom.find("option", value="FEDEX").attrs.get("selected") is not None     # the chosen carrier is kept
    assert not fake.posts() and store.list_files() == []


def test_spaces_in_the_tracking_number_are_removed_and_the_stored_body_carries_the_catalogue_code(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    token, sha = _prepared(web, {**FORM, "tracking_number": "7712 3456 7890"})
    stored = store.get_check(CAP, token)
    tracker = stored["body"]["trackers"][0]
    assert tracker["tracking_number"] == "771234567890"
    assert (tracker["carrier"], tracker["carrier_name_other"]) == ("FEDEX", "FedEx")
    assert sha == request_sha256(stored["body"])
    assert not fake.posts()


def test_legacy_carrier_text_is_still_accepted_and_mapped(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    token, _sha = _prepared(web, {**FORM, "carrier": "FedEx"})
    tracker = store.get_check(CAP, token)["body"]["trackers"][0]
    assert tracker["carrier"] == "FEDEX"


def test_other_carrier_goes_as_other_with_the_typed_name(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    token, _sha = _prepared(web, {**FORM, "carrier": "OTHER", "carrier_other_name": "Transportes Locales SA"})
    tracker = store.get_check(CAP, token)["body"]["trackers"][0]
    assert (tracker["carrier"], tracker["carrier_name_other"]) == ("OTHER", "Transportes Locales SA")


def test_other_without_a_name_is_refused_at_its_own_field(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    response = web.post(f"/fulfilment/{CAP}/preview", data={**FORM, "carrier": "OTHER", "carrier_other_name": ""})
    assert response.status_code == 400
    assert parse(response.text).find("p", id="error-carrier_other_name") is not None
    assert not fake.posts()


# ---- form page order primary button and hooks --------------
def test_form_page_order_primary_button_and_hooks(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    html = web.get(f"/fulfilment/{CAP}").text
    dom = parse(html)
    found = primaries(html)
    assert [n.text() for n in found] == ["Review what will be sent"]
    assert "btn-large" in found[0].attrs["class"].split()
    order = [n for n in dom.walk() if n.attrs.get("id") == "field-tracking_number" or n.tag == "details"
             and "What PayPal requires for this sale" in (n.find("summary").text() if n.find("summary") else "")]
    assert [n.attrs.get("id", n.tag) for n in order] == ["field-tracking_number", "details"]
    for state in ("transaction", "shipment-policy", "signature-requirement", "tracking-form"):
        assert dom.find(None, data_state=state) is not None, state
    assert dom.find("h1").text() == f"Tracking at the sale: {CAP}"
    assert back_links(html) == []
    assert "Fields marked Required must be completed." in html


def test_preview_redirects_to_a_check_page_that_renders_from_the_stored_body(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    response = _preview(web)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/fulfilment/{CAP}/check/") and location.endswith("#preview")
    token = _token_of(response)
    assert location == f"/fulfilment/{CAP}/check/{token}#preview"          # only the opaque token, nothing the seller typed
    page = _get(web, urlsplit(location).path)
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
    dom = parse(page.text)
    assert dom.find("section", id="preview") is not None
    assert dom.find(None, data_exact_content="") is not None
    assert "771234567890" in dom.find("section", id="preview").text()
    form = dom.find("form", data_state="confirm-form")
    assert form is not None and "data-sends" in form.attrs
    assert sorted(n.attrs["name"] for n in form.find_all("input")) == ["csrf_token", "request_sha256", "token"]
    assert form.find("input", name="token").attrs["value"] == token
    assert_single_primary(page.text)
    assert [n.text() for n in primaries(page.text)] == ["Add tracking in PayPal"]
    assert not fake.posts()


def test_register_with_an_unknown_or_foreign_token_is_refused_and_posts_nothing(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    token, sha = _prepared(web)
    other = "5S647034CC1979816"
    for target, tok in ((CAP, "nope-not-a-token"), (CAP, ""), (other, token)):
        r = web.post(f"/fulfilment/{target}/register", data={"csrf_token": TOKEN, "token": tok, "request_sha256": sha},
                     follow_redirects=False)
        assert r.status_code == 409, (target, tok)
        assert "This check has expired. Nothing was sent." in r.text
    assert not fake.posts() and store.list_files() == []


def test_register_with_a_tampered_sha_is_refused_and_posts_nothing(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    token, sha = _prepared(web)
    for bad in ("", "0" * 64, "garbage", sha[:-1] + ("0" if sha[-1] != "0" else "1")):
        r = _register(web, token, bad)
        assert r.status_code == 409 and "not what would be sent" in r.text
    assert not fake.posts() and store.list_files() == []


def test_two_registers_of_one_token_make_exactly_one_post(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    token, sha = _prepared(web)
    first = _register(web, token, sha)
    second = _register(web, token, sha)
    assert first.status_code == second.status_code == 303
    assert [c[2] for c in fake.posts()] == ["fulfilment_tracker_post"]
    row = store.get_file(CAP)
    assert second.headers["location"].startswith(f"/fulfilment/{CAP}/file/{row['id']}")
    assert store.get_file(CAP)["status"] == "registered" and len(store.list_files()) == 1


def test_an_unknown_token_on_the_check_page_is_an_expired_page_not_a_form(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    page = web.get(f"/fulfilment/{CAP}/check/never-issued")
    assert page.status_code == 404
    assert "This check has expired. Nothing was sent." in page.text
    assert 'data-state="confirm-form"' not in page.text and not fake.posts()


def test_the_check_table_is_never_read_by_the_file_listing(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    _prepared(web)
    assert store.list_files() == [] and store.get_file(CAP) is None
    assert 'data-state="no-files"' in web.get("/fulfilment").text


# ---- transport 5xx and 429 are stored as unknown and worded honestly ------------
FAILURES = [("500", PayPalError(500, "ISSUE", "dbg-1", {})), ("503", PayPalError(503, "ISSUE", "dbg-2", {})),
            ("429", PayPalError(429, "RATE_LIMIT", "dbg-3", {})), ("timeout", httpx.ReadTimeout("slow")),
            ("connect", httpx.ConnectError("down")), ("os", TimeoutError("late"))]


@pytest.mark.parametrize("name,failure", FAILURES, ids=[f[0] for f in FAILURES])
def test_transport_5xx_and_429_are_stored_as_unknown_and_worded_honestly(web_mode, monkeypatch, name, failure):
    fake = FakeClient(post_raises=failure)
    web, _ = _web(monkeypatch, fake)
    token, sha = _prepared(web)
    r = _register(web, token, sha)
    assert r.status_code == 303
    row = store.get_file(CAP)
    assert row["status"] == "unknown"
    page = _get(web, r.headers["location"])
    dom = parse(page.text)
    assert dom.find("h1").text() == UNKNOWN_HEADLINE
    assert "Proofie will not send it again on its own." in page.text
    actions = [f.attrs.get("action", "") for f in dom.find_all("form")]
    assert any(a.endswith("/check") for a in actions), actions
    assert "not registered" not in page.text and "Nothing in PayPal was changed" not in page.text
    assert "dbg-" not in page.text
    assert len(fake.posts()) == 1
    again = _register(web, token, sha)
    assert again.status_code == 303 and len(fake.posts()) == 1            # a repeat makes no new POST
    assert write_guard.lookup(CAP, "tracker", sha)["state"] == "unknown"


def test_a_definite_4xx_is_failed_with_its_reason_and_frees_the_key(web_mode, monkeypatch):
    fake = FakeClient(post_raises=PayPalError(400, "INVALID_PARAMETER_SYNTAX", "dbg-4", {}))
    web, _ = _web(monkeypatch, fake)
    token, sha = _prepared(web)
    r = _register(web, token, sha)
    assert r.status_code == 303 and store.get_file(CAP)["status"] == "failed"
    page = _get(web, r.headers["location"]).text
    assert "PayPal did not accept the tracking" in page and "Nothing was registered." in page
    assert UNKNOWN_HEADLINE not in page
    fake.post_raises = None
    again = _register(web, token, sha)
    assert again.status_code == 303 and len(fake.posts()) == 2           # the key was freed: a new approval may send
    assert store.get_file(CAP)["status"] == "registered"


def test_check_paypal_again_reads_only_and_appends_a_new_file_version(web_mode, monkeypatch):
    fake = FakeClient(post_raises=PayPalError(503, "ISSUE", "dbg-2", {}))
    web, _ = _web(monkeypatch, fake)
    token, sha = _prepared(web)
    first = _register(web, token, sha)
    old = store.get_file(CAP)
    # PayPal did save it after all: the read lists the number
    fake.trackers.append({"transaction_id": CAP, "tracking_number": "771234567890", "status": "SHIPPED",
                          "carrier": "FEDEX", "shipment_date": "2026-10-06", "last_updated_time": 1,
                          "links": [{"rel": "self"}]})
    fake.post_raises = None
    calls_before = len(fake.calls)
    r = web.post(f"/fulfilment/{CAP}/check", data={"csrf_token": TOKEN, "file_id": str(old["id"])},
                 follow_redirects=False)
    assert r.status_code == 303
    new = store.get_file(CAP)
    assert new["id"] != old["id"] and new["status"] == "registered"
    assert store.get_file_by_id(CAP, old["id"])["status"] == "unknown"       # the hashed row is never updated
    assert [c[0] for c in fake.calls[calls_before:]] == ["GET"] and not len(fake.posts()) > 1
    assert r.headers["location"] == f"/fulfilment/{CAP}/file/{new['id']}"
    actions = [a["action"] for a in repo.list_audit(config.db_path(), CAP)]
    assert "fulfilment_checked" in actions
    assert "Tracking registered in PayPal" in _get(web, r.headers["location"]).text


def test_check_paypal_again_when_paypal_does_not_list_it_says_so_and_stays_unknown(web_mode, monkeypatch):
    fake = FakeClient(post_raises=httpx.ConnectError("down"))
    web, _ = _web(monkeypatch, fake)
    token, sha = _prepared(web)
    _register(web, token, sha)
    old = store.get_file(CAP)
    r = web.post(f"/fulfilment/{CAP}/check", data={"csrf_token": TOKEN, "file_id": str(old["id"])},
                 follow_redirects=False)
    assert r.status_code == 303
    assert store.get_file(CAP)["status"] == "unknown"
    page = _get(web, r.headers["location"]).text
    # integration (e): a fresh send stays blocked until the release rule holds (test_ux_rc41_fulfilment_release.py)
    assert "PayPal answered and does not list tracking 771234567890 for this sale yet." in page
    assert "Prepare the tracking again" not in page
    assert len(fake.posts()) == 1


# ------------------------------------------------------------------------------------------------ R follow-ups
def _seed(status, body=None, limitation=None, capture=CAP):
    body = body or body_from_form(capture, FORM["tracking_number"], "FEDEX", "SHIPPED")
    sha = request_sha256(body)
    readback = {"tracking_number": FORM["tracking_number"], "status": "SHIPPED", "carrier": "FEDEX"} \
        if status == "registered" else None
    checks = {"checks": [], "not_evaluable": [], "address_comparison": {"result": "not_declared", "basis": "x"},
              "limitations": [], "rules": {}, "protection_eligibility": None}
    return store.save_file(capture, body, sha, sha, readback, checks, status, limitation), sha


@pytest.mark.parametrize("status", ["registered", "registered_unconfirmed", "unknown"])
def test_a_stored_file_blocks_a_register_even_without_a_reservation_row(web_mode, monkeypatch, status):
    web, fake = _web(monkeypatch)
    saved, sha = _seed(status)
    assert write_guard.lookup(CAP, "tracker", sha) is None
    token, tsha = _prepared(web)
    assert tsha == sha
    r = _register(web, token, sha)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/fulfilment/{CAP}/file/{saved['id']}")
    assert not fake.posts()


def test_a_legacy_failed_row_with_an_unknown_outcome_blocks_and_renders_as_unknown(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    saved, sha = _seed("failed", limitation="PayPal did not answer in time when trying to register the tracking; "
                                            "what happened is unknown.")
    token, _ = _prepared(web)
    r = _register(web, token, sha)
    assert r.status_code == 303 and not fake.posts()
    page = _get(web, f"/fulfilment/{CAP}/file/{saved['id']}").text
    assert parse(page).find("h1").text() == UNKNOWN_HEADLINE
    assert "Nothing in PayPal was changed" not in page


def test_a_bare_already_parameter_renders_no_notice(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    for suffix in ("?already=1", "?already=", "?already=" + "0" * 64):
        page = web.get(f"/fulfilment/{CAP}{suffix}").text
        assert 'data-state="already-registered"' not in page and "Already sent" not in page
    saved, _sha = _seed("registered")
    page = web.get(f"/fulfilment/{CAP}/file/{saved['id']}").text
    assert 'data-state="already-registered"' not in page


def test_the_already_notice_comes_from_the_reservation_not_from_the_url(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    body = body_from_form(CAP, FORM["tracking_number"], "FEDEX", "SHIPPED")
    sha = request_sha256(body)
    assert write_guard.reserve(CAP, "tracker", sha).reserved               # a send of this body is in flight
    page = web.get(f"/fulfilment/{CAP}?already={sha}").text
    assert 'data-state="already-registered"' in page and "Nothing was sent again" in page


def test_a_blocked_repeat_does_not_redirect_to_an_older_refused_file(web_mode, monkeypatch):
    fake = FakeClient(post_raises=PayPalError(422, "ISSUE", "dbg-9", {}))
    web, _ = _web(monkeypatch, fake)
    token, sha = _prepared(web)
    _register(web, token, sha)                                           # definite refusal: file 1, key freed
    refused = store.get_file(CAP)
    assert refused["status"] == "failed" and write_guard.lookup(CAP, "tracker", sha)["state"] == "refused"
    assert write_guard.reserve(CAP, "tracker", sha).reserved               # a later send is in flight
    again = _register(web, token, sha)
    assert again.status_code == 303
    assert f"/file/{refused['id']}" not in again.headers["location"]
    assert again.headers["location"].startswith(f"/fulfilment/{CAP}?already=")
    assert 'data-state="already-registered"' in web.get(again.headers["location"]).text


def test_a_saved_only_file_never_says_it_was_sent_to_paypal(web_mode, monkeypatch):
    web, fake = _web(monkeypatch, register=None)
    token, sha = _prepared(web)
    r = _register(web, token, sha)
    saved = store.get_file(CAP)
    assert saved["status"] == "saved_without_registration" and not fake.posts()
    page = _get(web, f"/fulfilment/{CAP}/file/{saved['id']}?already=1").text
    assert "already sent to PayPal" not in page and "already registered" not in page


def test_the_check_page_of_a_body_already_registered_shows_the_notice_not_the_form(web_mode, monkeypatch):
    web, fake = _web(monkeypatch, FakeClient(list_after_post=False))      # accepted, not listed: preview is not refused
    token, sha = _prepared(web)
    assert _register(web, token, sha).status_code == 303
    token2, sha2 = _prepared(web)
    assert sha2 == sha and token2 != token
    page = _get(web, f"/fulfilment/{CAP}/check/{token2}").text
    dom = parse(page)
    assert dom.find("form", data_state="confirm-form") is None
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/register")]
    assert dom.find(None, data_state="already-registered") is not None
    assert len(fake.posts()) == 1
