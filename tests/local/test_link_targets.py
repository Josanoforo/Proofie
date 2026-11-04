"""LINK-FIX (Nov 2): a control shown to the user must work. Every internal link a case page draws answers 2xx or 3xx;
where an action is not available for the case, the page says why in words and draws no link that leads to an error.
Synthetic rows, a fake read-only PayPal client, no network.

Expected, written before the first run (the first four fail on a14ee06f, the log is
data/kits/Proofie-Kits/logs-RC44/D101/link-fix/before-fix-link-targets-4.log):
- buyer-note page without a carrier tracking page: no `href="None"`, the sentence "No verified carrier tracking page" shows;
- prepare page for a sale with no saved shipment record: no link to the sale buyer note or the delivery-issue page, the
  reason is in words; with a saved shipment record both links are drawn and answer 200;
- presentation guide without a chosen case: the package print card is not a link to /disputes/print;
- a fresh case: the shipment-photo link opens a working page that says why the case link waits (its own Save works),
  the seller-lesson links are drawn only when the lesson page will accept them (a closed outcome with a code);
- a case that is not a seeded demo case does not offer "Seeded demo case"; a seeded one does and the page answers 200;
- a refused print says which check refused it.
"""
import os
import re
from pathlib import Path

import pytest
from _hosted import authenticated_client

from copilot import config
from copilot.app import create_app
from copilot.web.routes_disputes import get_client

DID = "PP-R-DEMO-0000001"
CAPTURE = "DEMOTXN000000001A"[:17]
SEEDED_NAME = "Ana Demo Ficticia"
ROOT = Path(__file__).resolve().parents[2]
FEATURES = sorted(p.name for p in (ROOT / "copilot" / "features").iterdir()
                  if p.is_dir() and not p.name.startswith("__"))


@pytest.fixture(autouse=True)
def _own_data_folder(tmp_path, monkeypatch):
    """Every page here may open a local store: keep them in a temporary folder, never in the checkout's data/ folder
    (the first version of this file left data/third_party_statements.sqlite3 behind, and the hosted upload budget test counted it)."""
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))


def _dispute(decided):
    """decided: False (open), True (closed with an outcome code) or "no_code" (RESOLVED, PayPal gives no outcome code)."""
    base = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE",
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "dispute_life_cycle_stage": "INQUIRY",
            "dispute_amount": {"currency_code": "USD", "value": "25.00"}, "create_time": "2026-09-25T16:00:00Z",
            "seller_response_due_date": "2099-01-01T00:00:00Z",
            "disputed_transactions": [{"seller_transaction_id": CAPTURE}], "evidences": [],
            "links": [{"rel": "provide_evidence", "method": "POST", "href": "https://x"}]}
    if decided:
        base.update(status="RESOLVED", dispute_life_cycle_stage="CHARGEBACK", links=[])
        if decided is True:
            base["dispute_outcome"] = {"outcome_code": "RESOLVED_BUYER_FAVOUR"}
    return base


class Fake:
    def __init__(self, decided=False, recipient=None):
        self.decided, self.recipient = decided, recipient

    def call(self, method, path, op, **kw):
        assert method == "GET"
        if "search" in op:
            if self.recipient is None:
                return {"transaction_details": []}
            return {"transaction_details": [{"transaction_info": {
                "transaction_id": CAPTURE, "transaction_initiation_date": "2026-09-01T12:00:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "25.00"}},
                "shipping_info": {"name": self.recipient, "address": {"country_code": "MX", "city": "Ciudad de Mexico"}}}]}
        if "tracking" in op:
            return {"trackers": []}
        if op == "list_disputes":
            return {"items": []}
        return _dispute(self.decided)       # show_dispute, outcome_read, resolution_read: the one dispute


def _client(monkeypatch, flags=None, decided=False, recipient=None):
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    for flag in (FEATURES if flags is None else flags):
        monkeypatch.setenv("COPILOT_FEATURE_" + flag.upper(), "true")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake(decided, recipient)
    return authenticated_client(app, client=("127.0.0.1", 12345))


def _hrefs(html):
    found = re.findall(r'<a\b[^>]*?href="([^"]*)"', html)
    return [h.replace("&amp;", "&") for h in found]


def _internal(html):
    return [h for h in _hrefs(html) if h.startswith("/") and not h.startswith(("/static", "//"))]


def _dead_links(c, html, skip=()):
    dead = []
    for href in dict.fromkeys(_internal(html)):
        if any(part in href for part in skip):
            continue
        status = c.get(href, follow_redirects=False).status_code
        if status >= 400:
            dead.append((href, status))
    return dead


def test_buyer_note_page_draws_no_tracking_link_when_no_carrier_page_is_known(monkeypatch):
    c = _client(monkeypatch)
    r = c.get(f"/disputes/{DID}/buyer-note?capture_id={CAPTURE}")
    assert r.status_code == 200
    assert 'href="None"' not in r.text and "Open public carrier tracking" not in r.text
    assert "No verified carrier tracking page is available" in r.text
    assert _dead_links(c, r.text) == []


@pytest.mark.parametrize("decided", [False, "no_code", True])
@pytest.mark.parametrize("view", ["prepare", "sale", "other", "records", "send", "approve", "review"])
def test_no_workspace_page_links_to_an_error(monkeypatch, view, decided):
    c = _client(monkeypatch, decided=decided)
    r = c.get(f"/disputes/{DID}/workspace/{view}")
    assert r.status_code == 200
    assert _dead_links(c, r.text) == []


@pytest.mark.parametrize("decided", [False, "no_code", True])
def test_outcome_and_overview_link_to_no_error(monkeypatch, decided):
    c = _client(monkeypatch, decided=decided)
    for page in (f"/disputes/{DID}", f"/disputes/{DID}/outcome", f"/disputes/{DID}/resolution"):
        r = c.get(page)
        assert r.status_code == 200, page
        assert _dead_links(c, r.text) == [], page


def test_presentation_guide_offers_no_package_card_without_a_chosen_case(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/presentation-mode")
    assert r.status_code == 200
    assert "/disputes/print" not in r.text and "Open this guide from a case with a saved package" in r.text
    assert _dead_links(c, r.text) == []


def test_photo_link_on_a_fresh_case_opens_a_working_page_and_says_why_the_case_link_waits(monkeypatch):
    c = _client(monkeypatch)
    prepare = c.get(f"/disputes/{DID}/workspace/prepare")
    entry = f"/fulfilment/{CAPTURE}/moment"
    assert entry in _hrefs(prepare.text) and f"{entry}?from_dispute={DID}" not in _hrefs(prepare.text)
    assert 'data-state="shipment-photos-case-link-needs-terms"' in prepare.text and "save the sale terms" in prepare.text
    page = c.get(entry)
    assert page.status_code == 200
    assert 'name="from_dispute" value=""' in page.text and "from_package" not in page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    saved = c.post(f"/fulfilment/{CAPTURE}/moment", data={"csrf_token": token, "carrier": "DHL", "tracking_number": "80123456",
                                                          "from_dispute": ""}, follow_redirects=False)
    assert saved.status_code == 303                       # the page's own Save works


def test_sale_lesson_links_are_drawn_only_when_the_lesson_page_will_accept_them(monkeypatch):
    lesson = f"/disputes/{DID}/sale-lesson?capture_id={CAPTURE}"
    for decided, drawn in ((False, False), ("no_code", False), (True, True)):
        c = _client(monkeypatch, decided=decided)
        for page in (f"/disputes/{DID}/outcome", f"/disputes/{DID}/workspace/sale"):
            body = c.get(page)
            assert body.status_code == 200, (decided, page)
            assert (lesson in _hrefs(body.text)) is drawn, (decided, page)
            assert _dead_links(c, body.text) == [], (decided, page)
        if drawn:
            assert c.get(lesson).status_code == 200
    c = _client(monkeypatch)
    assert "can be recorded once PayPal has recorded the outcome" in c.get(f"/disputes/{DID}/outcome").text
    c = _client(monkeypatch, decided="no_code")
    assert "can be recorded once PayPal has recorded the outcome" in c.get(f"/disputes/{DID}/workspace/sale").text


def test_seeded_demo_entry_is_drawn_only_for_a_case_the_page_accepts(monkeypatch):
    seeded = f"/disputes/{DID}/seeded-demo"
    flags = [*FEATURES, "demo_documents_seeded"]          # a flag that lives inside the demo_documents folder
    other = _client(monkeypatch, flags)
    page = other.get(f"/disputes/{DID}/workspace/prepare")
    assert seeded not in _hrefs(page.text) and other.get(seeded).status_code == 409      # the guard is the route's own
    mine = _client(monkeypatch, flags, recipient=SEEDED_NAME)
    page = mine.get(f"/disputes/{DID}/workspace/prepare")
    assert seeded in _hrefs(page.text)
    assert mine.get(seeded).status_code == 200


# ------------------------------------------------------------------------------------------------ crawl addendum (Nov 2)
def _error_layout(page):
    """The site's error layout: a full page with its way back, not a bare text body."""
    return ("<html" in page.text and "text/html" in page.headers["content-type"] and "<main" in page.text
            and 'href="/disputes' in page.text)


@pytest.mark.parametrize("path", [f"/fulfilment/{CAPTURE}/buyer-note", f"/fulfilment/{CAPTURE}/delivery-incident"])
def test_sale_note_pages_refuse_in_the_error_layout_when_no_shipment_is_saved(monkeypatch, path):
    c = _client(monkeypatch)
    page = c.get(path)
    assert page.status_code == 404 and "No saved local shipment record exists for this sale." in page.text
    assert _error_layout(page)


def test_call_script_and_accounting_line_refuse_in_the_error_layout_with_a_way_back(monkeypatch):
    c = _client(monkeypatch, decided=True)                    # a closed chargeback: no call script
    page = c.get(f"/disputes/{DID}/call-script")
    assert page.status_code == 409 and "open inquiry only" in page.text and _error_layout(page)
    assert f'href="/disputes/{DID}"' in page.text and c.get(f"/disputes/{DID}").status_code == 200
    bad = c.get("/disputes/not%20valid/call-script")
    assert bad.status_code == 400 and _error_layout(bad)


def test_resolution_page_shows_the_reason_instead_of_a_form_PayPal_would_refuse(monkeypatch):
    c = _client(monkeypatch)
    page = c.get(f"/disputes/{DID}/resolution")
    assert page.status_code == 200
    assert 'data-state="draft-not-available"' in page.text and "PayPal lists no offer or message action" in page.text
    assert f'action="/disputes/{DID}/resolution/draft"' not in page.text


def test_resolution_page_keeps_the_draft_form_when_PayPal_lists_a_message_action(monkeypatch):
    c = _client(monkeypatch)
    c.app.dependency_overrides[get_client] = lambda: _MessageAllowed()
    page = c.get(f"/disputes/{DID}/resolution")
    assert page.status_code == 200 and f'action="/disputes/{DID}/resolution/draft"' in page.text
    assert 'value="followup"' in page.text and 'value="partial_offer"' not in page.text      # only what PayPal lists


class _MessageAllowed(Fake):
    def call(self, method, path, op, **kw):
        raw = super().call(method, path, op, **kw)
        if isinstance(raw, dict) and raw.get("dispute_id"):
            raw["links"] = raw["links"] + [{"rel": "send_message", "method": "POST",
                                            "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{DID}/send-message"}]
        return raw


def test_sale_lesson_page_that_cannot_take_a_lesson_draws_no_form(monkeypatch):
    c = _client(monkeypatch)
    page = c.get(f"/disputes/{DID}/sale-lesson?capture_id={CAPTURE}")
    assert page.status_code == 409 and "has not recorded a closed outcome" in page.text
    assert "<form" not in page.text.split("<main", 1)[1] and "Save seller lesson" not in page.text
    c = _client(monkeypatch, decided=True)
    page = c.get(f"/disputes/{DID}/sale-lesson?capture_id={CAPTURE}")
    assert page.status_code == 200 and "Save seller lesson" in page.text


def test_local_demo_hub_tables_scroll_inside_their_container(monkeypatch):
    c = _client(monkeypatch)
    html = c.get(f"/features/local_demo_hub?case_id={DID}").text
    tables = html.count("<table")
    assert tables >= 1 and html.count('class="table-scroll"') == tables


# ------------------------------------------------------------------------------------- crawl round 2 (M4, M5, L11)
RECORD = "PP-R-HJE-10194967"


@pytest.mark.pool_scope
def test_record_case_hub_offers_only_links_that_answer(monkeypatch):
    from _hosted import configure_hosted
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_JUDGE_POOL", "PP-R-SAM-10191037")
    configure_hosted(monkeypatch)
    c = _client(monkeypatch, [*FEATURES, "judge_pool"])
    page = c.get(f"/features/local_demo_hub?case_id={RECORD}")
    assert page.status_code == 200
    assert _dead_links(c, page.text) == []
    assert f"/disputes/{RECORD}/resolution" not in _hrefs(page.text) and f"/disputes/{RECORD}/outcome" not in _hrefs(page.text)


@pytest.mark.pool_scope
def test_hub_keeps_its_case_links_for_a_case_the_visitor_may_work_on(monkeypatch):
    from _hosted import configure_hosted
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_JUDGE_POOL", DID)
    configure_hosted(monkeypatch)
    c = _client(monkeypatch, [*FEATURES, "judge_pool"])
    hrefs = _hrefs(c.get(f"/features/local_demo_hub?case_id={DID}").text)
    assert f"/disputes/{DID}/resolution" in hrefs and f"/disputes/{DID}/outcome" in hrefs


def test_access_page_brand_is_not_a_link_to_a_page_the_gate_sends_back(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-access-code")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    gate = TestClient(create_app(), base_url="https://localhost").get("/access")
    assert gate.status_code == 200
    assert '<span class="brand"' in gate.text and '<a class="brand"' not in gate.text
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    inside = _client(monkeypatch).get(f"/disputes/{DID}")
    assert '<a class="brand" href="/disputes"' in inside.text


def test_phone_menu_closes_with_escape():
    script = (ROOT / "copilot" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'event.key === "Escape" && mb.getAttribute("aria-expanded") === "true"' in script and "setMenu(false); mb.focus();" in script


# ------------------------------------------------------------------------------------------ local crawl (F-07)
def test_conformance_page_with_an_evidence_folder_that_does_not_exist_yet_says_nothing_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "not-created-yet"))
    c = _client(monkeypatch)
    page = c.get("/conformance")
    assert page.status_code == 200
    assert "Responses checked: none. No recorded PayPal responses were found on this computer." in page.text


# ------------------------------------------------------------------------------------------ local crawl (F-05)
FORM_PAGES = ("sale_readiness", "evidence_dossier", "explainable_queue", "appeal_workspace", "evaluation_workbench",
              "local_autopilot", "negotiation_drafts")


@pytest.mark.parametrize("slug", FORM_PAGES)
def test_an_emptied_box_answers_a_page_with_an_error_summary_never_raw_json(monkeypatch, slug):
    c = _client(monkeypatch)
    page = c.get(f"/features/{slug}")
    assert page.status_code == 200 and '<textarea id="data" name="data"' in page.text
    assert re.search(r'<textarea id="data" name="data"[^>]*\brequired\b', page.text)        # the browser stops an empty send first
    token = re.search(r'name="csrf_token" value="([^"]*)"', page.text)
    sent = c.post(f"/features/{slug}", data={"data": "", "csrf_token": token.group(1) if token else ""})
    assert sent.status_code == 422 and "text/html" in sent.headers["content-type"]
    assert '{"detail"' not in sent.text and "Fill in what is missing" in sent.text
    assert "Missing or not valid: the box on this page." in sent.text and 'href="/features/' + slug in sent.text.replace("&amp;", "&")


def test_lessons_exchange_import_without_a_file_answers_a_page(monkeypatch):
    c = _client(monkeypatch)
    page = c.get("/lessons-exchange")
    token = re.search(r'name="csrf_token" value="([^"]*)"', page.text).group(1)
    sent = c.post("/lessons-exchange/import", data={"csrf_token": token}, headers={"referer": "http://localhost/lessons-exchange"})
    assert sent.status_code == 422 and "text/html" in sent.headers["content-type"] and '{"detail"' not in sent.text
    assert "the file to import" in sent.text and 'href="/lessons-exchange"' in sent.text


def test_a_json_caller_keeps_the_default_validation_answer(monkeypatch):
    c = _client(monkeypatch)
    sent = c.post("/features/sale_readiness", json={"nothing": 1})
    assert sent.status_code == 422 and sent.headers["content-type"].startswith("application/json") and "detail" in sent.json()


# ------------------------------------------------------------------------------------------ local crawl (F-08, F-12)
def test_model_time_hint_is_shown_next_to_the_upload_button_only_when_a_local_model_runs(monkeypatch):
    c = _client(monkeypatch)
    page = c.get(f"/disputes/{DID}/workspace/prepare")
    assert page.status_code == 200
    assert 'data-state="model-time-hint"' in page.text and "This can take one to two minutes while the local model reads the files." in page.text
    from _hosted import configure_hosted
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    configure_hosted(monkeypatch)
    hosted = _client(monkeypatch).get(f"/disputes/{DID}/workspace/prepare")
    assert hosted.status_code == 200 and 'data-state="model-time-hint"' not in hosted.text


def test_link_fix_css_block_is_the_last_thing_in_app_css():
    css = (ROOT / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    tail = css.split("/* LINK-FIX */")
    assert len(tail) == 2 and "scroll-margin-top: 7rem" in tail[1] and tail[1].count("{") == 1      # one rule, at the very end
