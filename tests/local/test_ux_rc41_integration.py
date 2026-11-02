"""Behaviour that only exists once the seven flow branches meet.

Offline only: fake PayPal clients, a temp database per test (conftest.py), checks on the DOM and on the stored effect
(rows, audit, attempts), never only on words. Each test names the integration item it covers.
"""
import pytest
from _dom import parse, state

from copilot.db import repo
from copilot.web import routes_package, status

import test_routes_send as send_tests

DID = send_tests.DID

pytestmark = [pytest.mark.usefixtures("web_mode")]


@pytest.fixture
def flags_off(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)


# ------------------------------------------------------------------------------------------------ item b
@pytest.mark.usefixtures("flags_off")
def test_check_with_nothing_pending_takes_its_kind_from_the_stored_send_record(tmp_path, monkeypatch):
    """routes_send.check_package used the legacy text mapper (send_state_for); the result box now takes its kind from
    the package's stored send record, the same mapping the package page uses (send_state_for_record)."""
    c, db, token, fake = send_tests._client(tmp_path, monkeypatch, post_send_get_bodies=[send_tests._confirmed_body()])
    pkg = send_tests._approved_package_with_tracking(c, db, token)
    c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token})
    assert len(fake.provide_evidence_calls) == 1
    # a statement added past the route guard (as a legacy row would be): the current content no longer matches the
    # sent one, but the package's own record stays confirmed, so the check answers from it
    repo.insert_claims(db, pkg, [{"evidence_index": 0, "claim_index": 99, "text": "Another statement.",
                                  "source_ids": [], "basis": "seller_statement"}])
    record = status.send_record(db, DID, repo.get_package(db, pkg))
    assert record.state == "confirmed" and record.content_matches is False
    expected = routes_package.send_state_for_record(record, repo.get_latest_valid_approval(db, pkg) is not None)
    posts_before = len(fake.provide_evidence_calls)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/check", data={"csrf_token": token})
    box = state(parse(r.text), "confirmed")
    assert box is not None and f"banner-{expected['kind']}" in box.attrs["class"], box.attrs["class"]
    assert len(fake.provide_evidence_calls) == posts_before                 # GET only: nothing re-sent


# ------------------------------------------------------------------------------------------------ item c
def _norm(links):
    return {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE", "stage": "INQUIRY", "action_links": links,
            "can_provide_evidence": "provide-evidence" in links}


PROVIDE = {"provide-evidence": {"href": "https://api-m.sandbox.paypal.com/x", "method": "POST"}}


@pytest.mark.usefixtures("flags_off")
def test_for_dispute_a_confirmed_send_reopens_when_paypal_lists_provide_evidence_again():
    """A confirmed latest version is read-only: when PayPal asks for evidence again the next step is the start of a new
    version (upload, then generate), never 'confirmed' and never a review or send of the sent version."""
    from copilot.web import next_step
    from copilot.web.status import SendRecord
    confirmed = SendRecord(state="confirmed", confirmed_at="2026-10-17T04:10:00+00:00", attachments_match=True)
    ready = dict(dispute_id=DID, latest_package={"id": 7, "version": 1}, package_claims=2, package_has_tracking=True,
                 approval_valid=True, record=confirmed)
    asked = next_step.for_dispute(norm=_norm(PROVIDE), has_documents=True, **ready)
    assert asked.key == "generate" and asked.action.post == f"/disputes/{DID}/packages" and not asked.action.sends
    assert "Package v1 stays as it was sent." in asked.body and "Nothing is sent." in asked.body
    assert next_step.for_dispute(norm=_norm(PROVIDE), has_documents=False, **ready).key == "upload"
    assert next_step.for_dispute(norm=_norm(PROVIDE), has_documents=True, auto_prep_status="preparing",
                                 **ready).key == "preparing"
    assert next_step.for_dispute(norm=_norm({}), has_documents=True, **ready).key == "confirmed"


@pytest.mark.usefixtures("flags_off")
def test_the_dispute_page_panel_reopens_with_the_task_rows_when_paypal_lists_provide_evidence_again():
    import test_ux_rc41_dispute as dispute_tests
    from copilot import config
    db = config.db_path()
    package = dispute_tests._package(db)
    dispute_tests._attempt(db, package, "confirmed")
    dispute_tests._doc(db)
    web, fake = dispute_tests._web(dispute_tests._body(rels=("provide_evidence",)))
    text, dom = dispute_tests._page(web)
    assert "A new version needs its own send." in dispute_tests._task(dom, "send").text()   # v1 stays done,
    assert "approved" not in dispute_tests._task(dom, "approve").text()                     # the open rows are a new version
    panel = dom.find(id="next-step")
    assert panel.attrs["data-next-step"] == "generate"                                   # and so does the panel
    assert [f.attrs["action"] for f in panel.find_all("form")][0] == f"/disputes/{DID}/packages"
    assert all(call[0] == "GET" for call in fake.calls)
    quiet, _ = dispute_tests._web(dispute_tests._body("WAITING_FOR_BUYER_RESPONSE", rels=()))
    assert dispute_tests._page(quiet)[1].find(id="next-step").attrs["data-next-step"] == "confirmed"


# ------------------------------------------------------------------------------------------------ item d
def test_the_fulfilment_check_has_its_own_timeline_label():
    from copilot.web import presentation
    assert presentation.action_label("fulfilment_checked") == "Checked the tracking in PayPal"
    assert presentation.action_label("fulfilment_registered") == "Recorded the tracking registration result"


# ------------------------------------------------------------------------------------------------ item f
def test_the_send_summary_rows_have_a_two_column_layout_that_stacks_on_a_phone():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "copilot/web/static/app.css").read_text(encoding="utf-8")
    block = css.split("/* ux:integration start */")[1].split("/* ux:integration end */")[0]
    assert ".send-label" in block and ".send-rows {" in block and "grid-template-columns" in block
    assert "@media (max-width: 599.98px) { .send-rows { grid-template-columns: 1fr;" in block


# ------------------------------------------------------------------------------------------------ item j
@pytest.mark.usefixtures("flags_off")
def test_the_conflict_page_leaves_a_file_out_with_the_server_built_return_and_the_package_trail(tmp_path, monkeypatch):
    """error.html's conflict form: 'Leave {filename} out of the checks' carries the server-built return_to
    ('package:{pid}', as the package page does) so the seller lands back on the package checks; the trail names the
    package as the parent of the refusal (the breadcrumb convention: the current page last)."""
    import re
    from _ux_lint import back_links, breadcrumb_items
    from copilot.web.routes_disputes import templates
    c, db, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg = send_tests._approved_package_with_tracking(c, db, token)
    doc = repo.list_documents(db, DID)[0]
    html = templates.env.get_template("error.html").render(
        title="Error: This package conflicts with PayPal's data", message="Cannot approve.", kind="error",
        state="refused", items=[], show_last_read=False, retry_url=None, back_url=None, back_label="Back",
        last_read=None, dispute_id=DID,
        conflict_form={"dispute_id": DID, "package_id": pkg, "csrf_token": token,
                       "documents": [{"id": doc["id"], "filename": doc["filename"]}]})
    form = parse(html).find("form", action=f"/disputes/{DID}/documents/{doc['id']}/exclude")
    assert form is not None and form.find("button").text() == f"Leave {doc['filename']} out of the checks"
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', html.split(form.attrs["action"])[1].split("</form>")[0]))
    assert fields["return_to"] == f"package:{pkg}"
    assert breadcrumb_items(html) == [("All disputes", "/disputes"), (f"Dispute {DID}", f"/disputes/{DID}"),
                                      ("Evidence package", f"/disputes/{DID}/packages/{pkg}"),
                                      ("This package conflicts with PayPal's data", None)]
    assert back_links(html) == [] and "&larr;" not in html and "from consideration" not in html
    r = c.post(form.attrs["action"], data=fields, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/disputes/{DID}/packages/{pkg}?")
    assert r.headers["location"].endswith("#checks") and repo.get_document(db, doc["id"])["excluded"]


# ------------------------------------------------------------------------------------------------ item k
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_package_pending_state_has_exactly_one_check_again_the_panel_primary(tmp_path, monkeypatch):
    import test_ux_states as states_tests
    c, db, token, fake, pkg, _r = states_tests._uncertain_send(tmp_path, monkeypatch)
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg}").text)
    assert state(dom, "result-uncertain") is not None                               # the banner still says what is known
    buttons = [n for n in dom.walk() if n.tag == "button" and n.text().startswith("Check again")]
    assert len(buttons) == 1 and "btn-primary" in buttons[0].attrs["class"]
    panel = dom.find(id="next-step")
    assert panel.attrs["data-next-step"] == "pending" and [n for n in panel.walk() if n is buttons[0]]
    assert len(fake.provide_evidence_calls) == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_confirmed_package_keeps_its_one_secondary_check_again(tmp_path, monkeypatch):
    c, db, token, fake = send_tests._client(tmp_path, monkeypatch, post_send_get_bodies=[send_tests._confirmed_body()])
    pkg = send_tests._approved_package_with_tracking(c, db, token)
    c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token})
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg}").text)
    buttons = [n for n in dom.walk() if n.tag == "button" and n.text().startswith("Check again")]
    assert len(buttons) == 1 and "btn-secondary" in buttons[0].attrs["class"]


# ------------------------------------------------------------------------------------------------ item m
def test_the_per_check_messages_speak_as_proofie_not_we():
    """copilot/core/checklist.py: every message text passed to a check (b.add) names Proofie instead of 'we/our'."""
    import ast
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / "copilot/core/checklist.py").read_text(encoding="utf-8")
    found, messages = [], []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add" and len(node.args) >= 3:
            parts = [n.value for n in ast.walk(node.args[2]) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            text = "".join(parts)
            messages.append(text)
            if re.search(r"\b(?:we|our|us)\b", text, re.I):
                found.append((node.lineno, text))
    assert found == []
    assert any("Proofie's own consistency check, not a PayPal requirement" in m for m in messages)
    assert any("This is Proofie's own caution, not a PayPal requirement." in m for m in messages)
