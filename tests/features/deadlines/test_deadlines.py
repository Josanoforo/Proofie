"""Due ordering, core calculations, one include and shared GET budget; synthetic only."""
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from _hosted import authenticated_client, configure_hosted

from copilot import config
from copilot.core.disputes import humanize_deadline
from copilot.features.deadlines.service import prepare, read, urgency, WARNING_DAYS
from copilot.features.events.rate import Budget, BudgetUnavailable, ReadClient
from copilot.web.routes_disputes import templates, get_client

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[3]


def item(n, offset=None, status="WAITING_FOR_SELLER_RESPONSE"):
    # Only a case that waits for the seller has a response due; the fixtures default to that status
    body = {"dispute_id": "PP-R-DEMO-" + str(n), "status": status}
    if offset is not None:
        body["seller_response_due_date"] = (NOW + timedelta(hours=offset)).isoformat()
    return body


def test_sorting_is_only_owned_page_and_normalization_computes_days():
    values = [item(1), item(2, 120), item(3, -26), item(4, 50)]
    result = prepare(values, NOW)
    assert [r["norm"]["dispute_id"] for r in result] == ["PP-R-DEMO-3", "PP-R-DEMO-4", "PP-R-DEMO-2", "PP-R-DEMO-1"]
    assert [r["norm"]["remaining_days"] for r in result] == [-1, 2, 5, None]
    assert [r["warning"] for r in result] == [True, True, False, False]
    assert values[0]["dispute_id"] == "PP-R-DEMO-1"  # input/inbox order never mutated


def test_offsets_sort_by_aware_time():
    a, b = item(1), item(2)
    a["seller_response_due_date"] = "2026-10-08T01:00:00+02:00"
    b["seller_response_due_date"] = "2026-10-07T20:00:00-06:00"
    assert prepare([b, a], NOW)[0]["norm"]["dispute_id"] == a["dispute_id"]


@pytest.mark.parametrize("raw", [None, "broken", "2026-10-08T12:00:00"])
def test_missing_or_unverified_date_reuses_core_s2_text(raw):
    value = item(1)
    if raw is not None:
        value["seller_response_due_date"] = raw
    row = prepare([value], NOW)[0]
    assert row["deadline_text"] == humanize_deadline(row["norm"]) == "No verified deadline available"
    assert not row["warning"] and row["norm"]["remaining_days"] is None


@pytest.mark.parametrize("hours,warning", [(-1, True), (0, True), (71, True), (72, False), (73, False)])
def test_visible_threshold_is_under_three_days(hours, warning):
    row = prepare([item(1, hours)], NOW)[0]
    assert WARNING_DAYS == 3 and row["warning"] is warning


class Fake:
    def __init__(self, foreign=False):
        self.calls, self.foreign = [], foreign
    def call(self, method, path, op, **kwargs):
        assert method == "GET" and op == "list_disputes"
        self.calls.append(path)
        if len(self.calls) == 1:
            href = "https://example.com/v1/customer/disputes?page=2" if self.foreign else "/v1/customer/disputes?page=2"
            return {"items": [item(1, 50)], "links": [{"rel": "next", "href": href}]}
        return {"items": [item(2), item(3, 100)], "links": []}


def test_paginated_reader_uses_shared_get_budget_and_core_next_link():
    fake = Fake()
    rows, pages = read(fake, NOW, Budget(clock=lambda: 0))
    assert pages == 2 and len(rows) == 3 and fake.calls[1].endswith("?page=2")
    fake = Fake()
    with pytest.raises(BudgetUnavailable):
        read(fake, NOW, Budget(limit=1, clock=lambda: 0))
    assert len(fake.calls) == 1


def test_foreign_pagination_link_fails_before_transport():
    fake = Fake(foreign=True)
    with pytest.raises(ValueError):
        read(fake, NOW, Budget(clock=lambda: 0))
    assert len(fake.calls) == 1


@pytest.mark.parametrize("href", ["http://api-m.sandbox.paypal.com/v1/customer/disputes", "https://example.com/case", "//example.com/case", "https://api-m.sandbox.paypal.com:444/case", "https://api-m.sandbox.paypal.com/case#fragment"])
def test_shared_reader_rejects_unsafe_absolute_links(href):
    fake = Fake()
    with pytest.raises(ValueError):
        ReadClient(fake, Budget(clock=lambda: 0)).call("GET", href, "list_disputes")
    assert fake.calls == []


def test_sandbox_absolute_get_link_is_allowed():
    fake = Fake()
    ReadClient(fake, Budget(clock=lambda: 0)).call("GET", "https://api-m.sandbox.paypal.com/v1/customer/disputes", "list_disputes")
    assert len(fake.calls) == 1


def test_include_has_no_extra_get_and_threshold_matches_service():
    # The banner lists each case under the threshold (the same urgency() the pages use), amber, one link per case
    rows = prepare([item(1, 71), item(2), item(3, 72)], NOW)
    banner = templates.env.get_template("features/deadlines/_banner.html").render(rows=rows, checked_at=NOW.isoformat())
    assert "Respond soon" in banner and "PP-R-DEMO-1: respond by" in banner and 'href="/disputes/PP-R-DEMO-1"' in banner
    assert "PP-R-DEMO-2" not in banner and "PP-R-DEMO-3" not in banner and 'href="/deadlines"' in banner
    assert "banner-warn" in banner and "See all due dates" in banner
    empty = templates.env.get_template("features/deadlines/_banner.html").render(rows=prepare([item(1)], NOW))
    assert "deadline-inbox" not in empty
    assert [urgency(r["norm"], NOW) for r in prepare([item(1, 71), item(2, 72)], NOW)] == ["soon", "ok"] and WARNING_DAYS == 3


OLD_FIXTURE = "disputes_list-baseline-0.html"      # history: the template before the inbox redesign
NEW_FIXTURE = "disputes_list-frozen-1.html"                                    # frozen copy of the redesigned inbox


def _blob_sha1(text):
    """Git blob id of the text with LF line ends (the checkout may use CRLF)."""
    import hashlib
    data = text.replace(chr(13) + chr(10), chr(10)).encode("utf-8")
    return hashlib.sha1(b"blob %d" % len(data) + bytes([0]) + data).hexdigest()


def test_flag_off_inbox_renders_exact_main_template_bytes(monkeypatch):
    monkeypatch.delenv("COPILOT_FEATURE_DEADLINES", raising=False)
    # This frozen main-template baseline excludes independent fulfilment/replay navigation.
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "false")
    monkeypatch.setenv("COPILOT_FEATURE_REPLAY", "false")
    values = {"rows": [], "account_cliff": None, "checked_at": "DEMO-FIXED", "pages": 1}
    fixtures = Path(__file__).parent / "fixtures"
    # History stays asserted: the old frozen copy is still there, still renders, and the page it froze was the one
    # headed "Disputes" with the empty-state banner.
    old = (fixtures / OLD_FIXTURE).read_text(encoding="utf-8")
    old_page = templates.env.from_string(old).render(**values)
    assert "<h1>Disputes</h1>" in old_page and "No disputes need your attention right now" in old_page
    # Re-baselined with the inbox redesign: the new frozen copy is pinned by its git blob id and the live
    # template must render the same bytes with every flag off.
    new = (fixtures / NEW_FIXTURE).read_text(encoding="utf-8")
    assert _blob_sha1(new) == "7969f7f96b83f87f11a3a3ad0cb7d641584296b7"
    current = (ROOT / "copilot/web/templates/disputes_list.html").read_text(encoding="utf-8")
    # One declared flags-off edit since that frozen copy (UX v3 / D-68, reading A): the inbox purpose line under the heading.
    # The frozen copy and its blob id are untouched; the edit is removed from the live render before the byte comparison.
    purpose = '<p data-state="inbox-purpose">Prepare and review evidence; you decide whether to approve and send.</p>' + chr(10)
    live = templates.env.from_string(current).render(**values)
    assert live.count(purpose) == 1
    assert live.replace(purpose, "", 1).encode() == templates.env.from_string(new).render(**values).encode()
    assert "<h1>All disputes</h1>" in templates.env.from_string(current).render(**values)
    assert current.count('{% include "features/deadlines/_banner.html" %}') == 1
    assert 'feature_enabled("deadlines")' in current.split('{% include "features/deadlines/_banner.html" %}')[0].splitlines()[-1]


def test_page_renders_s2_warning_without_meta_refresh_or_feature_script():
    rows = prepare([item(1, 50), item(2), item(3, status="UNDER_REVIEW")], NOW)
    html = templates.env.get_template("features/deadlines/watch.html").render(rows=rows, pages=1, checked_at=NOW.isoformat(),
                   warning_days=WARNING_DAYS, read_error=None)
    assert "http-equiv" not in html                     # no automatic reload of a page the seller is reading
    assert "No verified deadline available" in html and "less than 3 days left" in html and "deadline-due-soon" in html
    assert "No response due from you" in html and "Outside the warning threshold" not in html and "Review now" not in html
    assert "Refresh from PayPal" in html and "PayPal was read at" in html
    source = (ROOT / "copilot/web/templates/features/deadlines/watch.html").read_text(encoding="utf-8")
    assert "<script" not in source


def test_hosted_route_reads_fake_only_and_creates_no_owned_table(tmp_path, monkeypatch):
    import httpx
    from copilot.app import create_app
    from copilot.core.disputes import SandboxClient
    import copilot.features.deadlines.routes as routes
    monkeypatch.setenv("COPILOT_FEATURE_DEADLINES", "true")
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    configure_hosted(monkeypatch)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "unused.db")
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external call")
    monkeypatch.setattr(SandboxClient, "call", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    fake = Fake()
    original = read
    monkeypatch.setattr(routes, "read", lambda client, now: original(client, now, Budget(clock=lambda: 0)))
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    result = authenticated_client(app, client=("127.0.0.1", 12345)).get("/deadlines")
    assert result.status_code == 200 and len(fake.calls) == 2 and "No verified deadline available" in result.text
    assert not (tmp_path / "unused.db").exists()


def test_budget_deferral_is_visible_and_does_not_claim_an_empty_paypal_read(monkeypatch, web_mode):
    from copilot.app import create_app
    import copilot.features.deadlines.routes as routes
    monkeypatch.setenv("COPILOT_FEATURE_DEADLINES", "true")
    def unavailable(*args):
        raise BudgetUnavailable()
    monkeypatch.setattr(routes, "read", unavailable)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    result = authenticated_client(app, client=("127.0.0.1", 12345)).get("/deadlines")
    assert result.status_code == 503 and "Nothing was changed. Try again in a minute with Refresh from PayPal." in result.text
    assert "No disputes were returned" not in result.text and "PayPal was read at" not in result.text
    assert result.headers["cache-control"] == "no-store" and "http-equiv" not in result.text
