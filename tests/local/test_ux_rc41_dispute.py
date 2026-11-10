"""The dispute hub (next step, status that agrees with PayPal, tasks, documents,
packages), the outcome, claims-review and ledger pages.

Offline: fake PayPal clients, a temp database per test (conftest.py). Every assertion is on the rendered DOM or on the
rows the page read, never on the model. Nothing here calls PayPal or Ollama.
"""
import copy
import json
import secrets
from types import SimpleNamespace
from pathlib import Path

import pytest
from _dom import parse
from _hosted import authenticated_client, set_test_csrf
import _ux_lint as lint

from copilot import config
from copilot.app import create_app
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.disputes import PayPalError
from copilot.core.send import approval_fingerprint_with_attachments, build_request_input, pick_tracking
from copilot.db import repo
from copilot.web import routes_disputes
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "copilot" / "web" / "templates"
REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
REQ_INPUT = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "x"}]}
OWNED_TEMPLATES = [TEMPLATES / "dispute_detail.html",
                   *sorted((TEMPLATES / "features" / "outcome").glob("*.html")),
                   TEMPLATES / "features" / "claims" / "review.html",
                   TEMPLATES / "features" / "ledger" / "review.html"]


def _body(status=None, rels=None, **update):
    """The synthetic waiting dispute with only the named seller links (rels use PayPal's underscores)."""
    body = copy.deepcopy(WAITING)
    if rels is not None:
        body["links"] = [link for link in body["links"] if link["rel"] == "self" or link["rel"] in rels]
    if status:
        body["status"] = status
    body.update(update)
    return body


class Fake:
    def __init__(self, body, recovery=None, recovery_raises=None):
        self.body, self.recovery, self.recovery_raises = body, recovery, recovery_raises
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        assert method == "GET", "the dispute page never writes"
        if op == "show_dispute":
            return self.body
        if op == "recovery_get":
            if self.recovery_raises:
                raise self.recovery_raises
            return self.recovery if self.recovery is not None else {"evidences": []}
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _web(body=None, **kw):
    fake = Fake(body if body is not None else _body(), **kw)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    return web, fake


def _flags(monkeypatch, **flags):
    for name, value in flags.items():
        monkeypatch.setenv("COPILOT_FEATURE_" + name.upper(), "true" if value else "false")


def _page(web, query="", view=None):
    response = web.get(f"/disputes/{DID}" + (f"/workspace/{view}" if view else "") + query)
    assert response.status_code == 200
    return response.text, parse(response.text)


def _page_all(web, query="", views=("prepare",)):
    """The overview with the body of the named task views appended inside its <main>: for pins that look at content now
    spread over the overview and the Prepare / Case records views (the case strip is not repeated)."""
    text = web.get(f"/disputes/{DID}{query}").text
    extra = ""
    for view in views:
        html = web.get(f"/disputes/{DID}/workspace/{view}{query}").text
        assert html.count('<div class="case-body"') == 1
        extra += html[html.index('<div class="case-body"'):html.index("</main>")]
    text = text.replace("</main>", extra + "</main>", 1)
    return text, parse(text)


def _primaries(dom):
    return [n for n in dom.walk() if "btn-primary" in n.attrs.get("class", "").split()]


def _doc(db, name="label.pdf", ok=True):
    return repo.insert_document(db, DID, name, "application/pdf", 10, "sha-" + name, "Carrier: FedEx" if ok else None, ok)


def _package(db, claims=True, tracking=True):
    package = repo.insert_package(db, DID, "human_edit", True, [])
    if claims:
        repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "I shipped the order.",
                                          "source_ids": [], "basis": "seller_statement"}])
    if tracking:
        repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    return repo.get_package(db, package)


def _approve(db, package):
    claims = repo.list_claims(db, package["id"])
    tracking, carrier = pick_tracking(extracted_docs_for(db, DID))
    sha = approval_fingerprint_with_attachments(DID, "provide_evidence", build_request_input(claims, tracking, carrier),
                                                [a["sha256"] for a in repo.list_attachments(db, package["id"])])
    repo.insert_approval(db, package["id"], sha, "synthetic seller")


def _attempt(db, package, state, request_id="req-1", sha="sha-1", reason=None, files=(), request_input=REQ_INPUT):
    store = SQLiteAttemptStore(db)
    try:
        row = {"state": state, "request_id": request_id, "reason": reason, "attachment_filenames": list(files)}
        if request_input is not None:
            row["request_input"] = request_input
        store.write(DID, sha, row)
    finally:
        store.close()
    repo.log_action(db, "system", "send_attempt_registered", DID,
                    {"package_id": package["id"], "request_id": request_id, "approval_sha256": "appr-1",
                     "attachments": list(files)})


# --- the next step
def test_no_documents_the_upload_link_is_the_only_primary_and_precedes_the_tasks(monkeypatch):
    _flags(monkeypatch, active_approval=False)
    web, _ = _web()
    text, dom = _page_all(web, views=("prepare",))
    panel = dom.find("section", data_next_step="upload")
    assert panel is not None and text.index('data-next-step="upload"') < text.index('id="tasks"')
    primaries = _primaries(dom)
    assert [(n.tag, n.text(), n.attrs.get("href")) for n in primaries] == [("a", "Upload a document", f"/disputes/{DID}/workspace/prepare#documents")]
    # O4 put a toggletip button (where the files stay) before the upload button: the upload button is the one that submits
    upload_buttons = [b for b in dom.find("section", id="documents").find_all("button") if b.attrs.get("type") != "button" and "tip-btn" not in b.attrs["class"].split()]
    assert [b.attrs["class"].split() for b in upload_buttons] == [["btn", "btn-secondary"]]
    assert "Choose a document" in text and ".pdf,.png,.jpg,.jpeg,.gif" in text
    assert "PDF, PNG, JPEG or GIF, under 10 MB each; 50 MB in total per send." in text


def test_documents_without_a_package_the_generate_post_is_the_primary_inside_the_panel(monkeypatch):
    web, _ = _web()
    _doc(config.db_path())
    text, dom = _page(web)
    panel = dom.find("section", data_next_step="generate")
    primaries = _primaries(dom)
    assert len(primaries) == 1 and primaries[0].text() == "Generate evidence package"
    assert primaries[0].inside("section") and panel in _ancestors(primaries[0])
    form = primaries[0].parent
    assert form.tag == "form" and form.attrs["action"] == f"/disputes/{DID}/packages"


def _ancestors(node):
    out = []
    while node.parent is not None:
        node = node.parent
        out.append(node)
    return out


def test_a_package_not_yet_approved_points_to_the_approval_page(monkeypatch):
    _flags(monkeypatch, active_approval=True)
    web, _ = _web()
    package = _package(config.db_path())
    text, dom = _page(web)
    (primary,) = _primaries(dom)
    assert primary.tag == "a" and primary.text() == "Review each statement and approve"
    assert primary.attrs["href"].endswith(f"/packages/{package['id']}/active-approval")
    assert "btn-large" in primary.attrs["class"]


def test_approved_and_sendable_continues_to_the_package_and_never_posts_a_send(monkeypatch):
    web, _ = _web()
    db = config.db_path()
    package = _package(db)
    _approve(db, package)
    text, dom = _page_all(web, views=("prepare",))
    (primary,) = _primaries(dom)
    assert primary.text() == "Continue to send evidence" and primary.attrs["href"].endswith("#next-step")
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith(("/send", "/register"))]
    assert "Approved, not sent" in dom.find("section", id="package").text()
    assert "tag--bad" not in dom.find("section", id="package").find_all("table")[0].find("td", data_label="Status").text()


def test_confirmed_without_seller_links_has_no_primary(monkeypatch):
    _flags(monkeypatch, resolution=True)
    web, _ = _web(_body("WAITING_FOR_BUYER_RESPONSE", rels=()))
    db = config.db_path()
    _attempt(db, _package(db), "confirmed")
    text, dom = _page(web)
    assert dom.find("section", data_next_step="confirmed") is not None
    assert _primaries(dom) == []


def test_confirmed_with_a_send_message_link_offers_the_resolution_hub_as_the_primary(monkeypatch):
    _flags(monkeypatch, resolution=True)
    web, _ = _web(_body("WAITING_FOR_BUYER_RESPONSE", rels=("send_message",)))
    db = config.db_path()
    _attempt(db, _package(db), "confirmed")
    text, dom = _page(web)
    (primary,) = _primaries(dom)
    assert primary.text() == "Resolution options and buyer messages"
    assert primary.attrs["href"] == f"/disputes/{DID}/resolution"
    links = [a for a in dom.find_all("a") if a.text() == "Resolution options and buyer messages"]
    assert len(links) == 1                                         # never a second copy in the optional group
    assert "PayPal now waits for the buyer." in text                # once
    assert dom.text().count("Proofie confirmed your evidence in PayPal's record at") == 1


def test_settling_makes_check_again_the_primary_and_names_the_key(monkeypatch):
    web, _ = _web(_body("UNDER_REVIEW", rels=()))
    text, dom = _page(web)
    assert dom.find("section", data_next_step="settling") is not None
    assert [p.text() for p in _primaries(dom)] == ["Check again"]                   # polish: the one thing to do
    assert len([a for a in dom.find_all("a") if a.text() == "Check again"]) == 1


def test_with_a_package_generate_lives_collapsed_and_is_not_primary(monkeypatch):
    web, _ = _web()
    db = config.db_path()
    _doc(db)
    package = _package(db, claims=False, tracking=False)
    text, dom = _page_all(web, views=("prepare",))
    details = dom.find("details", data_state="new-version")
    assert details is not None and "Package v1 stays as it is." in details.text()
    button = details.find("button")
    assert button.text() == "Start package v2" and "btn-primary" not in button.attrs["class"]
    assert details.find("form").attrs["action"] == f"/disputes/{DID}/packages"
    assert len(_primaries(dom)) <= 1


@pytest.mark.parametrize("scenario", ["upload", "generate", "finish", "review", "send", "confirmed", "pending"])
def test_never_two_primaries_in_any_state(monkeypatch, scenario):
    _flags(monkeypatch, active_approval=True, resolution=True, fulfilment=True, draft=True)
    db = config.db_path()
    body = _body()
    if scenario in ("generate", "finish", "review", "send", "confirmed", "pending"):
        _doc(db)
    if scenario == "finish":
        _package(db, claims=False)
    if scenario in ("review", "send", "confirmed", "pending"):
        package = _package(db)
    if scenario == "send":
        _approve(db, package)
    if scenario == "confirmed":
        _attempt(db, package, "confirmed")
        body = _body("WAITING_FOR_BUYER_RESPONSE", rels=("send_message", "make_offer"))
    if scenario == "pending":
        _attempt(db, package, "sent_unconfirmed")
    web, _ = _web(body)
    found = _primaries(_page(web)[1])
    assert len(found) == 1 if scenario != "confirmed" else len(found) <= 1


# --- the pending block
def test_pending_a_paypal_read_and_the_evidence_is_not_there():
    db = config.db_path()
    _attempt(db, _package(db), "sent_unconfirmed")
    web, _ = _web(recovery={"evidences": []})
    text, dom = _page_all(web, views=("prepare",))
    block = dom.find("section", data_state="send-unconfirmed")
    assert block is not None and "Send attempted - PayPal has not confirmed it yet." in block.text()
    assert "Proofie will not send it again on its own. PayPal was read at" in block.text()
    _only_check_again(dom, block)


def test_pending_c_paypal_did_not_answer():
    db = config.db_path()
    _attempt(db, _package(db), "sent_unconfirmed")
    web, _ = _web(recovery_raises=PayPalError(503, "SERVICE_UNAVAILABLE", "dbg", {}))
    text, dom = _page_all(web, views=("prepare",))
    block = dom.find("section", data_state="recheck-failed")
    assert block is not None and "PayPal did not answer at" in block.text()
    assert "Proofie does not know yet whether PayPal has the evidence. Proofie will not send it again on its own." in block.text()
    _only_check_again(dom, block)


def test_pending_d_no_stored_request_links_to_the_package_steps():
    db = config.db_path()
    package = _package(db)
    _attempt(db, package, "sent_unconfirmed", request_input=None)
    web, _ = _web()
    text, dom = _page_all(web, views=("prepare",))
    block = dom.find("section", data_state="recheck-not-possible")
    assert block is not None
    assert "Proofie cannot tell whether this attempt reached PayPal. Check the dispute in PayPal's Resolution Center, then follow the steps on the package page." in block.text()
    assert f"/disputes/{DID}/packages/{package['id']}#why-blocked" in [a.attrs["href"] for a in block.find_all("a")]
    _only_check_again(dom, block)


def test_pending_b_partial_names_the_missing_file():
    db = config.db_path()
    package = _package(db)
    _attempt(db, package, "sent_unconfirmed", reason="missing_attachment:01_carrier_letter.pdf",
             files=("01_carrier_letter.pdf",))
    web, _ = _web(recovery={"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                                           "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]},
                                           "notes": "x", "date": "2026-10-05"}]})
    text, dom = _page(web)
    block = dom.find("section", data_next_step="pending")
    assert "When Proofie checked, PayPal showed your statements without the file 01_carrier_letter.pdf. Proofie will not send it again on its own." in block.text()
    assert "Statements in PayPal, file missing" in text
    assert "Done" not in dom.find("tr", data_task="send").text()


def _only_check_again(dom, block):
    links = [a for a in dom.find_all("a") if a.text() == "Check again"]
    assert [a.attrs["href"] for a in links] == [f"/disputes/{DID}"]          # one link on the whole page
    assert "Reads the dispute from PayPal. Sends nothing to PayPal." in block.text()
    assert not block.find_all("form")
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith(("/send", "/register", "/resolution/register"))]
    assert "Sent to PayPal" not in dom.find("section", id="package").text()
    # Generate stays available, collapsed, with the sentence about the blocked send
    details = dom.find("details", data_state="new-version")
    assert details is not None and "Sending stays blocked until PayPal confirms the earlier attempt" in details.text()


# --- the tasks
def _task(dom, key):
    return dom.find("tr", data_task=key)


def test_confirmed_tasks_agree_with_paypal_and_keep_the_sent_version_after_a_source_change():
    db = config.db_path()
    package = _package(db)
    _attempt(db, package, "confirmed")
    web, _ = _web(_body("WAITING_FOR_BUYER_RESPONSE", rels=()))
    text, dom = _page(web)
    send, approve = _task(dom, "send").text(), _task(dom, "approve").text()
    assert "Done: package v1 confirmed in PayPal's record at" in send
    assert "Done: approval used for the send" in approve
    assert "Not started" not in approve + send and "tag--bad" not in text
    assert "Package v1 (1 statement, 0 files), confirmed in PayPal's record at" in dom.find("div", data_state="sent").text()
    # the manual fields change after the send: the sent version stays Done
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "CHANGED-9", "confidence": "read"}})
    text2, dom2 = _page(web)
    assert "Done: package v1 confirmed" in _task(dom2, "send").text()
    assert _task(dom2, "approve").find("a").attrs["href"].endswith(f"/packages/{package['id']}")


def test_a_pending_send_reads_approved_outcome_pending_and_never_done():
    db = config.db_path()
    _attempt(db, _package(db), "sent_unconfirmed")
    web, _ = _web(recovery={"evidences": []})
    text, dom = _page(web)
    assert "Approved; outcome pending" in _task(dom, "approve").text()
    assert "Send attempted - not confirmed by PayPal" in _task(dom, "send").text()
    assert "Done" not in _task(dom, "send").text() + _task(dom, "approve").text()


def test_an_uploaded_document_that_was_not_in_the_sent_package_is_said_plainly():
    """The walk (c73175a4) sent a package with 0 attachments while a document had been uploaded: the page must say the
    document was not part of what was sent (the request had attachments: [])."""
    db = config.db_path()
    package = _package(db)
    _attempt(db, package, "confirmed")
    _doc(db, "03_in_transit.pdf")
    web, _ = _web(_body("WAITING_FOR_BUYER_RESPONSE", rels=()))
    text, dom = _page(web)
    sent = dom.find("div", data_state="sent").text()
    assert "Package v1 (1 statement, 0 files)" in sent
    assert "1 uploaded document was not part of this package." in sent
    assert "What was sent:" not in sent


def test_a_newer_unsent_version_is_shown_separately_as_not_sent():
    db = config.db_path()
    first = _package(db)
    _attempt(db, first, "confirmed")
    repo.insert_package(db, DID, "human_edit", True, [])
    web, _ = _web(_body("WAITING_FOR_BUYER_RESPONSE", rels=()))
    text, dom = _page(web)
    sent = dom.find("div", data_state="sent").text()
    assert "Package v2 (newer): Not sent. A confirmation covers only the content sent then." in sent
    assert "Package v1" in sent and "confirmed in PayPal's record" in sent


def test_the_sent_version_reopens_only_when_paypal_lists_provide_evidence_again():
    db = config.db_path()
    package = _package(db)
    _attempt(db, package, "confirmed")
    web, _ = _web(_body(rels=("provide_evidence",)))
    text, dom = _page(web)
    # v1's send row stays done for the sent version; the open rows are about a NEW version
    assert "Done: package v1 confirmed" in _task(dom, "send").text()
    assert "A new version needs its own send." in _task(dom, "send").text()
    assert "approved" not in _task(dom, "approve").text() and "Cannot start yet" in _task(dom, "approve").text()
    assert "Not started: a new version after v1" in _task(dom, "prepare").text()


def test_blocked_rows_are_text_with_an_inline_link_to_the_blocker():
    web, _ = _web()
    text, dom = _page(web)
    approve = _task(dom, "approve")
    assert approve.find("a", class_="task-link") is None
    assert "Cannot start yet. First" in approve.text()
    assert [a.attrs["href"] for a in approve.find_all("a")] == [f"/disputes/{DID}/workspace/prepare#package"]
    upload = _task(dom, "upload")
    assert upload.find("a", class_="task-link").attrs["href"] == f"/disputes/{DID}/workspace/prepare#documents"
    assert "Not started" in upload.text() and "Optional" not in upload.text()
    assert len(dom.find_all("tr", class_="task-row")) == 4
    assert "of 6" not in text and "Step 1" not in text


def test_the_guided_names_come_from_presentation():
    from copilot.web.presentation import GUIDED_STEPS
    web, _ = _web()
    dom = _page(web)[1]
    got = [li.find("div", class_="task-main").text() for li in dom.find_all("li", class_="task-row")]
    for name, line in zip(GUIDED_STEPS, got):
        assert line.startswith(name)


# --- packages table
def test_package_status_words_per_state():
    db = config.db_path()
    draft = _package(db, claims=False, tracking=False)
    web, _ = _web()
    text, dom = _page_all(web, views=("prepare",))
    cell = dom.find("section", id="package").find("td", data_label="Status")
    assert "Draft: needs attention" in cell.text() and cell.find("span", class_="tag--bad") is not None
    assert dom.find("section", id="package").find("a", href=f"/disputes/{DID}/packages/{draft['id']}").text() == "Open package v1"
    assert "checks verified;" not in cell.text()          # v1.1.2: the verdict sentence, never the old "n of m" form


def _counted(monkeypatch, states):
    from copilot.web import routes_package
    monkeypatch.setattr(routes_package, "_checks_for",
                        lambda *a, **k: ([SimpleNamespace(state=s) for s in states], None))


@pytest.mark.parametrize("states, line", [
    (["verified"], "1 check verified."),
    (["verified", "present_unverifiable", "missing", "not_applicable"], "1 check verified. 1 missing. 1 cannot be checked."),
])
def test_the_package_row_counts_checks_like_the_package_page(monkeypatch, states, line):
    _package(config.db_path())
    _counted(monkeypatch, states)
    web, _ = _web()
    cell = _page_all(web, views=("prepare",))[1].find("section", id="package").find("td", data_label="Status")
    assert line in " ".join(cell.text().split())


def test_no_count_sentence_when_no_check_applies(monkeypatch):
    _package(config.db_path())
    _counted(monkeypatch, ["not_applicable"])
    web, _ = _web()
    cell = _page_all(web, views=("prepare",))[1].find("section", id="package").find("td", data_label="Status")
    assert "verified;" not in cell.text() and "0 of 0" not in cell.text()


def test_a_confirmed_package_row_says_sent_to_paypal():
    db = config.db_path()
    _attempt(db, _package(db), "confirmed")
    web, _ = _web(_body("WAITING_FOR_BUYER_RESPONSE", rels=()))
    text, dom = _page_all(web, views=("prepare",))
    cell = dom.find("section", id="package").find("td", data_label="Status")
    assert "Sent to PayPal" in cell.text() and cell.find("span", class_="tag--bad") is None


def test_a_complete_draft_is_a_plain_draft_not_red():
    db = config.db_path()
    _package(db)
    web, _ = _web()
    cell = _page_all(web, views=("prepare",))[1].find("section", id="package").find("td", data_label="Status")
    assert "Draft" in cell.text() and "needs attention" not in cell.text() and cell.find("span", class_="tag--bad") is None


# --- the mismatch note
def _info(state):
    record = SimpleNamespace(state=state, attachments_match=True, attachment_filenames=(), confirmed_at="2026-10-16T22:10:00-06:00", attempt=None)
    return {"record": record, "claims": [1], "approval_valid": False}


def test_the_checked_after_sending_note_needs_the_latest_package_to_be_confirmed():
    norm = {"dispute_id": DID, "can_provide_evidence": True}
    packages = [{"id": 1, "version": 1}, {"id": 2, "version": 2}]
    conflict = [SimpleNamespace(state="conflict")]
    v1_confirmed_v2_not = routes_disputes._detail_summary(norm, [], packages, [], checks=conflict,
                                                          infos={1: _info("confirmed"), 2: _info("none")})
    assert "Checked after sending" not in v1_confirmed_v2_not["mismatch_note"]
    assert v1_confirmed_v2_not["mismatch_note"] == "1 difference needs review on the package page."
    latest_confirmed = routes_disputes._detail_summary(norm, [], packages[:1], [], checks=conflict, infos={1: _info("confirmed")})
    assert latest_confirmed["mismatch_note"].startswith("Checked after sending: 1 difference")
    assert latest_confirmed["drift"] == "PayPal facts changed after you sent: 1 difference. Nothing to do unless you prepare a new package."
    asking_nothing = routes_disputes._detail_summary({**norm, "can_provide_evidence": False}, [], packages[:1], [],
                                                     checks=conflict, infos={1: _info("confirmed")})
    assert asking_nothing["drift"] == "PayPal facts changed after you sent: 1 difference. PayPal is not asking for evidence now."


def test_the_checked_after_sending_note_also_holds_for_a_later_file_latest_package():
    """A confirmed send with a file added afterwards (state later_file) is still a finished send."""
    norm = {"dispute_id": DID, "can_provide_evidence": True}
    conflict = [SimpleNamespace(state="conflict")]
    later = routes_disputes._detail_summary(norm, [], [{"id": 1, "version": 1}], [], checks=conflict,
                                            infos={1: _info("later_file")})
    assert later["mismatch_note"].startswith("Checked after sending: 1 difference")
    assert later["drift"] == "PayPal facts changed after you sent: 1 difference. Nothing to do unless you prepare a new package."
    only_earlier = routes_disputes._detail_summary(norm, [], [{"id": 1, "version": 1}, {"id": 2, "version": 2}], [],
                                                   checks=conflict, infos={1: _info("later_file"), 2: _info("none")})
    assert "Checked after sending" not in only_earlier["mismatch_note"] and only_earlier["drift"] is None


def test_the_dispute_page_shows_the_checked_after_sending_note_for_a_later_file_package(monkeypatch):
    """Rendered: package v1 confirmed, a file added afterwards, a conflicting check."""
    from copilot.web import routes_package
    db = config.db_path()
    package = _package(db)
    _attempt(db, package, "confirmed")                                  # the send carried no file
    repo.attach_document(db, package["id"], _doc(db, "added_later.pdf"))
    monkeypatch.setattr(routes_package, "_checks_for", lambda *args, **kwargs: ([SimpleNamespace(state="conflict")], None))
    text, dom = _page(_web(_body("WAITING_FOR_BUYER_RESPONSE"))[0])
    assert dom.find("p", data_state="mismatch-unchecked").text().startswith("Checked after sending: 1 difference")
    assert dom.find("p", data_state="drift").text() == ("PayPal facts changed after you sent: 1 difference. Nothing to do "
                                                          "unless you prepare a new package.")


# --- the done banner
def test_done_banners_only_for_this_disputes_own_documents_in_the_matching_state():
    db = config.db_path()
    excluded = _doc(db, "left_out.pdf")
    repo.set_document_excluded(db, excluded, True)
    kept = _doc(db, "kept.pdf")
    web, _ = _web()
    text, dom = _page(web, f"?done=document_excluded&doc={excluded}", view="prepare")
    banners = dom.find_all("div", data_state="done")
    assert len(banners) == 1 and banners[0].attrs["data-done"] == "document_excluded"
    assert "left_out.pdf is left out of the checks." in banners[0].text()
    assert dom.find("section", id="documents").find("div", data_state="done") is not None
    for query in (f"?done=document_excluded&doc={kept}", f"?done=document_included&doc={excluded}",
                  "?done=document_excluded&doc=9999", "?done=bogus&doc=1", f"?done=document_added&doc=abc",
                  f"?done=document_added&doc={excluded}", "?done=document_added"):
        assert _page(web, query, view="prepare")[1].find("div", data_state="done") is None, query
    assert _page(web, f"?done=document_included&doc={kept}", view="prepare")[1].find("div", data_state="done") is not None
    assert _page(web, view="prepare")[1].find("div", data_state="done") is None


# ------------------------------------------------------------------------------- shell rollout and page frame
def test_frame_title_h1_breadcrumb_and_no_back_links(monkeypatch):
    web, _ = _web()
    text, dom = _page(web)
    assert lint.breadcrumb_items(text) == [("All disputes", "/disputes"), (f"Dispute {DID}", None)]
    assert lint.back_links(text) == [] and lint.bare_buttons(text) == []
    import re
    assert re.search(r"<title>Dispute PP-.+: .+ - Proofie</title>", text)
    h1 = dom.find("h1")
    assert h1.text().startswith("Item not received: 25.00 USD")
    span = h1.find("span", class_="h1-id")
    assert span.attrs["translate"] == "no" and span.text() == f"Dispute {DID}"
    assert text.count("Fields marked Required") == 0 and "of 6" not in text
    assert lint.large_labels_ok(text)


def test_case_strip_and_single_statements():
    web, _ = _web()
    text, dom = _page(web)
    assert text.count("PayPal says the dispute is") == 0
    assert text.count("PayPal was read at") == 1 and "From PayPal, read" in _page(web, view="send")[0]
    facts = dom.find("dl", class_="facts").text()
    assert "Respond by" in facts and ("days left" in facts or "Due today" in facts) or "due date passed" in facts   # the fixture's fixed due date moves through these phrasings as the calendar passes it
    assert text.count("If you do nothing, Proofie sends nothing.") == 1 and "10 days of the dispute opening" in text
    assert "PayPal's decision: none yet" in _page(web, view="records")[0]
    no_due = _body()
    no_due.pop("seller_response_due_date")
    assert "No verified deadline available" in _page(_web(no_due)[0])[1].find("dl", class_="facts").text()


def test_the_non_inr_notice_follows_the_h1_with_the_new_copy(monkeypatch):
    web, _ = _web(_body(reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"))
    text, dom = _page(web)
    notice = dom.find("p", data_state="shipment-proof-only")
    assert notice.text() == ("This dispute is not about a missing item. Proofie builds proof of shipment only, which might not "
                             "answer it. Answer this dispute in PayPal, or check every statement carefully.")
    assert text.index("<h1") < text.index('data-state="case-strip"') < text.index('data-state="shipment-proof-only"')
    assert 'data-state="shipment-proof-only"' not in _page(_web()[0])[0]


def test_optional_group_links_and_gating(monkeypatch):
    _flags(monkeypatch, resolution=True, draft=True, fulfilment=True, triage=True, claims=True)
    web, _ = _web()
    text, dom = _page(web, view="other")
    group = dom.find("details", id="other-responses-list")
    assert group.find("summary").text() == "Other possible responses" and "open" in group.attrs
    hub = [a for a in group.find_all("a") if a.text() == "Resolution options and buyer messages"][0]
    assert hub.attrs["href"] == f"/disputes/{DID}/resolution"
    tracking = [a for a in group.find_all("a") if a.text() == "Add tracking in PayPal"][0]
    assert tracking.attrs["href"] == f"/fulfilment/DEMOTXN0000000001?from_dispute={DID}"      # the form learns which dispute the seller came from
    assert "Offer a refund or write to the buyer. You see the exact text before anything is sent. A message does not send your evidence." in group.text()
    assert "Adds the tracking number to the sale in PayPal after you approve it. It does not answer the dispute or change your evidence package." in group.text()
    assert group.find("a", href=f"/disputes/{DID}/draft").text() == "Draft a message to the buyer"
    assert not group.find_all("form")
    closed = _body("RESOLVED", rels=(), dispute_outcome={"outcome_code": "CANCELED_BY_BUYER"})
    _flags(monkeypatch, outcome=True)
    assert _page(_web(closed)[0])[1].find("a", href=f"/disputes/{DID}/resolution") is None


# -------------------------------------------------------------------------------------------- documents section
def test_leave_out_and_use_again_buttons_carry_the_file_name_and_vanish_for_a_sent_file():
    db = config.db_path()
    own = _doc(db, "sent_file.pdf")
    other = _doc(db, "other.pdf")
    package = _package(db)
    repo.attach_document(db, package["id"], own)
    _attempt(db, package, "confirmed", files=("sent_file.pdf",))
    web, _ = _web(_body("WAITING_FOR_BUYER_RESPONSE", rels=()))
    text, dom = _page_all(web, views=("prepare",))
    section = dom.find("section", id="documents")
    buttons = [b for b in section.find_all("button")]
    assert [b.text() for b in buttons if "checks" in b.text()] == ["Leave out of the checks other.pdf"]
    assert all(b.find("span", class_="visually-hidden") for b in buttons if "checks" in b.text())
    assert "Sent to PayPal, confirmed" in section.text()
    repo.set_document_excluded(db, other, True)
    text2, dom2 = _page_all(web, views=("prepare",))
    assert "Use in the checks again other.pdf" in dom2.find("section", id="documents").text()
    assert "Text extracted" in text2


def test_unreadable_document_cell_and_summary_line():
    _doc(config.db_path(), "scan.png", ok=False)
    text, dom = _page(_web()[0], view="prepare")
    assert "No. Type the details yourself in the package." in dom.find("section", id="documents").text()
    assert "Proofie could not read scan.png. Type the details yourself in the package." in dom.find("ul", data_state="unverifiable").text()


def test_the_timeline_is_one_collapsed_details_after_documents_and_package(monkeypatch):
    _flags(monkeypatch, ledger=True)
    db = config.db_path()
    repo.log_action(db, "vendor", "upload_document", DID, {"document_id": 1})
    text, dom = _page(_web()[0], view="records")
    details = dom.find("details", data_state="timeline")
    assert details is not None and details.find("ol") is not None
    assert details.find("summary").text() == "Action timeline (1 events)"
    assert details.find("a", href=f"/disputes/{DID}/ledger").text() == "What this app recorded"
    assert dom.find("section", id="summary") is None and 'id="documents"' not in text


def test_package_hint_is_static_per_mode(web_mode):
    text, dom = _page(_web()[0], view="prepare")
    hint = dom.find("p", data_state="package-hint").text()
    if web_mode == "hosted":
        assert hint == ("The AI assistant is off in this demo. You type the shipment facts and statements yourself. "
                        "Nothing is sent.")                                           # The hint is true
    else:
        assert hint == "The local AI model writes first statements from your documents. You check every one. Nothing is sent."


def test_owned_templates_pass_the_shell_lint():
    assert lint.template_lint(OWNED_TEMPLATES) == []


# ------------------------------------------------------------------------- claims review and ledger
def _render(template, **context):
    return routes_disputes.templates.env.get_template(template).render(context)


def test_claims_review_page_names_its_purpose_and_offers_the_forward_step(monkeypatch):
    _flags(monkeypatch, active_approval=True)
    claim = SimpleNamespace(text="Synthetic seller statement.", verdict="seller_statement", claim_basis="seller_statement",
                            bases=["none"], sources=[], key="demo", id=1)
    html = _render("features/claims/review.html", dispute_id=DID, package_id=7, package_version=2, claims=[claim],
                   safe_text=lambda v: v, document_names={})
    dom = parse(html)
    assert "See where each statement comes from" in dom.find("h1").text() and "<title>See where each statement comes from" in html
    assert "This page only shows sources. It does not record a review." in dom.text()
    link = [a for a in dom.find_all("a") if a.text() == "Review each statement and approve"][0]
    assert link.attrs["href"] == f"/disputes/{DID}/packages/7/active-approval" and "btn-large" in link.attrs["class"]
    assert "btn-primary" in link.attrs["class"] and "Your own statement. No document is cited." in dom.text()   # polish: the forward step is the primary
    assert lint.breadcrumb_items(html) == [("All disputes", "/disputes"), (f"Dispute {DID}", f"/disputes/{DID}"),
                                          ("Evidence package v2", f"/disputes/{DID}/packages/7"), ("Statement sources", None)]
    assert lint.back_links(html) == [] and " we " not in dom.text().lower()
    _flags(monkeypatch, active_approval=False)
    off = _render("features/claims/review.html", dispute_id=DID, package_id=7, package_version=2, claims=[claim],
                  safe_text=lambda v: v, document_names={})
    assert f'href="/disputes/{DID}/packages/7#approval"' in off
    model = SimpleNamespace(text="x", verdict="unverifiable", claim_basis="model_interpretation", bases=["model_interpretation"],
                            sources=[], key="m", id=2)
    assert "AI reading (local model)" in _render("features/claims/review.html", dispute_id=DID, package_id=7,
                                                 package_version=2, claims=[model], safe_text=lambda v: v, document_names={})


def test_ledger_title_equals_h1_and_actor_words_follow_the_origin():
    records = [{"label": "Approved a package", "timestamp": "2026-10-16T22:10:00-06:00", "actor": "vendor", "origin": "approvals", "metadata": {}},
               {"label": "Recorded app action", "timestamp": "2026-10-16T22:11:00-06:00", "actor": "system", "origin": "audit_log", "metadata": {}},
               {"label": "Recorded app action", "timestamp": "2026-10-16T22:12:00-06:00", "actor": "system", "origin": "audit_log", "metadata": {"via": "mcp"}},
               {"label": "Read", "timestamp": None, "actor": "paypal", "origin": "send_attempts", "metadata": {}}]
    html = _render("features/ledger/review.html", dispute_id=DID, data={"records": records, "limitations": []})
    dom = parse(html)
    assert dom.find("h1").text() == "What this app recorded" and "<title>What this app recorded: " in html
    words = [span.text() for span in dom.find_all("span") if "data-actor" in span.attrs]
    assert words == ["By you", "By Proofie", "Requested by a connected AI assistant (MCP)", "Read from PayPal"]
    assert "An approval is not a receipt from PayPal." in html
    assert lint.breadcrumb_items(html)[-1] == ("What this app recorded", None) and lint.back_links(html) == []

