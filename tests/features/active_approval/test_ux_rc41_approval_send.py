"""Behaviour of the approval page, the evidence send and its result page.

Offline only: fake PayPal clients, a temp database per test (root conftest). Behaviour items are written first and
checked against the DOM or the recorded effects (POST counts, stored rows), not against copy alone. The follow-ups
come from a later review.
"""
import copy
import hashlib
import re
import sqlite3

import pytest
from _dom import parse
from _hosted import authenticated_client, set_test_csrf
import _ux_lint as lint

from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.raw_storage import write_raw
from copilot.core.send import sha256_json
from copilot.core.send_recovery import RECEIVED_MESSAGE
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service, store
from copilot.web.routes_disputes import get_client

from .test_guarantees import DID, SendFake, decisions, ready
from .test_send_once_and_honest_status import OWN_NUMBER, PayPalAfterSend, _tracker

ALREADY = ("PayPal previously confirmed this exact package as received. Nothing new was sent. "
           "Proofie does not send the same package twice.")
USED_UP = "This version was sent to PayPal, so it cannot be approved again."


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr("copilot.core.send_recovery.time.sleep", lambda seconds: None)


@pytest.fixture
def mode_prepared(web_mode, prepared):
    return prepared


def _approved(prepared):
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    approval_id = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    return fake, db, package, review, approval_id


def _web(prepared, sender=None, token="UX-CSRF"):
    web, fake, db, package, _ = prepared
    if sender is not None:
        web.app.dependency_overrides[get_client] = lambda: sender
    web = authenticated_client(web.app)
    set_test_csrf(web, token)
    return web, token


def _attempt_state(db, review):
    attempts = SQLiteAttemptStore(db)
    try:
        row = attempts.read(DID, sha256_json(review.request_input))
    finally:
        attempts.close()
    return row["state"] if row else None


def _attach_file(db, package, name="extra-proof.pdf"):
    from test_routes_send import _minimal_pdf_with_lines
    text = "DEMO DATA Synthetic attachment " + name
    raw = _minimal_pdf_with_lines([text])
    sha = hashlib.sha256(raw).hexdigest()
    document = repo.insert_document(db, DID, name, "application/pdf", len(raw), sha, text, True)
    write_raw(config.uploads_dir(), sha, raw)
    repo.attach_document(db, package, document)
    return document


def _audit(db, action):
    return [row for row in repo.list_audit(db, DID) if row["action"] == action]


# ---- approve redirects to the package next step ------------
def test_p0_26_approve_redirects_to_the_package_next_step(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    web = authenticated_client(web.app)
    url = f"/disputes/{DID}/packages/{package}/active-approval"
    page = web.get(url)
    review = service.read(fake, db, DID, package)
    data = {"csrf_token": web.cookies["csrf_token"], "review_sha256": review.review_sha256,
            **{"claim-" + key: value for key, value in decisions(review).items()}}
    response = web.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/disputes/{DID}/packages/{package}?done=approved#next-step"
    assert page.status_code == 200


def test_p0_26_idle_state_has_exactly_one_primary_approve_button(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    web = authenticated_client(web.app)
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    lint.assert_single_primary(html)
    primary = lint.primaries(html)[0]
    assert primary.tag == "button" and primary.text() == "Approve this package"
    assert "Approving does not send anything. Sending is a separate step." in html
    assert not re.search(r"aria-disabled|\sdisabled[\s>=]", html)
    # the note sits directly above the button, not in the step caption
    assert html.index("Approving does not send anything. Sending is a separate step.") < html.index("Approve this package</button>")
    assert html.rindex("Approving does not send anything. Sending is a separate step.") > html.index('id="claim-list"')


def test_p0_26_approved_state_continues_to_the_send_step(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    web = authenticated_client(web.app)
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    dom = parse(html)
    assert dom.find("h1").text() == "Package approved"
    assert "<title>Package approved" in html
    link = lint.primaries(html)
    assert len(link) == 1 and link[0].tag == "a" and link[0].text() == "Continue to send evidence"
    assert link[0].attrs["href"].endswith("#next-step")
    assert not [n for n in dom.walk() if n.tag == "input" and n.attrs.get("type") == "checkbox" and "required" in n.attrs]
    assert not [n for n in dom.walk() if n.tag == "button" and n.text() == "Approve this package"]
    assert "Nothing has been sent to PayPal yet." in html and "You approved this package on" in html
    assert "Return to the package" not in html
    assert not re.search(r"aria-disabled", html)


def test_p0_26_approved_state_without_tracking_lists_what_is_missing(mode_prepared, monkeypatch):
    import dataclasses
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    real_read = service.read
    # the approval stands, but the page reads a package with no sendable request: it must list the fix, not offer Send
    monkeypatch.setattr(service, "read", lambda *a, **k: dataclasses.replace(real_read(*a, **k), request_input=None))
    web = authenticated_client(web.app)
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    assert "Cannot send yet" in html
    assert 'href="/disputes/%s/packages/%s#field-tracking_number"' % (DID, package) in html
    assert "Continue to send evidence" not in html
    lint.assert_single_primary(html)


# ---- delivered statement -----------------------------------
def _delivered_statement(db, package, text="The parcel was delivered to the buyer.", source_ids=()):
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 5, "text": text,
                                      "source_ids": list(source_ids), "basis": "seller_statement"}])
    return [r for r in repo.list_claims(db, package) if r["text"] == text][0]["id"]


def test_p0_25_blocked_details_name_the_statement_and_point_at_it(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    claim_id = _delivered_statement(db, package)
    review = service.read(fake, db, DID, package)
    assert isinstance(review.blocked, tuple) and all(isinstance(m, str) for m in review.blocked)
    detail = [d for d in review.blocked_details if d["claim_id"] == claim_id][0]
    assert detail["kind"] == "unsupported" and detail["phrase"].lower() == "delivered" and detail["number"] == 2
    web = authenticated_client(web.app)
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    dom = parse(html)
    assert "Statement 2 says “delivered”" in html and "Change the wording" in html
    edit = [a for a in dom.find_all("a") if a.attrs.get("href", "").endswith(f"#claim-{claim_id}")]
    assert edit and edit[0].text() == "Edit statement 2" and "btn-primary" in edit[0].attrs["class"]
    assert dom.find(None, id="approval-block-reasons") is not None or dom.find(None, data_state="blocked") is not None
    assert not [n for n in dom.walk() if n.tag == "button" and n.text() == "Approve this package"]
    assert 'data-verdict="blocked"' in html
    assert "These words claim the parcel arrived" in html
    lint.assert_single_primary(html)


def test_p0_25_two_blocked_statements_give_two_items(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    first = _delivered_statement(db, package, "The parcel was delivered to the buyer.")
    second = _delivered_statement(db, package, "The buyer signed for the box.")
    review = service.read(fake, db, DID, package)
    ids = [d["claim_id"] for d in review.blocked_details]
    assert first in ids and second in ids
    web = authenticated_client(web.app)
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    links = re.findall(r'href="[^"]*#claim-(\d+)"', html)
    assert str(first) in links and str(second) in links
    assert len(lint.primaries(html)) == 1


def test_p0_25_block_cannot_be_overridden_by_a_posted_form(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    _delivered_statement(db, package)
    review = service.read(fake, db, DID, package)
    web, token = _web(mode_prepared)
    data = {"csrf_token": token, "review_sha256": review.review_sha256,
            **{"claim-" + key: value for key, value in decisions(review).items()}}
    response = web.post(f"/disputes/{DID}/packages/{package}/active-approval", data=data, follow_redirects=False)
    assert response.status_code == 400
    assert repo.get_latest_valid_approval(db, package) is None


# ---- a sent version cannot be approved again ---------------
def test_p0_20b_a_sent_version_cannot_be_approved_again(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    approval_id = service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    after = service.read(sender, db, DID, package)
    with pytest.raises(service.ReviewError) as caught:
        service.approve(sender, db, DID, package, after.review_sha256, decisions(after), "", "")
    assert str(caught.value) == USED_UP
    web, token = _web(mode_prepared, sender)
    url = f"/disputes/{DID}/packages/{package}/active-approval"
    data = {"csrf_token": token, "review_sha256": after.review_sha256,
            **{"claim-" + key: value for key, value in decisions(after).items()}}
    response = web.post(url, data=data, follow_redirects=False)
    assert response.status_code == 400 and USED_UP in response.text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    page = web.get(url).text
    assert "Approval used for the send at" in page
    assert "<form" not in page.replace('<form method="post" data-guard-submit', "") or "claim-list" not in page
    assert not [n for n in parse(page).walk() if n.tag == "input" and n.attrs.get("type") == "checkbox"]
    assert repo.get_latest_valid_approval(db, package)["id"] == approval_id


# --- wording
def test_a2_2_confirmed_send_headline_is_evidence_sent_to_paypal(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    web, token = _web(mode_prepared, sender)
    response = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token})
    assert response.status_code == 200
    h1 = parse(response.text).find("h1").text()
    assert h1 == "Evidence sent to PayPal"
    assert "Evidence received by PayPal" not in response.text
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    for path in (root / "copilot/web/templates/result.html", root / "copilot/core/send_service.py",
                 root / "copilot/web/routes_send.py"):
        assert "Evidence received by PayPal" not in path.read_text(encoding="utf-8")


# ---- confirmed send with a cleared approval is answered without a post ---
def test_r_r_c1_confirmed_send_with_a_cleared_approval_is_answered_without_a_post(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    first = send_package(sender, db, DID, package)
    assert first.sent and first.note and repo.get_latest_valid_approval(db, package) is None
    again = send_package(sender, db, DID, package)
    assert sender.posts == 1
    assert again.http_status == 409 and again.title == "Package already received" and not again.sent
    assert again.message == ALREADY


def test_r_r_c1_route_shows_package_already_received(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    web, token = _web(mode_prepared, sender)
    assert web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token}).status_code == 200
    set_test_csrf(web, token)
    again = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token})
    assert sender.posts == 1
    assert "Package already received" in again.text and "Nothing new was sent." in again.text


def test_r_r_c1_mcp_gives_the_same_message(prepared, monkeypatch):
    from copilot import mcp_server
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    assert send_package(sender, db, DID, package).sent
    assert repo.get_latest_valid_approval(db, package) is None
    monkeypatch.setattr(mcp_server, "_make_client", lambda: sender)
    out = mcp_server.send_approved_package(DID)
    assert sender.posts == 1
    assert out["http_status"] == 409 and out["title"] == "Package already received" and out["sent"] is False
    assert ALREADY in out["message"] and "Not confirmed as received" not in out["message"]


# ---- five xx after write -----------------------------------
class FiveXXAfterWrite(PayPalAfterSend):
    """The POST is processed by PayPal but the answer is a 503: the read-back is the only truth."""

    def call(self, method, path, op, **kwargs):
        result = super().call(method, path, op, **kwargs)
        if method == "POST":
            from copilot.core.disputes import PayPalError
            raise PayPalError(503, "SERVICE_UNAVAILABLE", "DEBUG-1", {})
        return result


def test_r_r2c_3_a_5xx_whose_readback_confirms_is_a_confirmed_send_with_a_note(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = FiveXXAfterWrite(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1
    assert result.http_status == 200 and result.sent and result.title == RECEIVED_MESSAGE
    assert result.note and result.note.startswith("Changed after sending:")
    assert _attempt_state(db, review) == "confirmed"


def test_r_r2c_3_an_unconfirmed_5xx_after_a_change_stays_outcome_unknown(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = FiveXXAfterWrite(fake, review, trackers_after=("SOMEONE-ELSE-77",), lands=False)
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1
    assert result.http_status == 202 and not result.sent
    assert result.title == "Outcome unknown, review required"
    assert _attempt_state(db, review) == "sent_unconfirmed"


# ---- a failed reread after a confirmed send is not called a change ---
def test_r_r2c_7_a_failed_reread_after_a_confirmed_send_is_not_called_a_change(prepared, monkeypatch):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    real_read = service.read

    def read_that_fails_after_the_post(client, *args, **kwargs):
        if sender.posts and getattr(client, "after", False):
            raise RuntimeError("synthetic: sources cannot be read")
        return real_read(client, *args, **kwargs)
    monkeypatch.setattr(service, "read", read_that_fails_after_the_post)
    result = send_package(sender, db, DID, package)
    assert result.sent and result.http_status == 200
    assert result.note == ("The sources could not be checked again after sending; review them. "
                           "Nothing was re-sent.")
    assert "Changed after sending" not in result.note


def test_r_r2c_7_a_real_difference_still_reads_changed_after_sending(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    result = send_package(sender, db, DID, package)
    assert result.note.startswith("Changed after sending: ") and result.note.endswith("nothing was re-sent.")


# ---- a 2xx post whose row turned abandoned is sent review required ---
@pytest.mark.parametrize("trackers_after", [(OWN_NUMBER,), ("SOMEONE-ELSE-77",)], ids=["unchanged", "changed"])
def test_r_r2c_9_a_2xx_post_whose_row_turned_abandoned_is_sent_review_required(prepared, monkeypatch, trackers_after):
    from copilot.core import send_service
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=trackers_after)

    def abandoned(*args, **kwargs):
        return {"confirmed": False, "files_ok": False, "missing_files": [], "details": None, "abandoned": True}
    monkeypatch.setattr(send_service, "reconcile_after_send", abandoned)
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1
    assert result.http_status == 202 and result.title == "Sent, review required"
    assert "Not sent" not in result.title and "marked as not received" not in result.message


# ---- raw checks --------------------------------------------
def _raw_checks(client, db):
    from copilot.core.disputes import normalize
    from copilot.web.routes_package import _checks_for
    from datetime import datetime, timezone
    norm = normalize(client.call("GET", f"/v1/customer/disputes/{DID}", "show_dispute"), datetime.now(timezone.utc))
    checks, _ = _checks_for(norm, DID, client, db)
    return checks


def test_r_r2c_5_the_review_shows_paypals_own_tracker_but_the_digest_ignores_it(prepared):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    after = service.read(sender, db, DID, package)
    unmasked = _raw_checks(sender, db)
    assert after.checks == unmasked
    assert after.approval and after.approval["id"] == approval_id and not after.cleared
    assert not _audit(db, "invalidate_active_approval")
    masked_checks = _raw_checks(service.OwnTrackerMask(sender, service.sent_tracking_numbers(review.request_input), []), db)
    assert masked_checks != unmasked, "the fixture must make the registered tracker visible, or the test proves nothing"


def test_r_r2c_2_approval_page_and_package_page_agree_on_the_tracking_checks(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    after = service.read(sender, db, DID, package)
    web, _ = _web(mode_prepared, sender)
    unmasked = {c.id: c.state for c in _raw_checks(sender, db)}
    assert {c.id: c.state for c in after.checks} == unmasked
    assert web.get(f"/disputes/{DID}/packages/{package}/active-approval").status_code == 200


# ---- the pre send check is pinned a tracker that appears before ---
def test_r_ti_5_the_pre_send_check_is_pinned_a_tracker_that_appears_before_the_send_stops_it(prepared):
    fake, db, package, review, approval_id = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    sender.trackers = [_tracker(OWN_NUMBER)]               # PayPal gained the tracker after the approval
    result = send_package(sender, db, DID, package)
    assert result.http_status == 409 and not result.sent
    assert sender.posts == 0
    assert _attempt_state(db, review) == "blocked"


def test_r_ti_6_a_tracker_present_at_approval_and_unchanged_by_the_send_is_no_change(prepared):
    _, fake, db, package, _ = prepared
    fake.raw["evidences"] = [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}]
    review = ready(fake, db, package)
    sender = PayPalAfterSend(fake, review)
    sender.trackers = [_tracker(OWN_NUMBER)]
    review2 = service.read(sender, db, DID, package)
    approval_id = service.approve(sender, db, DID, package, review2.review_sha256, decisions(review2), "", "")
    sender.review = review2
    result = send_package(sender, db, DID, package)
    assert result.sent and result.note is None
    assert repo.get_latest_valid_approval(db, package)["id"] == approval_id


def test_r_ti_7_route_shows_the_change_note_after_the_headline(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    web, token = _web(mode_prepared, sender)
    response = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token})
    assert response.status_code == 200
    dom = parse(response.text)
    note = dom.find(None, data_state="changed-after-sending")
    assert note is not None and note.text().startswith("Changed after sending:")
    assert response.text.index("<h1") < response.text.index('data-state="changed-after-sending"')


def test_r_ti_7_mcp_message_carries_the_note(prepared, monkeypatch):
    from copilot import mcp_server
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    monkeypatch.setattr(mcp_server, "_make_client", lambda: sender)
    out = mcp_server.send_approved_package(DID)
    assert out["sent"] is True and "Changed after sending:" in out["message"]
    assert out["message"].endswith("nothing was re-sent.")


# ---- a receipt without a trackers baseline does not bring the difference back ---
def test_r_ti_14_a_receipt_without_a_trackers_baseline_does_not_bring_the_difference_back(prepared):
    import json
    fake, db, package, review, approval_id = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    with sqlite3.connect(db) as conn:                      # a receipt as the earlier release wrote it: no trackers key
        raw = conn.execute("SELECT confirmations_json FROM active_approval_receipts WHERE approval_id=?",
                           (approval_id,)).fetchone()[0]
        confirmations = json.loads(raw)
        del confirmations[0]["approved_case_evidence"]["trackers"]
        conn.execute("UPDATE active_approval_receipts SET confirmations_json=? WHERE approval_id=?",
                     (json.dumps(confirmations, ensure_ascii=False, sort_keys=True), approval_id))
    after = service.read(sender, db, DID, package)
    assert after.approval and after.approval["id"] == approval_id and not after.cleared
    assert not _audit(db, "invalidate_active_approval")


# ---- a file added after a confirmed send is never reported as received ---
def test_r_r2c_1a_a_file_added_after_a_confirmed_send_is_never_reported_as_received(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = PayPalAfterSend(fake, review)
    assert send_package(sender, db, DID, package).sent
    sender.body["status"], sender.body["links"] = fake.raw["status"], copy.deepcopy(fake.raw["links"])   # PayPal lists the link again
    _attach_file(db, package)
    current = service.read(sender, db, DID, package)
    assert current.approval is None                         # the attachment changed what was approved
    # The version PayPal confirmed is spent, also with a file added afterwards: it cannot be approved again
    # (edited assertion: this test used to approve it and expect the 206 from the send)
    with pytest.raises(service.ReviewError) as caught:
        service.approve(sender, db, DID, package, current.review_sha256, decisions(current), "", "")
    assert str(caught.value) == USED_UP
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1, "an exact package is never posted twice"
    assert not result.sent and result.http_status in (206, 400, 409)
    assert result.title != RECEIVED_MESSAGE


# ---- a settling dispute ----------------------------------------------
def _settling(sender):
    sender.body["status"] = "UNDER_REVIEW"
    sender.body["links"] = [{"rel": "self", "method": "GET",
                             "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{DID}"}]


def test_a2_5c_a_send_while_paypal_settles_is_stopped_locally_with_one_sentence(prepared):
    fake, db, package, review, _ = _approved(prepared)
    sender = SendFake(fake, review)
    _settling(sender)
    result = send_package(sender, db, DID, package)
    assert sender.posts == 0 and not result.sent
    assert result.message == "Not sent: PayPal is updating this dispute. Check again in a minute."
    assert _attempt_state(db, review) is None, "a transient wait must not leave a blocked attempt behind"


def test_a2_5c_route_shows_that_sentence_on_the_result_page(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = SendFake(fake, review)
    _settling(sender)
    web, token = _web(mode_prepared, sender)
    response = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token})
    assert "Not sent: PayPal is updating this dispute. Check again in a minute." in response.text
    assert sender.posts == 0


# ---- send answers 303 to a stored result that renders the same twice ---
def test_p1_39_send_answers_303_to_a_stored_result_that_renders_the_same_twice(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    web, token = _web(mode_prepared, sender)
    first = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token}, follow_redirects=False)
    assert first.status_code == 303
    location = first.headers["location"]
    assert re.fullmatch(rf"/disputes/{DID}/packages/{package}/result/[A-Za-z0-9_-]{{16,}}", location), location
    one, two = web.get(location), web.get(location)
    assert one.status_code == two.status_code == 200
    assert one.text == two.text
    assert "no-store" in one.headers["cache-control"]
    assert sender.posts == 1, "reading the stored result never calls PayPal"


def test_p1_39_the_stored_status_is_served_back(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = SendFake(fake, review)
    _settling(sender)
    web, token = _web(mode_prepared, sender)
    first = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token}, follow_redirects=False)
    assert first.status_code == 303
    again = web.get(first.headers["location"])
    assert again.status_code == 409 and "PayPal is updating this dispute" in again.text


def test_p1_39_an_unknown_or_foreign_result_id_is_a_neutral_404(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    web, token = _web(mode_prepared, sender)
    first = web.post(f"/disputes/{DID}/packages/{package}/send", data={"csrf_token": token}, follow_redirects=False)
    rid = first.headers["location"].rsplit("/", 1)[1]
    other = repo.insert_package(db, DID, "human_edit", True, [])
    foreign = web.get(f"/disputes/{DID}/packages/{other}/result/{rid}")
    assert foreign.status_code == 404 and f"/disputes/{DID}/packages/{other}" in foreign.text
    assert web.get(f"/disputes/{DID}/packages/{package}/result/not-a-real-id-0000000").status_code == 404
    assert web.get(f"/disputes/PP-R-DEMO-0000002/packages/{package}/result/{rid}").status_code in (400, 404)


def test_p1_39_check_again_also_redirects_to_a_stored_result(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review)
    web, token = _web(mode_prepared, sender)
    response = web.post(f"/disputes/{DID}/packages/{package}/check", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303 and "/result/" in response.headers["location"]
    page = web.get(response.headers["location"])
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
    assert sender.posts == 0


# ---- rollout templates follow the shell contract -----------
def test_rollout_templates_follow_the_shell_contract():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3] / "copilot" / "web" / "templates"
    assert lint.template_lint([root / "features/active_approval/review.html", root / "result.html"]) == []


def _approval_page(mode_prepared, mutate=None):
    web, fake, db, package, _ = mode_prepared
    if mutate:
        mutate(fake, db, package)
    web = authenticated_client(web.app)
    return web.get(f"/disputes/{DID}/packages/{package}/active-approval").text


def test_rollout_approval_page_breadcrumb_caption_and_no_back_links(mode_prepared):
    html = _approval_page(mode_prepared)
    package = mode_prepared[3]
    crumbs = lint.breadcrumb_items(html)
    assert [label for label, _ in crumbs] == ["All disputes", f"Dispute {DID}", "Evidence package v1",
                                             "Review each statement and approve"]
    assert crumbs[2][1] == f"/disputes/{DID}/packages/{package}" and crumbs[-1][1] is None
    assert lint.back_links(html) == []
    assert "Step 3 of 4: Review each statement and approve" in html
    assert "Back to package and edit statements" not in html
    assert 'href="/disputes/%s/packages/%s#statements"' % (DID, package) in html
    assert "Change statements on the package page" in html
    assert f"<title>Review each statement and approve: {DID} - Proofie</title>" in html
    assert lint.large_labels_ok(html) and lint.required_sentence_count(html) <= 1


def test_rollout_what_will_be_sent_is_exact_content_and_not_translated(mode_prepared):
    html = _approval_page(mode_prepared)
    dom = parse(html)
    summary = dom.find("section", id="send-summary")
    assert summary.attrs.get("translate") != "no"            # The section's headings and labels translate
    exact = summary.find(None, data_exact_content="")
    assert exact is not None and exact.attrs.get("translate") == "no"
    assert "Shown exactly as PayPal will receive it." in html
    assert 'novalidate' in html and "data-sends" not in html


def test_rollout_single_primary_in_every_approval_state(mode_prepared, monkeypatch):
    web, fake, db, package, _ = mode_prepared
    idle = _approval_page(mode_prepared)
    lint.assert_single_primary(idle)
    web = authenticated_client(web.app)
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    approved = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    lint.assert_single_primary(approved)
    assert lint.back_links(approved) == []
    # error state: the form posted without ticking anything
    repo.invalidate_approval(db, repo.get_latest_valid_approval(db, package)["id"])
    token = web.get(f"/disputes/{DID}/packages/{package}/active-approval").cookies.get("csrf_token") or web.cookies["csrf_token"]
    fresh = service.read(fake, db, DID, package)
    errored = web.post(f"/disputes/{DID}/packages/{package}/active-approval",
                       data={"csrf_token": web.cookies["csrf_token"], "review_sha256": fresh.review_sha256})
    assert errored.status_code == 400
    lint.assert_single_primary(errored.text)
    assert "<title>Error: " in errored.text


@pytest.mark.parametrize("title,message,status", [
    (RECEIVED_MESSAGE, "PayPal now shows this evidence as received.", 200),
    ("Package already received", "PayPal previously confirmed this exact package as received. Nothing new was sent.", 409),
    ("Result pending — Check again", "The request was sent, but three checks (~5s, ~15s, ~30s apart) did not show it.", 202),
    ("Outcome unknown, review required", "The send outcome is unknown.", 202),
    ("Not sent", "PayPal rejected this evidence.", 502),
    ("Not sent", "Not sent: PayPal is updating this dispute. Check again in a minute.", 409),
    ("Not approved", "This package is not approved yet.", 400),
])
def test_rollout_result_states_have_one_primary_breadcrumb_and_no_back_link(title, message, status):
    from copilot.web.routes_send import _page
    html = _page(title, message, DID, 1, status).body.decode()
    lint.assert_single_primary(html)
    assert lint.back_links(html) == [] and "Back to review" not in html
    assert [label for label, _ in lint.breadcrumb_items(html)][-1] == "Send result"
    assert "Step 4 of 4: Send evidence to PayPal" in html
    assert html.count("<h1") == 1


# ---- confirmed result has one headline and one forward step ---
def test_p1_38_confirmed_result_has_one_headline_and_one_forward_step():
    from copilot.web.routes_send import _page
    html = _page(RECEIVED_MESSAGE, "PayPal now shows this evidence as received.", DID, 1, 200,
                 note="Changed after sending: x; nothing was re-sent.").body.decode()
    dom = parse(html)
    main = dom.find("main")
    assert main.text().count("Evidence sent to PayPal") == 1
    primary = lint.primaries(html)[0]
    assert primary.text() == "View the dispute and PayPal's answer" and primary.attrs["href"] == f"/disputes/{DID}"
    banner = dom.find(None, class_="banner-ok")
    assert banner is not None and not banner.find_all("form")
    assert "Result displayed" not in html and "Back to review" not in html
    check = [n for n in dom.walk() if n.tag == "button" and n.text() == "Check again"]
    assert len(check) == 1 and "btn-secondary" in check[0].attrs["class"]
    assert not [n for n in banner.walk() if n is check[0]], "Check again sits outside the success panel"
    notice = dom.find("p", class_="notice")
    assert notice is not None and notice.attrs["data-state"] == "changed-after-sending"
    assert not [n for n in banner.walk() if n is notice], "the note is its own paragraph, not part of the banner"


def test_p1_37_uncertain_result_headline_body_and_one_check_again():
    from copilot.core.send_recovery import PENDING_MESSAGE
    from copilot.web.routes_send import _page
    html = _page(PENDING_MESSAGE, "x (~5s)", DID, 1, 202).body.decode()
    dom = parse(html)
    assert dom.find("h1").text() == "Send attempted - not confirmed by PayPal"
    assert "Proofie asked PayPal three times and did not get a confirmation yet. Proofie will not send it again on its own." in html
    assert "~5s" not in html
    primary = lint.primaries(html)
    assert len(primary) == 1 and primary[0].text() == "Check again"
    assert "Reads the dispute from PayPal. Sends nothing." in html


# ---- what will be sent lists text tracking carrier and files ---
def test_p1_40_what_will_be_sent_lists_text_tracking_carrier_and_files(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    none = _approval_page(mode_prepared)
    assert "Files (0)" in none and "Files: none. PayPal receives the text only." in none
    assert f'href="/disputes/{DID}/packages/{package}#attachments"' in none and "Attach a document" in none
    _attach_file(db, package, "label-proof.pdf")
    html = _approval_page(mode_prepared)
    assert "Files (1)" in html and "label-proof.pdf" in html
    assert "<dt>Carrier</dt>" in html and "<dt>Tracking number</dt>" in html and "Your text, as PayPal receives it" in html
    assert '<details data-state="send-prefix"><summary>Technical details</summary><p>PayPal receives this line before your text: DEMO DATA' in html
    assert "only when you press Send evidence to PayPal on the package page" in html


def test_p1_40_a_note_over_2000_characters_shows_the_cut_notice(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    long_text = ("Shipped by the carrier with the receipt kept. " * 50)[:2100]
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 7, "text": long_text,
                                      "source_ids": [], "basis": "seller_statement"}])
    html = _approval_page(mode_prepared)
    assert "PayPal receives only the first 2,000 characters." in html
    review = service.read(fake, db, DID, package)
    assert len(review.request_input["evidences"][0]["notes"]) == 2000


# ---- counters checkbox texts and spacing -------------------
def test_p1_41_counters_checkbox_texts_and_spacing(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 1, "text": "A synthetic statement of mine.",
                                      "source_ids": [], "basis": "seller_statement"}])
    html = _approval_page(mode_prepared)
    dom = parse(html)
    review = service.read(fake, db, DID, package)
    total = len(review.claims)
    counters = dom.find(None, id="approval-counters").text()
    own = sum(c.verdict == "seller_statement" for c in review.claims)
    assert own == total == 2 and not any(c.verdict in ("unverifiable", "conflict") for c in review.claims)
    # the counters say what is true: both statements cite no document; no conflict, so nothing is said about conflicts
    assert counters == f"0 of {total} statements checked. {own} have no document, so Proofie cannot verify them."
    assert "source.Required" not in html and "statement.Required" not in html
    assert "I checked statement 2 and send it as my own statement." in html
    assert ("I checked statement 1 and send it as my own statement." in html or "I compared statement 1 with the source above." in html
            or "I confirm statement 1 is true and says what I mean." in html)
    assert "Your own statement. No document is cited." in html
    names = {n.attrs["name"] for n in dom.walk() if n.tag == "input" and n.attrs.get("type") == "checkbox"}
    assert names == {"claim-" + c.key for c in review.claims}
    assert {n.attrs["value"] for n in dom.walk() if n.tag == "input" and n.attrs.get("type") == "checkbox"} <= {"confirmed", "seller_statement"}


def test_p1_42_typed_phrase_hint_and_source_inside_details(mode_prepared):
    from copilot.features.active_approval import rules
    web, fake, db, package, _ = mode_prepared
    fake.raw["dispute_amount"] = {"currency_code": "USD", "value": "750.00"}
    html = _approval_page(mode_prepared)
    dom = parse(html)
    assert rules.PHRASE in html
    assert "Proofie's own extra check for large amounts and fraud words. It is not a PayPal rule." in html.replace("&#39;", "'")
    import html as htmllib
    holders = [d for d in dom.find_all("details") if rules.SOURCE in htmllib.unescape(d.text())]
    assert len(holders) == 1 and holders[0].find("summary").text() == "Technical details"
    import html as htmllib
    assert htmllib.unescape(html).count(rules.SOURCE) == 1
    assert "at or above  " not in html


def test_u_p1_15b_an_approval_error_shows_twice_and_each_missing_tick_is_named(mode_prepared):
    web, fake, db, package, _ = mode_prepared
    web, token = _web(mode_prepared)
    review = service.read(fake, db, DID, package)
    response = web.post(f"/disputes/{DID}/packages/{package}/active-approval",
                        data={"csrf_token": token, "review_sha256": review.review_sha256})
    sentence = "Confirm every statement individually against its source."
    assert response.status_code == 400 and response.text.count(sentence) == 2
    assert "Tick statement 1 to confirm you checked it." in response.text
    stale = web.post(f"/disputes/{DID}/packages/{package}/active-approval",
                     data={"csrf_token": token, "review_sha256": "0" * 64})
    assert "Tick statement" not in stale.text


# ---- result labels come from the shared status vocabulary ---
def test_p1_03b_result_labels_come_from_the_shared_status_vocabulary():
    from copilot.web import status
    from copilot.web.routes_send import _page
    html = _page(RECEIVED_MESSAGE, "PayPal now shows this evidence as received.", DID, 1, 200).body.decode()
    assert status.evidence_status(status.SendRecord(state="confirmed")).label in html
    html = _page("Not sent", "PayPal rejected this evidence. Review the technical details below.", DID, 1, 502).body.decode()
    assert status.evidence_status(status.SendRecord(state="refused")).label in html
