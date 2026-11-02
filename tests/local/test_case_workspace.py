"""The case workspace (D-88): /disputes/<id> is a short overview and each task has its own page. Synthetic rows in a
temporary database, a fake read-only PayPal client; no network.

Expected, written before the first run:
- the overview holds only: the case strip, the next task, the task table, the case links, the short "where this dispute
  stands" summary and one "Other possible responses" panel that links to its own view; every panel that used to stack on the page is gone from it;
- each of the seven task views answers 200 with the flags on, carries the case strip, the left navigation, a way back to
  the overview, exactly one <main>, and marks its own navigation entry aria-current="page";
- with every flag off the links to flagged pages are not drawn; with a flag on the same link is reachable from the
  overview within two clicks (the inventory in internal/release/CASE-WORKSPACE-V3.md);
- the red rule: a blocked task row carries data-status="blocked" and its words;
- seller-facing views say none of the banned technical words (judge-only words may stay in the Hackathon review view);
- an unknown view is a 404 refusal page, a bad dispute id is refused before any PayPal call.
"""
import os
import re
from pathlib import Path

import pytest
from _dom import parse
from _hosted import authenticated_client

from copilot.app import create_app
from copilot.db import repo
from copilot import config
from copilot.web import case_workspace
from copilot.web.routes_disputes import get_client

DID = "PP-R-DEMO-0000001"
CAPTURE = "DEMOTXN000000001A"[:17]
ROOT = Path(__file__).resolve().parents[2]
FEATURES = sorted(p.name for p in (ROOT / "copilot" / "features").iterdir()
                  if p.is_dir() and not p.name.startswith("__"))
BANNED = ("MCP", "artifact", "provider state", " rows", "snapshot", "scheduler", "writer", "non-excluded", "gap(s)", "gaps")


class Fake:
    def call(self, method, path, op, **kw):
        assert method == "GET"
        if op == "show_dispute":
            return {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
                    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "dispute_life_cycle_stage": "INQUIRY",
                    "dispute_amount": {"currency_code": "USD", "value": "25.00"}, "create_time": "2026-09-25T16:00:00Z",
                    "seller_response_due_date": "2099-01-01T00:00:00Z",
                    "disputed_transactions": [{"seller_transaction_id": CAPTURE}], "evidences": [],
                    "links": [{"rel": "provide_evidence", "method": "POST", "href": "https://x"}]}
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(op)


def _env(monkeypatch, flags):
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    for flag in flags:
        monkeypatch.setenv("COPILOT_FEATURE_" + flag.upper(), "true")


def _client(monkeypatch, flags=()):
    _env(monkeypatch, flags)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    return authenticated_client(app, client=("127.0.0.1", 12345))


def _main_count(html):
    return len(re.findall(r"<main[\s>]", html))


def test_overview_holds_only_the_listed_blocks(monkeypatch):
    c = _client(monkeypatch, FEATURES)
    r = c.get(f"/disputes/{DID}")
    assert r.status_code == 200
    html = r.text
    for state in ("case-strip", "case-links", "other-responses", "summary"):
        assert f'data-state="{state}"' in html, state
    assert 'id="tasks"' in html and 'id="next-step"' in html
    for gone in ('id="documents"', 'id="package"', 'id="sale-readiness"', 'id="queue-explanation"', 'id="local-mcp-notes"',
                 'id="local-autopilot"', 'id="buyer-note"', 'data-state="paypal-timeline"', 'data-state="timeline"',
                 'id="optional"'):
        assert gone not in html, gone
    assert 'data-state="case-more-nav"' in html and "Other case links" in html
    assert f'href="/disputes/{DID}/workspace/other"' in html
    assert f'href="/disputes/{DID}/workspace/other"' in html
    assert _main_count(html) == 1


@pytest.mark.parametrize("view", case_workspace.VIEWS)
def test_every_task_view_renders_with_all_flags_on(monkeypatch, view):
    c = _client(monkeypatch, FEATURES)
    r = c.get(f"/disputes/{DID}/workspace/{view}")
    assert r.status_code == 200, r.text[:300]
    html = r.text
    assert 'data-state="case-strip"' in html
    assert 'data-state="case-primary-nav"' in html and f'href="/disputes/{DID}"' in html
    assert _main_count(html) == 1
    current = re.findall(r'<a href="([^"]+)" aria-current="page"', html)
    assert current == [f"/disputes/{DID}/workspace/{view}"]


def test_overview_marks_itself_current(monkeypatch):
    html = _client(monkeypatch, FEATURES).get(f"/disputes/{DID}").text
    assert re.findall(r'<a href="([^"]+)" aria-current="page"', html) == [f"/disputes/{DID}"]


def test_documents_and_package_moved_to_prepare(monkeypatch):
    html = _client(monkeypatch).get(f"/disputes/{DID}/workspace/prepare").text
    assert 'id="documents"' in html and 'id="package"' in html and 'name="file"' in html


def test_flags_off_hide_their_links_and_flags_on_show_them(monkeypatch):
    off = _client(monkeypatch).get(f"/disputes/{DID}")
    assert f"/disputes/{DID}/history" not in off.text and "/features/judge_walkthrough" not in off.text
    assert "Hackathon review" not in off.text and "Case records" in off.text
    records_off = _client(monkeypatch).get(f"/disputes/{DID}/workspace/records").text
    assert f"/disputes/{DID}/history" not in records_off
    records_on = _client(monkeypatch, ["case_history"]).get(f"/disputes/{DID}/workspace/records").text
    assert f'href="/disputes/{DID}/history"' in records_on
    on = _client(monkeypatch, ["judge_walkthrough", "mcp_local_contracts"]).get(f"/disputes/{DID}").text
    assert "Hackathon review" in on
    hack = _client(monkeypatch, ["judge_walkthrough", "mcp_local_contracts"]).get(f"/disputes/{DID}/workspace/hackathon").text
    assert "/features/judge_walkthrough" in hack and "/features/mcp_local_contracts" in hack


def test_other_responses_have_their_own_view_and_are_flag_driven(monkeypatch):
    html = _client(monkeypatch, ["resolution", "draft", "first_dispute_guide", "two_clocks"]).get(
        f"/disputes/{DID}/workspace/other").text
    block = html[html.index('id="other-responses-list"'):]
    for href in (f"/disputes/{DID}/resolution", f"/disputes/{DID}/draft", f"/disputes/{DID}/first-guide",
                 f"/disputes/{DID}/two-clocks"):
        assert f'href="{href}"' in block, href
    other_dom = parse(block)
    leading = other_dom.find(data_state="other-leading")
    more = other_dom.find("details", data_state="more-options")
    assert leading is not None and leading.find("a").attrs["href"] == f"/disputes/{DID}/resolution"
    assert more is not None and "open" not in more.attrs
    assert more.find("a", href=f"/disputes/{DID}/draft") is not None
    plain = _client(monkeypatch).get(f"/disputes/{DID}/workspace/other").text
    assert f"/disputes/{DID}/resolution" not in plain
    # O4: the overview folds exactly two things, both closed and both holding only links the left rail repeats:
    # the secondary case links and the other responses. Nothing else hides in a disclosure.
    overview = _client(monkeypatch).get(f"/disputes/{DID}").text
    assert re.findall(r'<details([^>]*)>', overview) == [' class="case-more-nav" data-state="case-more-nav"',
                                                        ' class="other-responses" id="other-responses" data-state="other-responses"']


def test_task_table_has_state_words_and_a_primary_open(monkeypatch):
    html = _client(monkeypatch).get(f"/disputes/{DID}").text
    rows = re.findall(r'<tr class="task-row" data-task="(\w+)" data-status="(\w+)"', html)
    assert [r[0] for r in rows] == ["upload", "prepare", "approve", "send"]
    assert dict(rows)["approve"] == "blocked" and "Cannot start yet" in html


def test_unknown_view_and_bad_id_are_refused(monkeypatch):
    c = _client(monkeypatch)
    assert c.get(f"/disputes/{DID}/workspace/nope").status_code == 404
    bad = c.get("/disputes/not%20valid/workspace/prepare")
    assert bad.status_code in (400, 404)


@pytest.mark.parametrize("view", [v for v in case_workspace.VIEWS if v != "hackathon"])
def test_seller_views_use_no_technical_words(monkeypatch, view):
    c = _client(monkeypatch, [f for f in FEATURES if f not in
                              ("mcp_local_contracts", "local_autopilot", "explainable_queue")] + ["sale_readiness"])
    html = c.get(f"/disputes/{DID}/workspace/{view}").text
    text = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", "", html))
    for word in BANNED:
        assert word not in text, (view, word)


def test_overview_text_has_no_technical_words(monkeypatch):
    c = _client(monkeypatch, [f for f in FEATURES if f not in ("mcp_local_contracts", "local_autopilot")])
    text = re.sub(r"<[^>]+>", " ", c.get(f"/disputes/{DID}").text)
    for word in BANNED:
        assert word not in text, word


def test_sale_readiness_words_are_plain(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_SALE_READINESS", "true")      # the view only exists with the flag on
    view = {"result": {"status": "incomplete; resolve listed gaps", "price_cents": 1500, "currency": "USD"},
            "gaps": ["valid_expected_date", "evidence:delivery", "tracking:tracking number"]}
    words = case_workspace.sale_readiness_words(view)
    assert words["price"] == "15.00 USD"
    assert words["missing"][0].startswith("Missing: a valid expected delivery date")
    assert "delivery" in words["missing"][1] and ":" not in words["missing"][1].replace("Missing:", "")
    assert "cents" not in str(words) and "gap" not in str(words).lower()


def _hrefs(html):
    return set(re.findall(r'href="([^"#][^"]*)"', html))


def test_every_inventory_item_is_reachable_within_two_clicks_from_the_overview(monkeypatch):
    """The inventory in internal/release/CASE-WORKSPACE-V3.md: click 1 is a navigation entry (or the folded disclosure
    on the overview), click 2 is the link itself. With every flag on, every flagged page the old case page linked to is
    among the links of the overview or of the pages the overview links to by navigation."""
    c = _client(monkeypatch, FEATURES)
    overview = c.get(f"/disputes/{DID}").text
    first = _hrefs(overview)
    nav = [h for h in first if h.startswith(f"/disputes/{DID}/workspace/")]
    assert len(nav) >= 7
    second = set()
    for href in nav:
        page = c.get(href)
        assert page.status_code == 200, href
        second |= _hrefs(page.text)
    reach = first | second
    base = f"/disputes/{DID}"
    expected = [f"{base}/history", f"{base}/outcome", f"{base}/ledger", f"{base}/resolution", f"{base}/draft",
                f"{base}/triage", f"{base}/call-script", f"{base}/two-clocks", f"{base}/first-guide",
                f"{base}/lost-package-paths?capture_id={CAPTURE}", f"{base}/buyer-note?capture_id={CAPTURE}",
                f"{base}/third-party-statements", f"/fulfilment/{CAPTURE}?from_dispute={DID}",
                f"/fulfilment/{CAPTURE}/moment", f"{base}/documents",
                "/features/judge_walkthrough?case_id=" + DID, "/features/local_demo_hub?case_id=" + DID,
                "/features/mcp_local_contracts", "/features/evaluation_workbench"]
    missing = [h for h in expected if h not in reach and not any(h in text for text in
               (c.get(n).text for n in nav))]
    assert not missing, missing


def test_flags_off_leave_out_the_hackathon_review(monkeypatch):
    html = _client(monkeypatch).get(f"/disputes/{DID}").text
    names = re.findall(r'data-nav="(\w+)"', html)
    # O4: Overview and Review claims are the two primary links (data-state case-primary-nav); the rest sit in "Other case links"
    assert names == ["prepare", "approve", "send", "records", "other", "sale"]   # sale: the case names a sale; no judge page
    assert 'data-state="case-primary-nav"' in html and ">Review claims<" in html


def _with_package(monkeypatch, flags):
    c = _client(monkeypatch, flags)
    db = config.db_path()
    repo.ensure_schema(db)
    package = repo.insert_package(db, DID, "auto_manual", True, [])
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "I shipped the parcel on 20 September.",
                                      "source_ids": [], "basis": "seller_statement"}])
    return c, package


def test_review_view_shows_each_statement_beside_its_source_and_keeps_the_full_package_reachable(monkeypatch):
    c, package = _with_package(monkeypatch, ("claims",))
    html = c.get(f"/disputes/{DID}/workspace/review").text
    assert 'data-state="review-sources"' in html and "I shipped the parcel on 20 September." in html
    assert 'class="claim-list"' in html and "Your own statement" in html
    assert f'href="/disputes/{DID}/packages/{package}"' in html and "Full package details" in html
    assert 'data-state="review-workspace"' in html and 'aria-label="Package review status"' in html
    assert html.index('data-state="review-sources"') < html.index('data-state="review-summary"')
    assert f'href="/disputes/{DID}/packages/{package}#statements"' in html
    assert 'method="post"' not in html
    off = _client(monkeypatch).get(f"/disputes/{DID}/workspace/review").text
    assert 'data-state="review-sources"' not in off and "Review claims" in off


def test_approve_view_previews_the_exact_content_and_the_approve_button_goes_to_the_unchanged_approval_page(monkeypatch):
    c, package = _with_package(monkeypatch, ("active_approval",))
    html = c.get(f"/disputes/{DID}/workspace/approve").text
    assert 'data-state="approve-preview"' in html and "I shipped the parcel on 20 September." in html
    assert "To PayPal" in html and "Tracking:" in html and "Attachments:" in html
    assert "Approving does not send anything" in html and "Full package details" in html
    # no tracking number yet: the task stays blocked and says why; no approve control is drawn on this view
    assert "Cannot start yet" in html and "name=\"review_sha256\"" not in html
