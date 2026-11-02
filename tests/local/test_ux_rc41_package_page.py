"""What the package page shows (next step, files, statements, shipment form, banners).

Same offline setup as test_ux_rc41_package.py (fake clients, temp database). Checks run on the DOM and on the request a
control would send, never only on words.
"""
import re

import pytest
from _dom import parse, state
import _ux_lint as lint

from copilot import config
from copilot.db import repo
from test_ux_rc41_package import (DID, OTHER, PDF, ROOT, TEMPLATE, add_claims, approve_passive, approved, draft, env,  # noqa: F401
                                  mark_attempt, mark_sent, new_package, page, save_shipment, upload)
import test_routes_send as send_tests

pytestmark = [pytest.mark.usefixtures("web_mode")]


def panel(html):
    return parse(html).find(id="next-step")


def forms_to(node, tail):
    return [f for f in node.find_all("form") if f.attrs.get("action", "").endswith(tail)]


# ---------------------------------------------------------------------------------------------- next step: send
def test_approved_and_sendable_has_exactly_one_send_form_inside_the_panel(env):
    c, db, token, _ = env
    pkg = draft(c, db, token, "First.", "Second.")
    doc = repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha-a", "x", True)
    repo.attach_document(db, pkg, doc)                                       # before approving: the approval covers the file
    approve_passive(c, token, pkg)
    html = page(c, pkg).text
    dom = parse(html)
    sends = forms_to(dom, "/send")
    assert len(sends) == 1 and sends[0].inside("section") and "data-sends" in sends[0].attrs
    step = dom.find(id="next-step")
    assert sends[0] in step.find_all("form")
    button = sends[0].find("button")
    assert "btn-primary" in button.attrs["class"].split() and button.text() == "Send evidence to PayPal"
    assert [n.text() for n in lint.primaries(html)] == ["Send evidence to PayPal"]
    note = state(dom, "send-note")
    assert note.text() == "Sends package v1 (2 statements, 1 file) to PayPal sandbox once. Proofie cannot withdraw it."
    # the panel precedes the shipment details
    assert html.index('id="next-step"') < html.index('id="shipment-details"')


def test_the_send_request_is_a_post_with_the_csrf_token_to_the_unchanged_route(env):
    c, db, token, _ = env
    pkg = approved(c, db, token)
    form = forms_to(parse(page(c, pkg).text), "/send")[0]
    assert form.attrs["method"] == "post" and form.attrs["action"] == f"/disputes/{DID}/packages/{pkg}/send"
    assert form.find("input", name="csrf_token").attrs["value"]


def test_approved_without_a_tracking_number_lists_the_fixes_and_offers_no_send(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    add_claims(db, pkg, "Statement.")
    repo.insert_approval(db, pkg, "not-sendable-yet-%d" % pkg, "vendor", "ok")
    html = page(c, pkg).text
    dom = parse(html)
    box = state(dom, "send-needs-tracking")
    assert box is not None and box.attrs["id"] == "next-step"
    hrefs = [a.attrs["href"] for a in box.find_all("a")]
    assert "#field-tracking_number" in hrefs and f"/disputes/{DID}/workspace/prepare#documents" in hrefs
    assert not [n for n in dom.walk() if "aria-disabled" in n.attrs or "disabled" in n.attrs]
    assert not forms_to(dom, "/send")
    assert [n.text() for n in lint.primaries(html)] == ["Add the tracking number"]
    assert "Add tracking in PayPal" not in html


def test_cannot_send_without_a_carrier_when_the_carrier_is_required(env, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_CARRIER_REQUIRED", "true")
    c, db, token, _ = env
    pkg = new_package(c, token)
    add_claims(db, pkg, "Statement.")
    c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": pkg, "tracking_number": "DEMO-9"},
           follow_redirects=False)
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import approval_fingerprint_with_attachments, build_request_input, pick_tracking
    request_input = build_request_input(repo.list_claims(db, pkg), *pick_tracking(extracted_docs_for(db, DID)))
    repo.insert_approval(db, pkg, approval_fingerprint_with_attachments(DID, "provide_evidence", request_input, []),
                         "vendor", "ok")
    box = panel(page(c, pkg).text)
    assert box.attrs["data-next-step"] == "cannot_send" and "#field-carrier" in [a.attrs["href"] for a in box.find_all("a")]


# -------------------------------------------------------------------------------------- next step: review / approve
def test_statements_and_tracking_but_not_approved_leads_to_the_active_approval_page(env, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    c, db, token, _ = env
    pkg = draft(c, db, token)
    html = page(c, pkg).text
    link = panel(html).find("a", class_="btn-primary")
    assert link.attrs["href"] == f"/disputes/{DID}/packages/{pkg}/active-approval"
    assert link.text() == "Open approval review" and "btn-large" in link.attrs["class"].split()
    assert not forms_to(parse(html), "/approve")
    assert len(lint.primaries(html)) == 1


def test_flag_off_the_approve_form_lives_in_the_panel(env, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "false")
    c, db, token, _ = env
    pkg = draft(c, db, token)
    html = page(c, pkg).text
    step = panel(html)
    forms = forms_to(step, "/approve")
    assert len(forms) == 1 and step.attrs["data-next-step"] == "approve_here"
    assert forms[0].find("button").text() == "Approve this package"
    assert [n.text() for n in lint.primaries(html)] == ["Approve this package"]


def test_the_approval_section_keeps_only_status_sentences(env):
    c, db, token, _ = env
    pkg = approved(c, db, token)
    dom = parse(page(c, pkg).text)
    approval = dom.find("h2", id="approval")
    assert approval is not None
    assert not forms_to(dom, "/send") or forms_to(dom, "/send")[0].inside("section")
    assert state(dom, "approved") is not None and "Approving does not send anything" not in state(dom, "approved").text()


# ----------------------------------------------------------------------------------------------- one primary, lint
@pytest.mark.parametrize("build", ["new", "statements", "approved", "sent", "pending"])
def test_exactly_one_primary_and_no_bare_controls_in_every_view_state(env, build):
    c, db, token, _ = env
    if build == "new":
        pkg = new_package(c, token)
    elif build == "statements":
        pkg = draft(c, db, token)
    elif build == "approved":
        pkg = approved(c, db, token)
    elif build == "sent":
        pkg = approved(c, db, token)
        mark_sent(db, pkg)
    else:
        pkg = approved(c, db, token)
        mark_attempt(db, pkg, "sent_unconfirmed")
    html = page(c, pkg).text
    if build == "sent":
        lint.assert_single_primary(html)
    else:
        lint.assert_single_primary(html)
    assert lint.bare_buttons(html) == [] and lint.large_labels_ok(html)
    assert lint.back_links(html) == [] and lint.required_sentence_count(html) == 0


def test_the_template_follows_the_shell_contract():
    assert lint.template_lint([TEMPLATE]) == []


def test_breadcrumb_title_caption_and_no_store(env):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    r = page(c, pkg)
    assert lint.breadcrumb_items(r.text) == [("All disputes", "/disputes"), (f"Dispute {DID}", f"/disputes/{DID}"),
                                             ("Evidence package v1", None)]
    assert parse(r.text).find("title").text() == f"Evidence package v1: {DID} - Proofie"
    assert "Step 2 of 4: Prepare evidence package" in parse(r.text).find("p", class_="step-note").text()
    assert r.headers["cache-control"] == "no-store"


def test_a_missing_package_page_is_a_404_with_one_primary_link(env):
    c, db, token, _ = env
    r = c.get(f"/disputes/{DID}/packages/999")
    assert r.status_code == 404 and state(parse(r.text), "package-not-found") is not None
    lint.assert_single_primary(r.text)
    assert lint.breadcrumb_items(r.text)[-1] == ("Package not found", None)


def test_what_will_be_sent_is_not_translated(env):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    dom = parse(page(c, pkg).text)
    will = state(dom, "will-send")
    # The summary is UI text and stays translatable; only literal values (tracking, file names) are frozen
    node = will
    while node is not None:
        assert node.attrs.get("translate") != "no", node.tag
        node = node.parent
    assert [s for s in will.find_all("span") if s.attrs.get("translate") == "no"]
    assert "DEMO-FDX-000001" in [s.text() for s in will.find_all("span") if s.attrs.get("translate") == "no"]


# ---------------------------------------------------------------------------------------------- order and anchors
def test_sections_and_anchors_come_in_the_planned_order(env):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    html = page(c, pkg).text
    ids = ["next-step", "shipment-details", "attachments", "statements", "checks", "approval"]
    positions = [html.index(f'id="{i}"') for i in ids]
    assert positions == sorted(positions)
    for anchor in ("field-tracking_number", "field-carrier"):
        assert f'id="{anchor}"' in html
    cid = repo.list_claims(db, pkg)[0]["id"]
    assert f'id="claim-{cid}"' in html
    assert html.index('id="claim-%d"' % cid) < html.index('id="checks"')


def test_checked_line_and_refresh_link_are_kept(env):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    dom = parse(page(c, pkg).text)
    bar = state(dom, "checked-at")
    assert bar is not None and bar.find("a").text() == "Refresh from PayPal"


# --------------------------------------------------------------------------------------------- shipment form
def test_carrier_precedes_tracking_inside_the_manual_fields_form(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    dom = parse(page(c, pkg).text)
    form = next(f for f in dom.find_all("form") if f.attrs["action"].endswith("/manual-fields"))
    assert "novalidate" in form.attrs
    ids = [n.attrs.get("id") for n in form.walk() if n.attrs.get("id") in ("field-carrier", "field-tracking_number")]
    assert ids == ["field-carrier", "field-tracking_number"]
    assert form.find("label", **{"for": "field-carrier"}).text().startswith("Carrier (shipping company)")
    assert form.find("label", **{"for": "field-tracking_number"}).text().startswith(
        "Tracking number (from the carrier's receipt)")
    intro = state(dom, "shipment-intro")
    assert "Required to send" in intro.text() and len(parse(page(c, pkg).text).find_all("p", data_state="shipment-intro")) == 1


def test_a_carrier_name_typed_as_the_tracking_number_is_explained_next_to_the_field(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    r = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": pkg, "tracking_number": "Fedex",
                                                       "carrier": "FEDEX"})
    assert r.status_code == 400
    dom = parse(r.text)
    error = dom.find("p", id="error-tracking_number")
    assert error is not None and "is a carrier name, not a tracking number" in error.text()
    assert dom.find("input", id="field-tracking_number").attrs["value"] == "Fedex"
    assert dom.find("details", data_state="shipment-form").attrs.get("open") is not None
    assert repo.get_manual_fields(db, DID) == {}


def test_shipment_form_is_closed_only_when_nothing_is_missing(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    first = page(c, pkg)
    assert "open" in parse(first.text).find("details", data_state="shipment-form").attrs
    token = first.cookies.get("csrf_token")
    values = {"tracking_number": "DEMO-1", "carrier": "FEDEX", "delivery_status": "shipped", "postal_code": "64000",
              "city": "Monterrey", "state": "NL", "delivery_date_day": "14", "delivery_date_month": "10",
              "delivery_date_year": "2026"}
    r = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": pkg, **values},
               follow_redirects=False)
    assert r.status_code == 303
    assert "open" not in parse(page(c, pkg).text).find("details", data_state="shipment-form").attrs


def test_requirement_groups_follow_the_send_gate(env, monkeypatch):
    c, db, token, _ = env
    pkg = new_package(c, token)
    dom = parse(page(c, pkg).text)
    groups = {li.attrs["data-requirement-group"]: li.text() for li in dom.find_all("li") if "data-requirement-group" in li.attrs}
    assert set(groups) == {"Required to send", "Needed for the checks", "Optional"}
    assert groups["Required to send"].startswith("Required to send (1)")
    monkeypatch.setenv("COPILOT_FEATURE_CARRIER_REQUIRED", "true")
    dom = parse(page(c, pkg).text)
    groups = {li.attrs["data-requirement-group"]: li.text() for li in dom.find_all("li") if "data-requirement-group" in li.attrs}
    assert groups["Required to send"].startswith("Required to send (2)")


# ----------------------------------------------------------------------------------------------------- files
def test_files_come_before_statements_and_each_row_carries_a_status_and_a_large_attach_button(env):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    upload(c, token, "label.pdf")
    upload(c, token, "photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    docs = repo.list_documents(db, DID)
    repo.attach_document(db, pkg, docs[0]["id"])
    html = page(c, pkg).text
    assert html.index('id="attachments"') < html.index('id="statements"')
    dom = parse(html)
    rows = {r.find_all("td")[1].text().split(" · ")[0]: r for r in dom.find("details", id="files-fold").find("table", class_="stack").find_all("tr") if r.find("td")}
    attach = [b for b in dom.find_all("button") if b.text().startswith("Attach to the response")]
    assert len(attach) == 1
    assert "btn-large" in attach[0].attrs["class"].split() and attach[0].find("span", class_="visually-hidden").text() == "photo.png"
    assert "Attached" in rows["label.pdf"].find_all("td")[0].text() and "Not attached" in rows["photo.png"].find_all("td")[0].text()
    remove = [b for b in dom.find_all("button") if b.text().startswith("Remove from the response")]
    assert len(remove) == 1 and "btn-link" in remove[0].attrs["class"].split()
    assert "Detach from the response" not in html


# ------------------------------------------------------------------------------------------------- statements
def test_statement_forms_use_textareas_and_the_new_button_words(env):
    c, db, token, _ = env
    pkg = draft(c, db, token, "First.", "Second.")
    html = page(c, pkg).text
    dom = parse(html)
    edits = dom.find_all("form", class_="claim-edit")
    assert len(edits) == 2
    for number, form in enumerate(edits, start=1):
        assert form.find("textarea", name="text") is not None
        assert form.find("button").text() == f"Save this statement {number}"
        assert form.find("label").text().startswith(f"Edit statement {number}")
    add = dom.find("form", class_="add-claim")
    assert add.find("button").text() == "Add this statement" and add.find("label").text().startswith("New statement")
    for banned in ("Fields marked Required must be filled in before saving.", ">Save<", ">Add<", ">Confirm<"):
        assert banned not in html
    assert "Your statements to PayPal (2)" in html and "What we're saying" not in html
    removes = [b for b in dom.find_all("button") if b.text().startswith("Remove this statement")]
    assert len(removes) == 2


def test_an_image_reading_is_a_sentence_and_a_confirm_button(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    doc = repo.insert_document(db, DID, "photo.png", "image/png", 10, "sha-p", None, False)
    repo.insert_claims(db, pkg, [{"evidence_index": 0, "claim_index": 0, "text": "The label shows tracking DEMO-7.",
                                  "source_ids": [f"doc_{doc}"], "basis": "model_interpretation",
                                  "suggested_field": "ship_date", "suggested_value": "2026-10-14"}])
    dom = parse(page(c, pkg).text)
    sentence = state(dom, "reading-suggested").text()
    assert sentence == "Proofie read this from photo.png: Ship date 14 October 2026. Check it against the image."
    form = next(f for f in dom.find_all("form") if f.attrs["action"].endswith("/confirm-reading"))
    assert form.find("button").text() == "Confirm this reading" and "=" not in form.find("button").text()
    hidden = {i.attrs["name"]: i.attrs["value"] for i in form.find_all("input")}
    assert hidden["field_name"] == "ship_date" and hidden["value"] == "2026-10-14" and hidden["package_id"] == str(pkg)
    repo.confirm_document_reading(db, doc, "ship_date", "2026-10-14")
    assert state(parse(page(c, pkg).text), "reading-confirmed").text() == "Confirmed by you: Ship date 14 October 2026."


# ---------------------------------------------------------------------------------------------------- status chip
def test_the_chip_comes_from_the_status_vocabulary(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    first = page(c, pkg)
    assert "Draft: needs attention" in state(parse(first.text), "package-chip").text()
    ready = approved(c, db, first.cookies.get("csrf_token"))
    chip = state(parse(page(c, ready).text), "package-chip")
    assert "Approved, not sent" in chip.text() and chip.find("span", class_="tag--bad") is None


def test_a_package_with_no_attempt_has_no_check_again_form_and_attempts_keep_theirs(env):
    c, db, token, _ = env
    pkg = approved(c, db, token, "Statement A.")
    first = page(c, pkg)
    dom = parse(first.text)
    assert not forms_to(dom, "/check") and state(dom, "approved") is not None
    token = first.cookies.get("csrf_token")
    pending = approved(c, db, token, "Statement B.")
    mark_attempt(db, pending, "sent_unconfirmed")
    assert forms_to(parse(page(c, pending).text), "/check")
    sent_token = page(c, pending).cookies.get("csrf_token")
    sent = approved(c, db, sent_token, "Statement C.")
    mark_sent(db, sent)
    assert forms_to(parse(page(c, sent).text), "/check")


def test_the_banner_state_comes_from_the_stored_record_not_from_banner_words(env, monkeypatch):
    from copilot.web import routes_package
    c, db, token, _ = env
    pkg = approved(c, db, token)
    mark_sent(db, pkg)
    monkeypatch.setattr(routes_package, "send_status_banner", lambda *a, **k: "Words that match nothing")
    dom = parse(page(c, pkg).text)
    assert state(dom, "confirmed") is not None and state(dom, "prepared") is None


# ---------------------------------------------------------------------------------------------- copy
def test_non_missing_item_dispute_gets_the_scope_line_and_an_inr_dispute_does_not(env):
    c, db, token, fake = env
    pkg = new_package(c, token)
    assert state(parse(page(c, pkg).text), "not-missing-item") is None
    fake.detail_body = {**send_tests.WAITING, "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"}
    line = state(parse(page(c, pkg).text), "not-missing-item")
    assert line.text() == "This dispute is not about a missing item. Proofie builds proof of shipment only, which might not answer it."


def test_ai_unchecked_banner_copy_and_conditional_sentence(env):
    c, db, token, _ = env
    plain = repo.insert_package(db, DID, source="ai", validation_ok=False, review_flags=[])
    box = state(parse(page(c, plain).text), "ai-unchecked")
    assert "The assistant's answer could not be checked" in box.text()                 # The quoted headline
    assert "Proofie did not add the AI text to this package. Type the shipment details and add your own statements below." in box.text()
    assert "Statements marked Suggested" not in box.text()
    link = box.find("a")
    assert link.text() == "Generate again from the dispute page" and "btn-secondary" in link.attrs["class"].split()
    repo.insert_claims(db, plain, [{"evidence_index": 0, "claim_index": 0, "text": "From an image.", "source_ids": [],
                                    "basis": "model_interpretation"}])
    assert "Statements marked Suggested come from reading your images; check each one." in \
        state(parse(page(c, plain).text), "ai-unchecked").text()


def test_why_blocked_block_text_and_form(env):
    c, db, token, _ = env
    first = approved(c, db, token)
    mark_attempt(db, first, "sent_unconfirmed")
    other = new_package(c, token)
    dom = parse(page(c, other).text)
    block = dom.find("details", id="why-blocked")
    text = block.text()
    # "PayPal's Resolution Center" is a link in the step 2 sentence and the tick box (its text adds a
    # hidden new-tab note), so each sentence is pinned in its halves around the link.
    for needle in ("Sending is paused: an earlier send is not confirmed yet", "1. Press Check again.",
                   "2. Open this dispute in", "PayPal's Resolution Center (opens in a new tab)",
                   "3. If the earlier send is not there, tick the box and press Mark as not received.",
                   "I checked", "and the earlier send is not there."):
        assert needle in text, needle
    assert len([a for a in block.find_all("a") if a.text() == "PayPal's Resolution Center (opens in a new tab)"]) == 2
    form = block.find("form")
    assert form.attrs["action"].endswith(f"/packages/{other}/confirm-not-received")
    box = form.find("input", name="confirmed")
    assert box.attrs["value"] == "on" and "required" in box.attrs
    assert panel(page(c, other).text).attrs["data-next-step"] == "other_pending"


def test_unreadable_document_sentence(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    repo.insert_document(db, DID, "scan.png", "image/png", 10, "sha-s", None, False)
    box = state(parse(page(c, pkg).text), "extraction-failed")
    assert "Proofie could not read the text in scan.png. Type the details yourself, or upload a PDF with selectable text." in box.text()


def test_a_not_started_check_row_says_sending_answers_the_request(env):
    c, db, token, _ = env
    pkg = approved(c, db, token)
    dom = parse(page(c, pkg).text)
    rows = [r for r in dom.find_all("tr") if "data-check" in r.attrs]
    bad = [r for r in rows if r.attrs["data-tag-state"] == "bad"]
    assert all("Sending is the step that answers this request." not in r.text() for r in bad)
    for r in rows:
        if "Sending is the step that answers this request." in r.text():
            assert r.attrs["data-tag-state"] == "todo"


def test_exclude_action_in_a_check_row_names_the_file_and_carries_the_return_target(env):
    from copilot.web import routes_package
    rows = routes_package._check_rows([], DID, 3, {})
    assert rows == []
    c, db, token, _ = env
    pkg = new_package(c, token)
    doc = repo.insert_document(db, DID, "other.pdf", "application/pdf", 10, "sha-o", "x", True)
    from types import SimpleNamespace
    check = SimpleNamespace(id="tracking_number", label_for_seller="Tracking number", state="conflict", basis="case_request",
                            action="review_difference", sources=[f"document:{doc}"], risk=False, seller_message="x")
    rows = routes_package._check_rows([check], DID, pkg, {str(doc): {"filename": "other.pdf"}})
    exclude = [a for a in rows[0]["actions"] if a["kind"] == "exclude"][0]
    assert exclude["label"] == "Leave other.pdf out of the checks" and exclude["return_to"] == f"package:{pkg}"
