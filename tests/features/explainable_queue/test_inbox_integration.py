"""Habitual inbox/detail integration for the explainable queue."""
import hashlib
import sqlite3

from _hosted import authenticated_client
from fastapi.testclient import TestClient
import pytest


def test_existing_inbox_order_gets_queue_explanations_and_verified_late_event(monkeypatch):
    """Expected before run: /disputes keeps Proofie's waiting/due/resolved order while adding per-row deadline and
    local-gap explanations; same-case detail shows one verified late event without replacing the current snapshot.
    PayPal transport is a GET-only fake, and the event comes from Proofie's real verified webhook store.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.db import repo
    from copilot.features.webhooks import store as webhook_store
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS", "true")
    monkeypatch.delenv("COPILOT_FEATURE_TIMELINE", raising=False)      # S3: the timeline is its own flag, off until the test turns it on

    def case(case_id, status, due):
        return {"dispute_id": case_id, "reason": "UNAUTHORISED", "status": status,
                "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"value": "25.00", "currency_code": "USD"},
                "create_time": "2026-10-01T12:00:00Z", "update_time": "2026-10-20T10:00:00Z",
                "seller_response_due_date": due, "links": [], "disputed_transactions": []}

    from datetime import datetime, timedelta, timezone
    soon = lambda days: (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")   # relative: the test must not decay with the calendar
    items = [case("PP-R-DEMO-0000002", "WAITING_FOR_SELLER_RESPONSE", soon(2)),
             case("PP-R-DEMO-0000004", "RESOLVED", None),
             case("PP-R-DEMO-0000003", "WAITING_FOR_SELLER_RESPONSE", None),
             case("PP-R-DEMO-0000001", "WAITING_FOR_SELLER_RESPONSE", soon(1))]
    by_id = {item["dispute_id"]: item for item in items}

    class ReadOnlyFake:
        def __init__(self):
            self.calls = []

        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET"
            if op == "list_disputes":
                return {"items": items, "links": []}
            if op == "show_dispute":
                return by_id[path.rsplit("/", 1)[-1]]
            raise AssertionError(f"unexpected transport operation {op}: {path}")

    fake = ReadOnlyFake()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))

    doc_text = "Seller order note"
    doc_sha = hashlib.sha256(doc_text.encode()).hexdigest()
    doc_id = repo.insert_document(config.db_path(), "PP-R-DEMO-0000002", "order-note.txt", "text/plain",
                                  len(doc_text), doc_sha, doc_text, True)
    package_id = repo.insert_package(config.db_path(), "PP-R-DEMO-0000002", "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "Order details recorded.", "source_ids": [f"doc_{doc_id}"], "basis": "seller_statement"}])
    webhook_store.save_event("evt-case-early", "CUSTOMER.DISPUTE.UPDATED", "dispute", "PP-R-DEMO-0000001",
                             "transmission-1", "a" * 64, "verified", readback_status="WAITING_FOR_SELLER_RESPONSE")
    with pytest.raises(sqlite3.IntegrityError):
        webhook_store.save_event("evt-case-early", "CUSTOMER.DISPUTE.UPDATED", "dispute", "PP-R-DEMO-0000001",
                                 "transmission-duplicate", "b" * 64, "verified", readback_status="OPEN")

    inbox = client.get("/disputes")
    assert inbox.status_code == 200, inbox.text
    order = [inbox.text.index(f'data-dispute="{case_id}"') for case_id in
             ("PP-R-DEMO-0000001", "PP-R-DEMO-0000002", "PP-R-DEMO-0000003", "PP-R-DEMO-0000004")]
    assert order == sorted(order)  # same existing inbox bucket/deadline order; queue adapter adds no competing sort
    assert "Why this case is here:" in inbox.text and "PayPal has not given a response deadline." in inbox.text and "The response deadline is soon." in inbox.text
    assert "No documents are stored in Proofie for this case yet." in inbox.text
    assert "The latest evidence package has no stored statements." not in inbox.text
    assert 'data-dispute="PP-R-DEMO-0000004"' in inbox.text
    closed_row = inbox.text.split('data-dispute="PP-R-DEMO-0000004"', 1)[1].split("</tr>", 1)[0]
    # UX recovery step 5: "Why this case is here" is kept for rows that need the seller; a resolved row says
    # "Nothing to do now" and carries no explanation (the case page still has it)
    assert "Nothing to do now" in closed_row and "Why this case is here" not in closed_row
    needs_row = inbox.text.split('data-dispute="PP-R-DEMO-0000001"', 1)[1].split("</tr>", 1)[0]
    assert "Why this case is here" in needs_row

    detail = client.get("/disputes/PP-R-DEMO-0000001/workspace/records")
    assert detail.status_code == 200, detail.text
    assert 'data-state="case-queue-explanation"' in detail.text
    # S3: without the timeline flag the queue panel still renders, but the PayPal timeline (and its verified-event rows) does not
    assert 'data-state="paypal-timeline"' not in detail.text and 'data-origin="verified_event"' not in detail.text
    monkeypatch.setenv("COPILOT_FEATURE_TIMELINE", "true")
    detail = client.get("/disputes/PP-R-DEMO-0000001/workspace/records")
    assert detail.status_code == 200, detail.text
    assert detail.text.count('data-origin="verified_event"') == 1
    assert "older than the current case snapshot and does not replace its status" in detail.text
    queue_panel = detail.text.split('data-state="case-queue-explanation"', 1)[1].split("</section>", 1)[0]
    # O4: status and the response date are said once, in the case bar above (rulebook v2 V5); the panel keeps the reason, the gaps and the notes
    assert "Verified event notes" in queue_panel
    for raw in (" cents", "CST", "gap(s)", "non-excluded", "local record"):   # R-1: no raw machine text in the panel
        assert raw not in queue_panel, raw
    assert "Respond by" in detail.text and "(Mexico City time)" in detail.text and detail.text.count('data-safety="due-date"') == 1
    assert "Proofie is missing" in queue_panel and "does not check that uploaded evidence is genuine" in queue_panel
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
