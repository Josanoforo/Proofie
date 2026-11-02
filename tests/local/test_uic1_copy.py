"""The first-run copy in the templates, verbatim, in the right state.
Offline: a fake client replays synthetic sandbox-shaped bodies (pilot/fixtures/synthetic)."""
import pytest
from _hosted import authenticated_client, reset_csrf
from _dom import parse
import copy
import html
import json
import re
import secrets
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.app import create_app
from copilot.core.checklist_input import MANUAL_FIELD_NAMES
from copilot.db import repo
from copilot.web.routes_disputes import get_client
from copilot.web.security import CSRF_COOKIE_NAME

REC = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
UNDER_REVIEW = json.loads((REC / "dispute_PP-R-DEMO-0000001_under_review.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE1 = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
INR = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"


class Fake:
    def __init__(self, list_body=None, detail=None):
        self.list_body, self.detail = list_body, detail

    def call(self, method, path, op, **kw):
        if op == "list_disputes":
            return self.list_body
        if op == "show_dispute":
            return self.detail
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _web(fake):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return c, token


def _text(r):
    return html.unescape(r.text)


def _with_reason(body, reason):
    b = copy.deepcopy(body)
    b["reason"] = reason
    for t in b.get("disputed_transactions", []):
        t["reason"] = reason
    return b


# ---- first-run sentence -----------------------------------------------------------------------------------------------------
C1 = ('New here? Proofie reads the disputes of your own PayPal sandbox account. To try it, create an "item not received" '
      'dispute by hand in the sandbox: the step-by-step guide is in the repository, docs/JUDGE-RUN.md, step 3. '
      'A new dispute can take a few minutes to appear here.')


def test_c1_empty_inbox_has_the_guide_paragraph_between_the_first_paragraph_and_the_button():
    c, _ = _web(Fake(list_body={"items": []}))
    t = re.sub(r"<[^>]+>", "", _text(c.get("/disputes")))
    t = re.sub(r"\s+", " ", t)
    assert "No disputes need your attention right now" in t and C1 in t
    assert t.index("Nothing was changed.") < t.index(C1) < t.index("Check PayPal again")


def test_c1_the_paragraph_is_absent_when_there_are_disputes():
    c, _ = _web(Fake(list_body=LIST_PAGE1, detail=WAITING))
    assert "New here?" not in c.get("/disputes").text


# ---- waiting dispute without a draft -----------------------------------------------------------------------------------------------------
def test_p4_waiting_dispute_without_a_draft_says_open_the_dispute_and_keeps_the_state_attribute(monkeypatch):
    monkeypatch.setattr(config, "AUTOPREP_EXCLUDE_IDS", frozenset({DID}))            # no background prep for it
    c, _ = _web(Fake(list_body=LIST_PAGE1, detail=WAITING))
    r = c.get("/disputes")
    dom = parse(r.text)
    cell = dom.find("td", data_label="Next step")
    assert cell.attrs["data-draft-state"] == "none"                     # the state hook stays; the words and button are new
    assert cell.find("a", class_="btn").text().startswith("Respond to this dispute")
    assert "No draft yet" not in r.text and "Nothing to do now" not in cell.text()


def test_p4_any_other_status_says_nothing_to_do_now():
    other = copy.deepcopy(LIST_PAGE1)
    other["items"][0]["status"] = "UNDER_REVIEW"
    c, _ = _web(Fake(list_body=other, detail=UNDER_REVIEW))
    r = c.get("/disputes")
    cell = parse(r.text).find("td", data_label="Next step")
    assert cell.attrs["data-draft-state"] == "none"
    # UNDER_REVIEW with no action link is PayPal still updating the dispute (status vocabulary): words, no button
    assert "PayPal is updating this dispute" in cell.text() and cell.find("a", class_="btn") is None
    assert "No draft yet" not in r.text


# ---- on the dispute page ---------------------------------------------------------------------------
def test_c10_steps_and_renamed_headings_when_paypal_asks_for_evidence():
    c,_=_web(Fake(detail=WAITING));r=c.get(f"/disputes/{DID}")
    assert r.status_code == 200
    dom=parse(r.text);tasks=dom.find("table",class_="task-table")
    assert tasks is not None and len(tasks.find_all("tr", class_="task-row")) == 4          # Four guided steps
    for name in ("Upload documents","Prepare evidence package","Review each statement and approve","Send evidence to PayPal"):
        assert name in tasks.text()
    assert "of 6" not in r.text and "1. Documents" not in r.text
    # O4 (approved B composition): the overview opens on "Case status and next step"; the task table stays one tap away in the closed block
    assert 'id="tasks"' in r.text and 'data-state="summary"' in r.text and 'id="documents"' not in r.text
    prep=c.get(f"/disputes/{DID}/workspace/prepare").text
    assert prep.index('id="documents"') < prep.index('id="package"')
    pdom=parse(prep)
    assert pdom.find("h2", id="documents-title").text() == "Documents" and pdom.find("h2", id="package-title").text() == "Evidence package"


def test_c10_no_steps_where_paypal_is_not_asking_for_evidence():
    c, _ = _web(Fake(detail=UNDER_REVIEW))
    r = c.get(f"/disputes/{DID}")
    assert 'class="steps"' not in r.text and 'data-next-step="settling"' in r.text   # PayPal is updating the dispute, no steps


def test_c13_banner_absent_for_item_not_received_and_present_for_another_reason():
    assert WAITING["reason"] == INR
    c, _ = _web(Fake(detail=WAITING))
    r = c.get(f"/disputes/{DID}")
    assert "This dispute is not about a missing item" not in r.text and "shipment-proof-only" not in r.text
    c, _ = _web(Fake(detail=_with_reason(WAITING, "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")))
    r = c.get(f"/disputes/{DID}")
    t = _text(r)
    assert 'data-state="shipment-proof-only"' in r.text
    assert ("This dispute is not about a missing item. Proofie builds proof of shipment only, which might not answer it. "
            "Answer this dispute in PayPal, or check every statement carefully.") in t
    assert t.index("Dispute " + DID) < t.index("This dispute is not about a missing item") < t.index("Case status and next step")      # after the h1


# ---- package page: the hints and the step note -----------------------------------------------------------------------------
def _package_page(manual=None, approved=False, claim=None):
    c, token = _web(Fake(detail=WAITING))
    db = config.db_path()
    repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    pid = repo.insert_package(db, DID, source="human_edit", validation_ok=False, review_flags=[])
    if manual:
        repo.set_manual_fields(db, DID, {k: {"value": v, "confidence": "read"} for k, v in manual.items()})
    if claim:
        repo.insert_claims(db, pid, [{"evidence_index": 0, "claim_index": 0, "text": claim, "source_ids": [],
                                      "basis": "seller_statement"}])
    return c, c.get(f"/disputes/{DID}/packages/{pid}")


def test_step_note_under_the_title():
    _, r = _package_page()
    assert r.status_code == 200 and 'class="step-note"' in r.text
    assert "Step 2 of 4: Prepare evidence package" in r.text            # The guided four-step caption
    assert r.text.index("<h1") < r.text.index("step-note")      # O4: the h1 carries an id for the return link


def test_c3_heading_intro_and_two_groups():
    _,r=_package_page();t=_text(r)
    assert '<h2 id="shipment-details">Shipment details</h2>' in r.text and "Shipment details you can type in" not in r.text
    from copilot import config
    required = "tracking number and carrier" if config.feature_enabled("carrier_required") else "tracking number"
    # One intro paragraph; the release composition (C-D) has CARRIER_REQUIRED on, so the carrier is named too
    assert (f"Required to send: {required}. Needed: fields that turn a check red until you complete them. "
            "Everything else is optional. You can save incomplete details.") in t
    assert "Your entries are labelled as yours, not PayPal's." in t
    assert "Used by the checks below whenever a document does not already cover them." not in t
    assert "<summary>Optional details that help the checks</summary>" in r.text
    group,details=r.text.split('<details data-state="optional-details"')
    inside=details.split("</details>")[0]
    for name in ("tracking_number","carrier","delivery_status","postal_code","city","state"):
        assert f'id="field-{name}"' in group and f'id="field-{name}"' not in inside
    assert 'id="field-delivery_date"' in group and 'id="field-delivery_date"' not in inside
    for name in ("ship_date","street","receiver_name","delivery_type","capture_date"):
        assert f'name="{name}"' in inside
    assert len(MANUAL_FIELD_NAMES)==13 and set(MANUAL_FIELD_NAMES[:2])=={"tracking_number","carrier"}


def test_c3_details_is_closed_unless_an_optional_field_has_a_value():
    # Existing optional values stay visible; empty optional controls remain folded. Required fields keep their own group.
    for manual in ({},{"city":"Monterrey"},{"tracking_number":"1Z999"}):
        _,page=_package_page(manual=manual)
        assert re.search(r'<details data-state="optional-details">',page.text)
    _,page=_package_page(manual={"ship_date":"2026-10-20"})
    assert re.search(r'<details data-state="optional-details" open>',page.text)
    # Needed city is no longer part of the optional group.
    assert page.text.index('id="field-city"') < page.text.index('data-state="optional-details"')       # a required-group value does not open it


def test_c3_the_required_marker_is_on_the_tracking_number_only_and_in_text(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_CARRIER_REQUIRED","true")
    _,r=_package_page();dom=parse(r.text)
    for name in ("tracking_number","carrier"):
        label=dom.find("label",**{"for":f"field-{name}"})
        assert label is not None and "Required to send" in label.text()
    assert r.text.index('id="field-carrier"') < r.text.index('id="field-tracking_number"') < r.text.index('id="field-delivery_status"')   # Carrier first
    for name in ("street","receiver_name","delivery_type"):
        assert "Required" not in dom.find("label",**{"for":f"field-{name}"}).text()


SHIPPED_HINTS = {
    "tracking_number": "The code on the carrier's receipt or tracking page, usually letters and digits. It is not the company name. Spaces are removed for you.",
    "carrier": "The company that delivers the parcel, for example FedEx, DHL, Estafeta or UPS.",  
    "delivery_status": 'For example "delivered". "In transit" is shown as in transit, is not shown as delivered.',
    "delivery_date": "The delivery date shown on the carrier's proof.",
    "ship_date": "The date the carrier picked up the parcel.",
    "postal_code": "The postal code of the delivery address.",
    "city": "The city of the delivery address.",
    "state": "The state of the delivery address.",
    "street": "Only needed if your document shows a street address.",
    "receiver_name": "Who received the package. For information only.",
}
REWRITTEN = ("signature", "delivery_type", "capture_date")      # held back in the first copy pass (each contradicted a check); final hints are tested in test_uic2_copy.py


def test_c3_every_shipped_hint_is_verbatim_with_aria_describedby_and_the_three_old_contradicting_hints_stay_gone():
    _,r=_package_page();t=_text(r);dom=parse(r.text)
    for name,hint in SHIPPED_HINTS.items():
        assert f'id="hint-{name}"' in r.text and hint in t
        names=[name]
        for control in names:
            field=dom.find("input",name=control) or dom.find("select",name=control)
            assert field is not None and f"hint-{name}" in field.attrs.get("aria-describedby","").split()
    for name in REWRITTEN:assert f'id="hint-{name}"' in r.text
    assert set(SHIPPED_HINTS)|set(REWRITTEN)==set(MANUAL_FIELD_NAMES)
    for old in ("puts your Seller Protection at risk.","The policy asks for it only above its limit.","The date on the delivery proof itself."):
        assert old not in t


def test_c3_labels_in_sentence_case_follow_the_spec():
    _,r=_package_page();dom=parse(r.text)
    labels={"delivery_status":"Delivery status","delivery_date":"Delivery date","ship_date":"Ship date","postal_code":"Postal code","city":"City","state":"State","street":"Street","receiver_name":"Receiver name","signature":"Signature","delivery_type":"Delivery type","capture_date":"Date the proof was captured","carrier":"Carrier"}
    for name,label in labels.items():
        assert dom.find("label",**{"for":f"field-{name}"}).text().startswith(label),name
        if name in ("delivery_date","ship_date","capture_date"):
            assert dom.find("input",id=f"field-{name}").attrs["type"] == "date"


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c5_the_approve_and_send_notes_sit_next_to_their_buttons():
    # The approve form is the next step only once the package has a tracking number and a statement.
    c, r = _package_page(manual={"tracking_number": "DEMO-1", "carrier": "FEDEX"}, claim="We shipped it.")
    approve = ('Approving does not send anything. Sending is a separate step: nothing reaches PayPal until you press '
               '"Send evidence to PayPal".')
    t = _text(r)
    assert approve in t and 'data-state="approve-note"' in r.text
    assert r.text.index("approve-note") < r.text.index("Approve this package")
    form = r.text[r.text.index('/approve"'):]
    assert "approve-note" in form[:form.index("</form>")]                      # inside the approve form
    assert "This sends the approved content" not in t                           # not approved yet: no send form


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c5_the_send_note_appears_above_the_send_button_when_there_is_something_to_send(tmp_path, monkeypatch):
    from test_routes_send import _approved_package_with_tracking, _client as send_client
    c, db_path, token, _fake = send_client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    t = _text(r)
    note = "Sends package v1 (0 statements, 0 files) to PayPal sandbox once. Proofie cannot withdraw it."
    assert note in t and 'data-state="send-note"' in r.text
    assert r.text.index("Send evidence to PayPal") < r.text.index("send-note")  # the note sits directly under the button


# ---- The negative assertions (notes appear only in their own state) ---------------------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c5_send_note_is_absent_when_approved_but_there_is_no_tracking_number(tmp_path, monkeypatch):
    from test_routes_send import _client as send_client
    c, db_path, token, _fake = send_client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
               data={"csrf_token": token, "sufficiency_override": "No tracking yet, approving anyway."},
               follow_redirects=False)
    assert r.status_code == 303
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert 'data-state="send-needs-tracking"' in page.text            # the approved-but-nothing-to-send branch
    assert 'data-state="send-note"' not in page.text and "This sends the approved content" not in _text(page)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c5_approve_note_is_absent_once_the_package_is_approved(tmp_path, monkeypatch):
    from test_routes_send import _approved_package_with_tracking, _client as send_client
    c, db_path, token, _fake = send_client(tmp_path, monkeypatch)
    pkg_id = _approved_package_with_tracking(c, db_path, token)
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert 'data-state="approved"' in page.text
    assert 'data-state="approve-note"' not in page.text and "Approving does not send anything" not in _text(page)


def test_step_note_is_absent_on_the_package_not_found_page():
    c, _ = _web(Fake(detail=WAITING))
    page = c.get(f"/disputes/{DID}/packages/999999")
    assert "package-not-found" in page.text
    assert "step-note" not in page.text and "Step 3 of 3" not in page.text


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf
