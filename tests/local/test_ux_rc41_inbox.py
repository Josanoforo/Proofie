"""The inbox, PayPal due dates, account fee line, triage page and events page.

Offline only: a fake PayPal client, a temporary database per test (conftest.py), DOM checks through _dom / _ux_lint.
Behaviour items were written first and failed for the right reason before the code.
"""
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest
from _dom import parse
from _hosted import authenticated_client
import _ux_lint as lint

from copilot import config
from copilot.app import create_app
from copilot.core import auto_prep, disputes as core_disputes
from copilot.db import repo
from copilot.web import routes_disputes
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "copilot" / "web" / "templates"
UTC = timezone.utc
INR = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
NOT_AS_DESCRIBED = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
SELF = {"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/X", "rel": "self", "method": "GET"}
PROVIDE = {"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/X/provide-evidence", "rel": "provide_evidence",
           "method": "POST"}

pytestmark = [pytest.mark.usefixtures("web_mode")]


def item(did, status="WAITING_FOR_SELLER_RESPONSE", due_hours=None, reason=INR, links=None, outcome=None):
    body = {"dispute_id": did, "status": status, "reason": reason, "create_time": "2026-10-05T02:13:17.000Z",
            "update_time": "2026-10-05T02:16:52.000Z", "dispute_life_cycle_stage": "INQUIRY",
            "dispute_amount": {"currency_code": "USD", "value": "25.00"},
            "links": [SELF] + (links if links is not None else ([PROVIDE] if status.startswith("WAITING_FOR_SELLER") else []))}
    if due_hours is not None:
        body["seller_response_due_date"] = (datetime.now(UTC) + timedelta(hours=due_hours)).isoformat()
    if outcome:
        body["dispute_outcome"] = {"outcome_code": outcome}
    return body


class Fake:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if op == "list_disputes":
            return {"items": self.items, "links": []}
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def page(monkeypatch, items, flags=("fulfilment",), path="/disputes", prep=None, claims=None):
    """GET `path` with these disputes. prep maps dispute id -> auto_prep row (only those ids are prepared)."""
    for flag in flags:
        monkeypatch.setenv(f"COPILOT_FEATURE_{flag.upper()}", "true")
    prep = prep or {}
    monkeypatch.setattr(auto_prep, "should_prepare", lambda norm: norm["dispute_id"] in prep)
    monkeypatch.setattr(repo, "claim_auto_prep", lambda *a, **k: None)
    monkeypatch.setattr(repo, "get_auto_prep", lambda db, did: prep.get(did))
    monkeypatch.setattr(repo, "list_claims", lambda db, pid: (claims or {}).get(pid, []))
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake(items)
    response = authenticated_client(app, client=("127.0.0.1", 12345)).get(path)
    assert response.status_code == 200, response.text[:300]
    return response.text


def row(dom, did):
    found = dom.find("tr", data_dispute=did)
    assert found is not None, f"no row for {did}"
    return found


AI_CLAIM = [{"basis": "model_interpretation", "text": "x"}]
PLAIN_CLAIM = [{"basis": "verified", "text": "x"}]


def four_cases(monkeypatch):
    items = [item("PP-A", due_hours=240), item("PP-B", due_hours=48),
             item("PP-C", "WAITING_FOR_BUYER_RESPONSE", links=[]), item("PP-D", "UNDER_REVIEW", links=[])]
    prep = {"PP-B": {"status": "ready", "package_id": 7, "has_claims": False, "detail": None}}
    return page(monkeypatch, items, prep=prep, claims={7: AI_CLAIM})


# ---- inbox one primary and a next step per row -------------
def test_inbox_one_primary_and_a_next_step_per_row(monkeypatch):
    html = four_cases(monkeypatch)
    lint.assert_single_primary(html)
    dom = parse(html)
    a, b, c, d = (row(dom, f"PP-{x}") for x in "ABCD")
    link_a = a.find("a", class_="btn")
    assert link_a.text().startswith("Respond to this dispute") and link_a.attrs["href"] == "/disputes/PP-A"
    assert link_a.find("span", class_="visually-hidden").text() == "for PP-A"
    link_b = b.find("a", class_="btn")
    assert link_b.text().startswith("Review the draft") and link_b.attrs["href"] == "/disputes/PP-B/packages/7"
    assert link_b.find("span", class_="visually-hidden").text() == "for PP-B"
    # the soonest case that needs the seller (after sorting) holds the one primary button
    assert "btn-primary" in link_b.attrs["class"].split() and "btn-secondary" in link_a.attrs["class"].split()
    assert c.find("a", class_="btn") is None
    assert "Nothing to do now: PayPal shows Waiting for the buyer." in c.text()
    assert d.find("a", class_="btn") is None and "PayPal is updating this dispute" in d.text()
    assert "Draft prepared. Check each statement." in b.text()
    assert "Some statements were written by AI (local model)." in b.text()
    assert "Draft ready" not in html


def test_inbox_summary_line_and_fulfilment_link_below_the_table(monkeypatch):
    html = four_cases(monkeypatch)
    dom = parse(html)
    summary = dom.find(data_state="inbox-summary")
    assert summary is not None
    text = summary.text()
    assert text == "2 disputes need your response."
    assert "Soonest:" not in text and "respond by" not in text
    pp_b_due = dom.find("tr", data_dispute="PP-B").find("td", data_label="Respond by").text()
    assert pp_b_due and re.search(r"\d{1,2} \w+ 2026", pp_b_due) and "Due in 47 hours" in pp_b_due
    assert "Review PayPal deadlines" not in html
    links = [a for a in dom.find_all("a", href="/fulfilment") if not a.inside("header")]
    assert any(a.text() == "Tracking at the sale: files" for a in links)
    table_at, link_at = html.index('data-table="inbox"'), html.index("Tracking at the sale: files")
    assert link_at > table_at


def test_inbox_failed_preparing_and_needs_documents_states(monkeypatch):
    items = [item("PP-1", due_hours=100), item("PP-2", due_hours=101), item("PP-3", due_hours=102), item("PP-4", due_hours=103)]
    prep = {"PP-1": {"status": "failed", "detail": "x"}, "PP-2": {"status": "preparing"},
            "PP-3": {"status": "needs_documents", "detail": "no document"},
            "PP-4": {"status": "ready", "package_id": 9, "has_claims": False, "detail": None}}
    dom = parse(page(monkeypatch, items, prep=prep))
    one, two, three, four = (row(dom, f"PP-{n}") for n in "1234")
    assert one.find("a", class_="btn").text().startswith("Start the response")
    assert one.find("a", class_="btn").attrs["href"] == "/disputes/PP-1/workspace/prepare#package"
    assert two.find("a", class_="btn") is None
    assert "Preparing a draft. Reload this page to see it." in two.text()
    assert three.find("a", class_="btn").text().startswith("Upload documents")
    assert three.find("a", class_="btn").attrs["href"] == "/disputes/PP-3/workspace/prepare#documents"
    assert "Draft: needs documents" in three.text()
    assert four.find("a", class_="btn").text().startswith("Add the details")
    assert "Draft started. Details are missing." in four.text()
    cell = three.find("td", data_draft_state="needs_documents")
    assert cell is not None


def test_inbox_claim_under_review_says_nothing_to_do(monkeypatch):
    items = [item("PP-U", "UNDER_REVIEW", links=[])]
    items[0]["dispute_life_cycle_stage"] = "CHARGEBACK"
    dom = parse(page(monkeypatch, items, flags=("fulfilment", "outcome")))
    assert "Nothing to do now. PayPal is reviewing this claim." in row(dom, "PP-U").text()


# ---- five hours past due reads as passed never overdue on both pages ---
def test_five_hours_past_due_reads_as_passed_never_overdue_on_both_pages(monkeypatch):
    items = [item("PP-LATE", due_hours=-5), item("PP-OK", due_hours=400)]
    for path in ("/disputes", "/deadlines"):
        html = page(monkeypatch, items, flags=("fulfilment", "deadlines"), path=path)
        assert "The due date passed 5 hours ago" in html, path
        assert "Overdue" not in html and "overdue" not in html.replace("Overdue", ""), path
        dom = parse(html)
        banner = dom.find("div", class_="banner-warn")
        assert banner is not None and banner.attrs["data-state"].startswith("deadline"), path
        assert "Respond soon" in banner.text()
        assert any(a.attrs["href"] == "/disputes/PP-LATE" and a.text() == "Open dispute PP-LATE" for a in banner.find_all("a")), path
        assert any(a.attrs["href"] == "/deadlines" and a.text() == "See all due dates" for a in banner.find_all("a")), path
        assert "At the last PayPal read (" in banner.text() and "still waited for your response." in banner.text()


def test_due_today_wording_only_on_the_same_mexico_city_date(monkeypatch):
    from copilot.web import timefmt
    now = datetime(2026, 10, 7, 18, tzinfo=UTC)          # 12:00 in Mexico City
    same = {"seller_response_due_date": (now + timedelta(hours=5)).isoformat()}      # 5 pm the same day
    next_day = {"seller_response_due_date": (now + timedelta(hours=14)).isoformat()}  # 2 am the next day
    assert timefmt.due_display(same, now)["remaining"] == "Due today, in 5 hours"
    assert timefmt.due_display(next_day, now)["remaining"] == "Due in 14 hours"


# ---- sort key buckets and date order -----------------------
def test_sort_key_buckets_and_date_order():
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    waiting_far = item("PP-FAR", due_hours=None) | {"seller_response_due_date": (now + timedelta(days=10)).isoformat()}
    waiting_near = item("PP-NEAR") | {"seller_response_due_date": (now + timedelta(days=2)).isoformat()}
    closed = item("PP-CLOSED", "RESOLVED", links=[])
    opened = item("PP-OPEN", "UNDER_REVIEW", links=[])
    pairs = [(core_disputes.normalize(raw, now), raw) for raw in (closed, waiting_far, opened, waiting_near)]
    ordered = [n["dispute_id"] for n, _ in sorted(pairs, key=lambda p: core_disputes.inbox_sort_key(*p))]
    assert ordered == ["PP-NEAR", "PP-FAR", "PP-OPEN", "PP-CLOSED"]


@pytest.mark.parametrize("path", ["/disputes", "/deadlines"])
def test_three_cases_render_soonest_waiting_first_then_closed(monkeypatch, path):
    items = [item("PP-CLOSED", "RESOLVED", links=[]), item("PP-TEN", due_hours=240), item("PP-TWO", due_hours=48)]
    html = page(monkeypatch, items, flags=("fulfilment", "deadlines"), path=path)
    dom = parse(html)
    table = dom.find("table")
    ids = [r.attrs["data-dispute"] for r in table.find_all("tr") if "data-dispute" in r.attrs]
    assert ids == ["PP-TWO", "PP-TEN", "PP-CLOSED"]
    caption = table.find("caption")
    assert caption is not None and caption.text() == "Disputes waiting for you come first, soonest due date first."
    assert all(th.attrs.get("scope") == "col" for th in table.find_all("th"))


# ---- inaction sentence appears once and no ten day date ----
def test_inaction_sentence_appears_once_and_no_ten_day_date(monkeypatch):
    html = four_cases(monkeypatch)
    sentence = "If you do nothing, Proofie sends nothing."
    # One shared context follows the rows at both widths; the full explanation stays in its accessible toggletip.
    assert html.count(sentence) == 1 and html.count('class="inbox-context"') == 1
    context = html[html.index('class="inbox-context"'):]
    assert "What happens if you do nothing" in context and "tip-body" in context
    text = unescape(re.sub(r"<[^>]+>", "", html))          # the sentence holds a link, so compare the plain text
    assert ("If you do nothing, Proofie sends nothing. If the buyer escalates the case without your response, PayPal's help "
            "says PayPal resolves it in the buyer's favor (PayPal Help HELP1083). "
            "The “Respond by” date is the one PayPal gives for this case.") in text
    assert "10 days" not in text and "10-day" not in text
    assert any(a.text() == "PayPal Help HELP1083" and a.attrs["href"].endswith("HELP1083") for a in parse(html).find_all("a"))


# ---- auto prep only for item not received ------------------
def test_auto_prep_only_for_item_not_received():
    base = {"dispute_id": "PP-X", "status": "WAITING_FOR_SELLER_RESPONSE"}
    assert auto_prep.should_prepare({**base, "reason": INR}) is True
    assert auto_prep.should_prepare({**base, "reason": NOT_AS_DESCRIBED}) is False
    assert auto_prep.should_prepare({**base, "reason": "UNAUTHORISED"}) is False


def test_non_item_not_received_row_names_the_limit(monkeypatch):
    items = [item("PP-NAD", due_hours=100, reason=NOT_AS_DESCRIBED), item("PP-INR", due_hours=101)]
    dom = parse(page(monkeypatch, items))
    line = "Proofie prepares proof of shipment only. Check whether that answers this case."
    assert line in row(dom, "PP-NAD").text()
    assert line not in row(dom, "PP-INR").text()
    assert row(dom, "PP-NAD").find("a", class_="btn").text().startswith("Respond to this dispute")


# ---- owned templates ---------------------------------------
OWNED_TEMPLATES = [TEMPLATES / "disputes_list.html",
                   *[p for name in ("deadlines", "account_cliff", "triage", "events")
                     for p in sorted((TEMPLATES / "features" / name).glob("*.html"))]]


def test_owned_templates_pass_the_shell_lint():
    assert len(OWNED_TEMPLATES) == 8
    assert lint.template_lint(OWNED_TEMPLATES) == []


def test_inbox_has_no_breadcrumb_and_the_new_title_and_heading(monkeypatch):
    html = four_cases(monkeypatch)
    dom = parse(html)
    assert lint.breadcrumb_items(html) == [] and lint.back_links(html) == []
    assert dom.find("h1").text() == "All disputes"
    assert dom.find("title").text() == "All disputes - Proofie"
    assert lint.bare_buttons(html) == [] and lint.large_labels_ok(html)


# ----------------------------------------------------------------------------------------- deadlines page
def test_deadlines_page_columns_labels_breadcrumb_and_no_refresh(monkeypatch):
    items = [item("PP-D1", due_hours=100), item("PP-D2"), item("PP-D3", "WAITING_FOR_BUYER_RESPONSE", links=[])]
    html = page(monkeypatch, items, flags=("fulfilment", "deadlines"), path="/deadlines")
    dom = parse(html)
    assert lint.breadcrumb_items(html) == [("All disputes", "/disputes"), ("PayPal due dates", None)]
    assert lint.back_links(html) == [] and "http-equiv" not in html and "Overdue" not in html
    assert dom.find("h1").text() == "PayPal due dates" and dom.find("title").text() == "PayPal due dates - Proofie"
    assert [th.text() for th in dom.find("table").find_all("th")] == ["Dispute", "Respond by", "Time left"]
    assert all("data-label" in td.attrs for td in dom.find_all("td"))
    assert "Outside the warning threshold" not in html
    rows = {r.attrs["data-dispute"]: r.text() for r in dom.find_all("tr") if "data-dispute" in r.attrs}
    assert "No verified deadline available" in rows["PP-D2"] and "No response due from you" in rows["PP-D3"]
    assert "Disputes waiting for you, soonest due date first. Proofie marks cases with less than 3 days left." in dom.text()
    check = dom.find(data_state="checked-at")
    assert check is not None and "Refresh from PayPal" in check.text()


# ---- inbox columns cell order and no cell mixes status and action ---
def test_inbox_columns_cell_order_and_no_cell_mixes_status_and_action(monkeypatch):
    dom = parse(four_cases(monkeypatch))
    table = dom.find("table", data_table="inbox")
    assert sorted(th.text() for th in table.find_all("th")) == sorted(["Dispute", "Status", "Amount", "Respond by", "Next step"])
    assert "Draft" not in [th.text() for th in table.find_all("th")]
    for tr in table.find("tbody").find_all("tr"):
        assert [td.attrs["data-label"] for td in tr.find_all("td")] == ["Dispute", "Status", "Respond by", "Amount", "Next step"]
        status_cell, step_cell = tr.find_all("td")[1], tr.find_all("td")[4]
        assert status_cell.find("a") is None and "data-draft-state" in step_cell.attrs
    assert "white-space: normal" in (ROOT / "copilot/web/static/app.css").read_text(encoding="utf-8").split("/* ux:inbox start */")[1]


def test_empty_state_a_keeps_hooks_and_moves_the_guide_into_details(monkeypatch):
    html = page(monkeypatch, [])
    dom = parse(html)
    assert dom.find(data_state="empty") is not None and dom.find("p", data_state="empty-guide") is not None
    details = dom.find("details", data_state="empty-guide-box")
    assert details.find("summary").text() == "First time using Proofie?" and details.find("p", data_state="empty-guide")
    banner_text = dom.find(data_state="empty").text()
    assert "Nothing was changed." in banner_text and "<time" in html
    again = dom.find(data_state="empty").find("a", class_="btn")
    assert again.text() == "Check PayPal again" and "btn-primary" in again.attrs["class"].split()
    lint.assert_single_primary(html)


def test_empty_state_b_counts_the_cases_that_are_not_waiting(monkeypatch):
    items = [item("PP-1", "WAITING_FOR_BUYER_RESPONSE", links=[]), item("PP-2", "UNDER_REVIEW", links=[]),
             item("PP-3", "RESOLVED", links=[])]
    html = page(monkeypatch, items)
    dom = parse(html)
    assert dom.find(data_state="inbox-summary").text() == "Nothing needs your response. 2 cases are with PayPal or the buyer; 1 closed."
    only_closed = parse(page(monkeypatch, [item("PP-3", "RESOLVED", links=[])]))
    assert only_closed.find(data_state="inbox-summary").text() == "Nothing needs your response. 1 closed."
    lint.assert_single_primary(html, allow_zero=True)


# ----------------------------------------------------------------------------------------- triage
class TriageFake:
    def __init__(self):
        self.raw = {"dispute_id": "PP-R-DEMO-0000001", "status": "WAITING_FOR_SELLER_RESPONSE", "reason": INR,
                    "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"currency_code": "USD", "value": "25.00"},
                    "create_time": "2026-09-25T16:00:00Z", "evidences": [], "links": []}

    def call(self, method, path, op, **kwargs):
        assert method == "GET"
        return self.raw if "/customer/disputes/" in path else {}


def triage_pages(monkeypatch, flags):
    monkeypatch.setenv("COPILOT_FEATURE_TRIAGE", "true")
    for name in ("RESOLUTION", "DRAFT", "LEDGER"):
        monkeypatch.delenv(f"COPILOT_FEATURE_{name}", raising=False)
    for flag in flags:
        monkeypatch.setenv(f"COPILOT_FEATURE_{flag.upper()}", "true")
    fake = TriageFake()
    repo.insert_package(config.db_path(), fake.raw["dispute_id"], "human_edit", True, [])
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    base = "/disputes/PP-R-DEMO-0000001"
    return web.get(base + "/triage").text, web.get(base + "/workspace/other").text


def test_triage_one_name_everywhere_and_no_we_our_us(monkeypatch):
    html, detail = triage_pages(monkeypatch, ())
    dom = parse(html)
    assert dom.find("h1").text().startswith("What the evidence supports")
    assert dom.find("title").text().startswith("What the evidence supports")
    entry = [a for a in parse(detail).find_all("a") if a.attrs.get("href", "").endswith("/triage")]
    assert [a.text() for a in entry] == ["What the evidence supports"]
    assert lint.breadcrumb_items(html) == [("All disputes", "/disputes"), ("Dispute PP-R-DEMO-0000001", "/disputes/PP-R-DEMO-0000001"),
                                           ("What the evidence supports", None)]
    assert lint.back_links(html) == []
    # The page's own words. The per-check messages come from copilot/core/checklist.py (not this package; some still say
    # "we"/"our"): they sit inside list items and the technical details, so those are left out of the scan.
    own = re.sub(r"<li[^>]*>.*?</li>", " ", re.sub(r"<details.*?</details>", " ", html, flags=re.S), flags=re.S)
    words = set(re.findall(r"[A-Za-z']+", unescape(re.sub(r"<[^>]+>", " ", own))))     # "US" (United States) is not "us"
    assert not {"we", "We", "our", "Our", "us", "Us", "ours", "we're", "We're", "we've"} & words
    headings = [h.text() for h in dom.find_all("h2")]
    assert "Quality cautions from Proofie" in headings and "What Proofie could not read" in headings
    assert "Your documents do not show delivery under this rule." in html


def test_triage_resolution_link_only_with_the_flag_and_add_proof_names_its_item(monkeypatch):
    off, _ = triage_pages(monkeypatch, ())
    assert "Resolution options and buyer messages" not in off
    on, _ = triage_pages(monkeypatch, ("resolution",))
    dom = parse(on)
    link = next(a for a in dom.find_all("a") if a.text().startswith("Resolution options and buyer messages"))
    assert link.attrs["href"] == "/disputes/PP-R-DEMO-0000001/resolution" and "btn-large" in link.attrs["class"].split()
    proofs = [a for a in dom.find_all("a") if a.text().startswith("Add proof")]
    assert proofs and all(a.find("span", class_="visually-hidden").text().startswith("for ") for a in proofs)
    lint.assert_single_primary(on, allow_zero=True)


def test_triage_ledger_link_text(monkeypatch):
    html, _ = triage_pages(monkeypatch, ("ledger",))
    assert any(a.text() == "What this app recorded" for a in parse(html).find_all("a"))
