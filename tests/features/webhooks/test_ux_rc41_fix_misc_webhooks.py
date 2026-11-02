"""The notices table is a keyboard-reachable scroll region."""
import re

from test_webhooks_routes import PAGE, POST, _web
from webhooks_fakes import HEADERS, FakeClient, dispute_get, event, raw


def test_mu_11_the_scroll_region_is_focusable_named_and_announced_by_a_hint(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", "true")
    web, _fake = _web(monkeypatch, FakeClient(get_answer=dispute_get("WAITING_FOR_BUYER_RESPONSE")))
    assert web.post(POST, content=raw(event(status="OPEN")), headers=HEADERS).status_code == 200
    page = web.get(PAGE).text
    region = re.search(r'<div class="table-scroll"([^>]*)>\s*<table', page, re.S)
    assert region, "the table sits directly inside the scroll region"
    attrs = region.group(1)
    assert 'role="region"' in attrs and 'tabindex="0"' in attrs
    label = re.search(r'aria-label="([^"]+)"', attrs).group(1)
    assert label == "PayPal notices table"
    hint = re.search(r'<p class="table-scroll-hint"[^>]*>(.*?)</p>', page, re.S)
    assert hint and "sideways" in hint.group(1) and hint.start() < page.index('<table class="webhook-events"')
    assert page.count('class="table-scroll"') == 1 and page.count("table-scroll-hint") == 1


def test_mu_11_no_scroll_region_without_events(monkeypatch):
    web, _fake = _web(monkeypatch)
    page = web.get(PAGE).text
    assert 'data-state="no-events"' in page and "table-scroll" not in page
