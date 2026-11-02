"""The dispute page (templates/dispute_detail.html and routes_disputes.py): the documents table, the next
step before the case strip, the stage fact and strip, the undo in the leave-out banner, the Resolution Center
sentence, no browser validation bubble on the upload form, table captions. Offline only: fake PayPal clients and a temp
database per test (conftest.py); assertions are on the DOM and on the stored rows the page wrote or read. Both web modes.
"""
import re
from pathlib import Path

import pytest
from _dom import parse
from _hosted import set_test_csrf

from copilot import config
from copilot.db import repo
from copilot.web import links, presentation, routes_disputes

import test_ux_rc41_dispute as dt
from test_ux_rc41_dispute import DID

# fixtures and helpers of the earlier rounds, for the legacy-attempt case (same imports as test_ux_rc41_fix3_state3)
from test_ux_rc41_fix_state import LABEL_A, LABEL_B, STATEMENT, add_file, document_rows  # noqa: F401
from test_ux_rc41_package import approve_passive, draft, env  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]

TEMPLATE = Path(__file__).resolve().parents[2] / "copilot" / "web" / "templates" / "dispute_detail.html"


# ------------------------------------------------------------------------------------------------ a legacy attempt names its files only
# Expected: a detached file of a names-only (pre-sha) confirmed attempt reads 'Sent to PayPal, confirmed <time>' in the
# documents table, the summary still says "was sent and is no longer attached here", and a sha-tracked partial send keeps
# 'Not sent' for the file PayPal does not list.
def test_the_detached_file_of_a_legacy_names_only_attempt_reads_sent_in_the_documents_table(env):
    c, db, token, fake = env
    from test_ux_rc41_fix_state import seed_confirmed
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    doc_b = add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg, with_shas=False)                       # an attempt that predates the sha records
    repo.detach_document(db, pkg, doc_b)                           # a detach while it was unconfirmed
    html = c.get(f"/disputes/{DID}/workspace/prepare").text
    rows = {r["file"]: r["in_paypal"] for r in document_rows(html)}
    assert rows["b.pdf"].startswith("Sent to PayPal, confirmed "), rows
    assert rows["a.pdf"].startswith("Sent to PayPal, confirmed "), rows
    text = parse(c.get(f"/disputes/{DID}").text).text()          # the summary sits on the overview
    assert "b.pdf was sent and is no longer attached here." in text and "b.pdf is in PayPal" not in text
    leave_out = [r for r in document_rows(html) if r["file"] == "b.pdf"][0]["forms"]
    assert leave_out == [], "a sent file offers no leave-out button"


def test_a_legacy_name_is_consumed_once_two_uploads_of_one_name_are_not_both_sent(env):
    c, db, token, fake = env
    from test_ux_rc41_fix_state import seed_confirmed
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    doc_b = add_file(c, db, token, pkg, "b.pdf", LABEL_B)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg, with_shas=False)
    repo.detach_document(db, pkg, doc_b)
    repo.insert_document(db, DID, "b.pdf", "application/pdf", 10, "c" * 64, "x", True)      # a second upload of that name
    rows = [r for r in document_rows(c.get(f"/disputes/{DID}/workspace/prepare").text) if r["file"] == "b.pdf"]
    assert sorted(r["in_paypal"].startswith("Sent to PayPal") for r in rows) == [False, True], rows


# ------------------------------------------------------------------------------------------------ next step before the case strip
# Expected: DOM order is h1, the not-an-item-not-received note, the late-confirmation banner, the next-step panel, the case
# strip, the tasks. The first primary action is inside the panel.
def test_the_next_step_panel_precedes_the_case_strip_and_the_tasks():
    text, dom = dt._page(dt._web(dt._body(reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"))[0])
    marks = [text.index("<h1"), text.index('data-state="case-strip"'), text.index('data-state="shipment-proof-only"'),
             text.index('data-next-step="'), text.index('id="tasks"')]
    assert marks == sorted(marks) and len(set(marks)) == 5
    panel = dom.find(id="next-step")
    assert panel is not None and [n for n in dt._primaries(dom) if n in list(panel.walk())], "the primary lives in the panel"


def test_the_late_confirmation_banner_stays_above_the_panel_in_the_template_source():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert (source.index("{% if late_confirmations %}") < source.index("{% call next_step_panel(")
            < source.index('id="tasks"'))


# ------------------------------------------------------------------------------------------------ STAGE
# Expected: PayPal's four stage values print the table words; the strip lists the four in PayPal's order with exactly one
# 'current stage' marker; nothing derived (no done / to-come words), no form or control inside it; an unknown value prints
# 'Stage not recognized' and no strip; a missing value prints no fact; the legacy CLAIM prints 'Claim' and no strip.
STAGES = [("INQUIRY", "Inquiry"), ("CHARGEBACK", "Chargeback"), ("PRE_ARBITRATION", "Pre-arbitration"),
          ("ARBITRATION", "Arbitration")]


def _stage_page(stage):
    body = dt._body()
    if stage is None:
        body.pop("dispute_life_cycle_stage", None)
    else:
        body["dispute_life_cycle_stage"] = stage
    return dt._page(dt._web(body)[0], view="records")


def _stage_fact(dom):
    for row in dom.find("dl", class_="facts").find_all("div"):
        if row.find("dt").text() == "Stage":
            return row.find("dd").text()
    return None


@pytest.mark.parametrize("value, name", STAGES)
def test_each_of_paypals_four_stages_prints_its_word_and_marks_only_itself_in_the_strip(value, name):
    text, dom = _stage_page(value)
    assert _stage_fact(dom) == name
    strip = dom.find("ol", class_="stage-strip")
    assert strip.attrs["aria-label"] == "Where this dispute is in PayPal's process"
    items = strip.find_all("li")
    assert [i.text().removesuffix(" (current stage)") for i in items] == [n for _, n in STAGES]
    current = [i for i in items if i.attrs.get("aria-current") == "step"]
    assert [i.text() for i in current] == [f"{name} (current stage)"]
    assert all("(current stage)" not in i.text() for i in items if i not in current)
    assert value not in text.replace("PRE_ARBITRATION" if False else "\0", "")          # never the enum on screen
    assert not strip.find_all("a") and not strip.find_all("form") and not strip.find_all("button")
    assert not strip.find_all("input")
    assert not re.search(r"\b(done|completed|to come|upcoming|next)\b", strip.text(), re.I)


def test_an_unknown_stage_prints_status_not_recognized_and_no_strip():
    text, dom = _stage_page("SOMETHING_NEW")
    assert _stage_fact(dom) == "Stage not recognized" and dom.find("ol", class_="stage-strip") is None
    assert "SOMETHING_NEW" not in text


def test_a_missing_stage_prints_no_fact_and_no_strip():
    text, dom = _stage_page(None)
    assert _stage_fact(dom) is None and dom.find("ol", class_="stage-strip") is None


def test_the_legacy_claim_value_prints_claim_and_never_enters_the_strip():
    text, dom = _stage_page("CLAIM")
    assert _stage_fact(dom) == "Claim" and dom.find("ol", class_="stage-strip") is None


def test_the_strip_uses_no_second_primary_and_the_page_keeps_one():
    text, dom = _stage_page("CHARGEBACK")
    assert len(dt._primaries(dom)) <= 1


# ------------------------------------------------------------------------------------------------ UNDO
# Expected: only 'document_excluded' carries an undo: one POST form to the existing include route with the csrf field and
# the accessible name 'Undo leaving out <file>'. No undo for a document PayPal holds, for the opposite key, or for a forged
# or stale ?done=. The undo POST returns to the page with the 'document_included' banner and the document is used again.
def _excluded_page(name="left_out.pdf"):
    db = config.db_path()
    doc = dt._doc(db, name)
    repo.set_document_excluded(db, doc, True)
    web, fake = dt._web()
    return web, doc, dt._page(web, f"?done=document_excluded&doc={doc}", view="prepare")


def test_the_leave_out_banner_holds_one_undo_form_to_the_include_route_with_a_csrf_field():
    web, doc, (text, dom) = _excluded_page()
    banner = dom.find("div", data_state="done")
    forms = banner.find_all("form")
    assert len(forms) == 1 and forms[0].attrs["method"] == "post"
    assert forms[0].attrs["action"] == f"/disputes/{DID}/documents/{doc}/include"
    assert [i.attrs["name"] for i in forms[0].find_all("input")] == ["csrf_token"]
    button = forms[0].find("button")
    assert button.text() == "Undo leaving out left_out.pdf"
    assert button.find("span", class_="visually-hidden") is not None
    assert "left_out.pdf is left out of the checks." in banner.text()


def test_the_opposite_and_other_banners_carry_no_undo():
    db = config.db_path()
    kept = dt._doc(db, "kept.pdf")
    web, _ = dt._web()
    banner = dt._page(web, f"?done=document_included&doc={kept}", view="prepare")[1].find("div", data_state="done")
    assert banner is not None and banner.find_all("form") == []
    added = dt._page(web, f"?done=document_added&doc={kept}", view="prepare")[1].find("div", data_state="done")
    assert added is not None and added.find_all("form") == []


def test_a_forged_or_stale_done_query_shows_no_banner_and_so_no_undo():
    db = config.db_path()
    kept = dt._doc(db, "kept.pdf")
    web, _ = dt._web()
    for query in (f"?done=document_excluded&doc={kept}", "?done=document_excluded&doc=9999",
                  f"?done=document_excluded&doc={kept}x", "?done=detached&doc=1"):
        text, dom = dt._page(web, query, view="prepare")
        assert dom.find("div", data_state="done") is None and f"/documents/{kept}/include" not in text, query


def test_a_document_that_paypal_holds_shows_no_undo(env):
    c, db, token, fake = env
    from test_ux_rc41_fix_state import seed_confirmed
    pkg = draft(c, db, token, STATEMENT)
    doc = add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    repo.set_document_excluded(db, doc, True)                      # past the route guard, as an old row would be
    html = c.get(f"/disputes/{DID}/workspace/prepare?done=document_excluded&doc={doc}").text
    banner = parse(html).find("div", data_state="done")
    assert banner is not None and banner.find_all("form") == []


def test_posting_the_undo_uses_the_document_again_and_returns_with_the_matching_banner():
    db = config.db_path()
    web, doc, (text, dom) = _excluded_page()
    form = dom.find("div", data_state="done").find("form")
    token = form.find("input", name="csrf_token").attrs["value"]            # the token the page rendered, not one made up here
    assert len(token) >= 20
    set_test_csrf(web, token)
    audit_before = len(repo.list_audit(db, DID))
    response = web.post(form.attrs["action"], data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/disputes/{DID}/workspace/prepare?done=document_included&doc={doc}")
    assert not next(d for d in repo.list_documents(db, DID) if d["id"] == doc)["excluded"]
    assert [e["action"] for e in repo.list_audit(db, DID)][audit_before:] == ["include_document"]
    after = dt._page(web, f"?done=document_included&doc={doc}", view="prepare")[1].find("div", data_state="done")
    assert after is not None and after.find_all("form") == []                    # no ping-pong


def test_a_buyer_closed_dispute_and_a_claim_under_review_show_no_undo(monkeypatch):
    """The page offers no document change there, so the banner offers no undo either."""
    db = config.db_path()
    doc = dt._doc(db, "left_out.pdf")
    repo.set_document_excluded(db, doc, True)
    query = f"?done=document_excluded&doc={doc}"
    dt._flags(monkeypatch, outcome=True)
    closed = dt._body("RESOLVED", rels=(), dispute_outcome={"outcome_code": "CANCELED_BY_BUYER"})
    under_review = dt._body("UNDER_REVIEW", rels=(), dispute_life_cycle_stage="CHARGEBACK")
    for body in (closed, under_review):
        text, dom = dt._page(dt._web(body)[0], query, view="prepare")
        banner = dom.find("div", data_state="done")
        assert banner is not None and banner.find_all("form") == [] and f"/documents/{doc}/include" not in text
    text, dom = dt._page(dt._web()[0], query, view="prepare")                                         # the same page of an open dispute does offer it
    assert len(dom.find("div", data_state="done").find_all("form")) == 1


def test_done_banner_undo_is_built_by_the_shell_hook_for_exactly_the_leave_out_key():
    undo = presentation.done_undo("document_excluded", "/x", "a.pdf")
    assert undo["hidden"] == "leaving out a.pdf" and undo["fields"] == ()
    assert presentation.done_undo("document_included", "/x", "a.pdf") is None
    assert routes_disputes._done_banner({"done": "document_excluded", "doc": "1"},
                                        [{"id": 1, "filename": "a.pdf", "excluded": 1}], DID)["undo"]["post"] \
        == f"/disputes/{DID}/documents/1/include"
    assert routes_disputes._done_banner({"done": "document_excluded", "doc": "1"},
                                        [{"id": 1, "filename": "a.pdf", "excluded": 1}], DID, can_change=False)["undo"] is None


# ------------------------------------------------------------------------------------------------ RESOLUTION CENTER
# Expected: one sentence in 'Where this dispute stands' whose words 'PayPal's Resolution Center (sandbox)' are the link; the
# link goes to the sandbox host, opens in a new tab with rel noopener, carries the hidden new-tab note; the page links no
# live-host Resolution Center; the app requests nothing from it.
def test_the_summary_panel_has_the_sign_in_sentence_with_the_sandbox_resolution_center_link():
    web, fake = dt._web()
    text, dom = dt._page(web, view="send")
    summary = dom.find("section", id="send-facts")
    sentence = summary.find("p", data_state="resolution-center")
    anchors = sentence.find_all("a")
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor.attrs["href"] == links.RESOLUTION_CENTER_URL and links.RESOLUTION_CENTER_URL.startswith(
        "https://www.sandbox.paypal.com/")
    assert anchor.attrs["rel"] == "noopener" and anchor.attrs["target"] == "_blank"
    assert anchor.find("span", class_="visually-hidden").text() == "(opens in a new tab)"
    assert anchor.children[0] == "PayPal's Resolution Center (sandbox)"                # the anchor's own words, exactly
    assert anchor.text() == "PayPal's Resolution Center (sandbox) (opens in a new tab)"
    visible = sentence.text().replace(" (opens in a new tab)", "")
    assert visible == links.SIGN_IN_SENTENCE
    assert [c for c in fake.calls if "resolutioncenter" in c[1]] == []
    every = [a.attrs.get("href", "") for a in dom.find_all("a")]
    assert not any("www.paypal.com/resolutioncenter" in h for h in every)
    assert [h for h in every if "sandbox.paypal.com" in h] == [links.RESOLUTION_CENTER_URL]


# ------------------------------------------------------------------------------------------------ FORM + TABLES
# Expected: the upload form carries novalidate and keeps required (and aria-required) on the file input, so the server's own
# page answers an empty submit; both tables have a visually-hidden caption and every th has a scope.
def test_the_upload_form_has_novalidate_and_keeps_its_required_file_input():
    text, dom = dt._page(dt._web()[0], view="prepare")
    form = dom.find("section", id="documents").find("form")
    assert "novalidate" in form.attrs
    field = form.find("input", id="upload-file")
    assert "required" in field.attrs and field.attrs.get("aria-required") == "true"


def test_an_empty_upload_gets_the_servers_own_refusal_page_not_a_browser_bubble():
    web, fake = dt._web()
    token = "upload-token-" + "y" * 20
    set_test_csrf(web, token)
    response = web.post(f"/disputes/{DID}/documents", data={"csrf_token": token})
    assert response.status_code == 400 and "Select a document to upload" in response.text


def test_the_documents_and_packages_tables_have_hidden_captions_and_scoped_headers():
    db = config.db_path()
    dt._doc(db, "label.pdf")
    dt._package(db)
    text, dom = dt._page(dt._web()[0], view="prepare")
    expected = {"documents": "Documents uploaded for this dispute", "package": "Evidence packages for this dispute"}
    for section_id, caption in expected.items():
        table = dom.find("section", id=section_id).find("table")
        cap = table.find("caption")
        assert cap is not None and cap.text() == caption and "visually-hidden" in cap.attrs["class"].split()
        assert table.attrs["class"] == "stack"
        assert all(th.attrs.get("scope") in ("col", "row") for th in table.find_all("th"))


# ------------------------------------------------------------------------------------------------ same statements, same files
# Expected: the send task row keeps 'When Proofie checked, PayPal held these statements from an earlier send.' for a collision with other
# files, and reads 'PayPal already has this exact package. Nothing to send.' when the collision says same_files; both
# add the once-per-dispute rule and link the Resolution Center only while PayPal lists seller evidence; otherwise they keep the "Change a statement" link; the row stays blocked.
@pytest.mark.parametrize("listed", [True, False], ids=["listed", "not-listed"])
@pytest.mark.parametrize("same_files, sentence", [
    (False, "When Proofie checked, PayPal held these statements from an earlier send."),
    (True, "PayPal already has this exact package. Nothing to send.")])
def test_the_send_row_words_the_collision_by_whether_the_files_match(monkeypatch, same_files, sentence, listed):
    from types import SimpleNamespace
    from copilot.web import status
    package = dt._package(config.db_path())
    collision = SimpleNamespace(state="confirmed", earlier_version=1, earlier_package_id=package["id"], unsent=(),
                                same_files=same_files)
    monkeypatch.setattr(status, "statement_collision", lambda *args, **kwargs: collision)
    body = dt._body(evidences=[{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT"}]) if listed else None
    text, dom = dt._page(dt._web(body)[0] if body else dt._web()[0])
    row = dom.find("tr", data_task="send")
    assert row.attrs["data-status"] == "blocked"
    if listed:
        assert sentence in row.text() and "Proofie sends evidence once per dispute" in row.text()
        assert "Change a statement" not in row.text()
        assert row.find("a").attrs["href"] == "https://www.sandbox.paypal.com/resolutioncenter"
    else:                                                       # the three original assertions
        assert sentence in row.text() and "Change a statement" in row.text()
        assert row.find("a").text() == "Change a statement" and row.find("a").attrs["href"].endswith("#statements")
        assert "once per dispute" not in row.text() and "PayPal already lists" not in row.text()


# ------------------------------------------------------------------------------------------------ stacked table labels
# Expected: every td of the documents and packages tables that holds content carries its data-label; the 'Actions' label sits
# on every cell: a cell that holds a control, and the cell of a document PayPal holds, which says in words that no action
# is available (stacked cards need a label and a value on every cell).
def test_every_cell_of_the_stacked_tables_has_a_label_and_the_actions_label_marks_a_control():
    db = config.db_path()
    dt._doc(db, "label.pdf")
    dt._package(db)
    text, dom = dt._page(dt._web()[0], view="prepare")
    for section_id in ("documents", "package"):
        table = dom.find("section", id=section_id).find("table")
        cells = [td for td in table.find_all("td")]
        assert cells and all(td.attrs.get("data-label") for td in cells), section_id
    row = dom.find("section", id="documents").find("tbody").find("tr")
    actions = row.find_all("td")[-1]
    assert actions.attrs["data-label"] == "Actions" and len(actions.find_all("form")) == 1


def test_a_sent_document_has_a_labelled_last_cell_that_says_no_action_is_available(env):
    c, db, token, fake = env
    from test_ux_rc41_fix_state import seed_confirmed
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    seed_confirmed(db, pkg)
    row = parse(c.get(f"/disputes/{DID}/workspace/prepare").text).find("section", id="documents").find("tbody").find("tr")
    cell = row.find_all("td")[-1]
    assert cell.attrs.get("data-label") == "Actions" and cell.text() == "No action available" and cell.find_all("form") == []
    # said once: the column header is the hidden word "Actions", the cell holds the sentence and no second hidden copy
    html = c.get(f"/disputes/{DID}/workspace/prepare").text
    assert html.count("No action available") == 1


# ------------------------------------------------------------------------------------------------ empty documents line
# Expected: an ordinary open dispute with no documents keeps the invitation to type the details in the package; a claim under
# review (stage CHARGEBACK, status UNDER_REVIEW), where no package can be sent, says only that no documents are uploaded.
def test_the_empty_documents_line_invites_typing_only_where_a_package_can_be_sent(monkeypatch):
    dt._flags(monkeypatch, outcome=True)
    text, dom = dt._page(dt._web()[0], view="prepare")
    line = dom.find("p", data_state="no-documents")
    assert line is not None and line.text() == (
        "No documents uploaded yet. Scanned images may need you to type the details yourself in the package.")
    under_review = dt._body("UNDER_REVIEW", rels=(), dispute_life_cycle_stage="CHARGEBACK")
    text, dom = dt._page(dt._web(under_review)[0], view="prepare")
    line = dom.find("p", data_state="no-documents")
    assert line is not None and line.text() == "No documents uploaded."
    closed = dt._body("RESOLVED", rels=(), dispute_outcome={"outcome_code": "CANCELED_BY_BUYER"})
    text, dom = dt._page(dt._web(closed)[0], view="prepare")                                      # the buyer closed the case: same neutral line
    assert dom.find("p", data_state="no-documents").text() == "No documents uploaded."
