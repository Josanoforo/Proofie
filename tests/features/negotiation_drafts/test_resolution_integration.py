"""Local seller draft to existing resolution preview, with only GET transport calls."""
import html
import re
import os

from _hosted import authenticated_client
from fastapi.testclient import TestClient


def test_resolution_screen_prepares_two_same_case_drafts_and_refuses_unavailable_offer(monkeypatch, tmp_path):
    """Expected before run: flag-off hides the panel; habitual resolution view consumes current snapshot/action links; two exact local offers are
    saved under the same case, the second enters Proofie's existing review validator, no provider POST occurs, and an
    offer absent from a later snapshot is refused. One read-only fake supplies both cases.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "configured-data"))
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")

    cases = {
        "PP-R-M10-0000001": {"status": "WAITING_FOR_SELLER_RESPONSE", "links": ["make-offer", "send-message"]},
        "PP-R-M10-0000002": {"status": "WAITING_FOR_BUYER_RESPONSE", "links": ["send-message"]},
    }

    class ReadOnlyPayPal:
        def __init__(self):
            self.calls = []

        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET"
            if op == "list_disputes":      # the inbox read some pages make (read-only)
                return {"items": [], "links": []}
            if op == "account_cliff_search":      # the C-D composition has account_cliff on: a read-only account search, nothing to report
                return {"transaction_details": [], "total_pages": 1}
            case_id = path.rstrip("/").split("/")[-1]
            if case_id not in cases:
                raise AssertionError(f"unexpected provider read: {path} ({op})")
            row = cases[case_id]
            return {"dispute_id": case_id, "reason": "UNAUTHORISED", "status": row["status"],
                    "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"value": "40.00", "currency_code": "USD"},
                    "create_time": "2026-10-01T12:00:00Z", "disputed_transactions": [],
                    "links": [{"rel": "self", "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{case_id}", "method": "GET"}]
                              + [{"rel": rel, "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{case_id}/{rel}", "method": "POST"} for rel in row["links"]]}

    fake = ReadOnlyPayPal()
    monkeypatch.delenv("COPILOT_FEATURE_NEGOTIATION_DRAFTS", raising=False)
    flag_off_app = create_app()
    flag_off_app.dependency_overrides[get_client] = lambda: fake
    flag_off = authenticated_client(flag_off_app, client=("127.0.0.1", 12345))
    hidden = flag_off.get("/disputes/PP-R-M10-0000001/resolution")
    assert hidden.status_code == 200 and 'data-state="negotiation-draft"' not in hidden.text

    monkeypatch.setenv("COPILOT_FEATURE_NEGOTIATION_DRAFTS", "true")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))

    page = client.get("/disputes/PP-R-M10-0000001/resolution")
    assert page.status_code == 200 and 'data-state="negotiation-draft"' in page.text

    def csrf(text):
        found = re.search(r'name="csrf_token" value="([^"]+)"', text)
        assert found
        return html.unescape(found.group(1))

    first = client.post("/disputes/PP-R-M10-0000001/resolution/draft", data={
        "csrf_token": csrf(page.text), "kind": "partial_offer", "locale": "en",
        "recipient": "Buyer contact from seller record", "amount": "25.00", "conditions": "After inspection"})
    assert first.status_code == 200 and "Local proposal v1" in first.text
    assert "USD 25.00 as a partial resolution" in first.text
    assert "proposal only; no message or offer was sent" in first.text

    page = client.get("/disputes/PP-R-M10-0000001/resolution")
    assert page.status_code == 200 and "USD 2500 cents" in page.text
    second = client.post("/disputes/PP-R-M10-0000001/resolution/draft", data={
        "csrf_token": csrf(page.text), "kind": "partial_offer", "locale": "en",
        "recipient": "Buyer contact from seller record", "amount": "32.00", "conditions": "After inspection"})
    assert second.status_code == 200 and "Local proposal v2" in second.text
    assert "USD 32.00 as a partial resolution" in second.text
    handoff = re.search(r'<form method="post" action="/disputes/PP-R-M10-0000001/resolution/preview#preview">(.*?)</form>', second.text, re.S)
    assert handoff and 'name="action" value="offer"' in handoff.group(1)
    form = {name: html.unescape(value) for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', handoff.group(1))}
    preview = client.post("/disputes/PP-R-M10-0000001/resolution/preview", data=form, follow_redirects=False)
    assert preview.status_code == 303 and "/resolution/check/" in preview.headers["location"]
    check = client.get(preview.headers["location"])
    assert check.status_code == 200 and "32.00 USD" in check.text
    assert "USD 32.00 as a partial resolution" in check.text and "After inspection" in check.text

    no_offer_page = client.get("/disputes/PP-R-M10-0000002/resolution")
    refused = client.post("/disputes/PP-R-M10-0000002/resolution/draft", data={
        "csrf_token": csrf(no_offer_page.text), "kind": "partial_offer", "locale": "en",
        "recipient": "Buyer contact from seller record", "amount": "25.00", "conditions": "After inspection"})
    assert refused.status_code == 409 and "does not currently allow this action" in refused.text
    from copilot.features.negotiation_drafts import service
    history_path = service.case_history_path("PP-R-M10-0000002")
    history_path.write_text("{corrupt history\n", encoding="utf-8")
    fresh_page = client.get("/disputes/PP-R-M10-0000002/resolution")
    assert fresh_page.status_code == 200
    safe_error = client.post("/disputes/PP-R-M10-0000002/resolution/draft", data={
        "csrf_token": csrf(fresh_page.text), "kind": "information", "locale": "en",
        "recipient": "Buyer contact from seller record", "amount": "", "conditions": "Review recorded facts"})
    assert safe_error.status_code == 409 and 'data-state="draft-refused"' in safe_error.text
    assert "corrupt" in safe_error.text.lower()
    assert safe_error.status_code != 500
    assert all(method == "GET" for method, _, _ in fake.calls)
