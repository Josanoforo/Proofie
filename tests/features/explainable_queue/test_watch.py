"""D-101 r2 (R2-D): the inbox watch. Read-only route, opt-in box, default on only when the inbox is empty."""
import os
from pathlib import Path

import pytest

from _hosted import authenticated_client
from copilot import config
from copilot.app import create_app
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[3]
DID = "PP-R-DEMO-0000001"
BODY = {"dispute_id": DID, "status": "WAITING_FOR_SELLER_RESPONSE", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "dispute_life_cycle_stage": "INQUIRY", "create_time": "2026-10-21T00:00:00Z",
        "dispute_amount": {"currency_code": "USD", "value": "25.00"},
        "disputed_transactions": [{"seller_transaction_id": "DEMO-TXN-000001"}], "evidences": [], "links": []}


class Listing:
    """A fake PayPal that only answers GET: the list (the ids given), the dispute read, and empty context reads."""

    def __init__(self, ids):
        self.ids, self.calls = ids, []

    def call(self, method, path, operation, **kwargs):
        self.calls.append((method, operation))
        assert method == "GET", method
        if operation == "list_disputes":
            return {"items": [{**BODY, "dispute_id": i} for i in self.ids], "links": []}
        if operation in ("show_dispute", "dispute_detail"):
            return BODY
        if operation in ("context_transaction_search", "account_cliff_search"):
            return {"transaction_details": []}
        if operation == "context_tracking":
            return {"trackers": []}
        if operation == "recovery_get":
            return {"evidences": []}
        raise AssertionError(f"unexpected operation: {operation}")


def _web(monkeypatch, tmp_path, ids, flag="true"):
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    if flag is not None:
        monkeypatch.setenv("COPILOT_FEATURE_INBOX_WATCH", flag)
    config.set_db_path_override(tmp_path / "watch.db")
    config.set_uploads_dir_override(tmp_path / "uploads")
    fake = Listing(ids)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return authenticated_client(app, client=("127.0.0.1", 12345)), fake


@pytest.fixture(autouse=True)
def _reset():
    yield
    config.set_db_path_override(None)
    config.set_uploads_dir_override(None)


def test_the_route_lists_only_the_ids_the_page_does_not_have_and_reads_only(monkeypatch, tmp_path):
    web, fake = _web(monkeypatch, tmp_path, ["PP-R-AAA-0001", "PP-R-BBB-0002"])
    r = web.get("/inbox/watch?seen=PP-R-AAA-0001")
    assert r.status_code == 200 and r.json()["new"] == ["PP-R-BBB-0002"] and r.json()["listed"] == 2
    assert r.headers["cache-control"] == "no-store"
    assert web.get("/inbox/watch?seen=PP-R-AAA-0001,PP-R-BBB-0002").json()["new"] == []
    assert {method for method, _ in fake.calls} == {"GET"}


def test_an_empty_or_invalid_seen_list_means_everything_listed_is_new(monkeypatch, tmp_path):
    web, _fake = _web(monkeypatch, tmp_path, ["PP-R-AAA-0001"])
    assert web.get("/inbox/watch").json()["new"] == ["PP-R-AAA-0001"]
    assert web.get("/inbox/watch?seen=<script>,../x,PP R").json()["new"] == ["PP-R-AAA-0001"]


def test_an_empty_inbox_offers_the_box_unticked_too(monkeypatch, tmp_path):
    web, _fake = _web(monkeypatch, tmp_path, [])
    page = web.get("/disputes")
    assert page.status_code == 200 and 'data-state="inbox-watch"' in page.text
    assert 'id="inbox-watch-toggle">' in page.text and "checked" not in page.text.split('id="inbox-watch-toggle"')[1].split(">")[0]
    assert 'data-seen=""' in page.text and "Tick the box" in page.text


def test_the_box_is_never_ticked_by_the_server_whatever_the_page_holds(monkeypatch, tmp_path):
    source = (ROOT / "copilot/web/templates/features/explainable_queue/_watch.html").read_text(encoding="utf-8")
    assert "checked" not in source.replace("is never ticked", "")        # opt-in everywhere, the hosted demo included
    for ids in ([], [DID]):
        web, _fake = _web(monkeypatch, tmp_path, ids)
        tag = web.get("/disputes").text.split('id="inbox-watch-toggle"')[1].split(">")[0]
        assert "checked" not in tag


def test_an_inbox_with_cases_lists_them_and_leaves_the_box_unticked(monkeypatch, tmp_path):
    web, _fake = _web(monkeypatch, tmp_path, [DID])
    page = web.get("/disputes")
    assert page.status_code == 200 and f'data-seen="{DID}"' in page.text
    assert 'id="inbox-watch-toggle" checked' not in page.text and "Watch for new cases" in page.text


def test_the_script_is_shipped_and_stays_opt_in(monkeypatch, tmp_path):
    js = (ROOT / "copilot/web/static/app.js").read_text(encoding="utf-8")
    assert "inbox-watch-toggle" in js and "if (!toggle.checked) { return; }" in js and "data-max" in js
    assert "method" not in js[js.index("Inbox watch"):].split("fetch(")[1].split(")")[0]      # a plain GET, nothing posted


def test_with_the_flag_off_there_is_no_box_and_no_route(monkeypatch, tmp_path):
    web, _fake = _web(monkeypatch, tmp_path, [DID], flag="false")
    assert 'data-state="inbox-watch"' not in web.get("/disputes").text
    assert web.get("/inbox/watch").status_code == 404
