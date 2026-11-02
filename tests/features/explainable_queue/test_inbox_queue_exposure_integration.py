"""Habitual /disputes route keeps its real task/deadline order with a read-only snapshot fake."""
from datetime import datetime, timedelta, timezone

from _hosted import authenticated_client


def test_habitual_inbox_routes_cases_by_existing_deadline_and_next_step(monkeypatch, tmp_path):
    """Expected: due-first order for seller-response cases (overdue, current, partial, undated), then blocked and
    resolved; actual inbox next-step links reflect each row status. Every transport call is GET-only and no write path
    or model/provider is available. The reusable exposure helper is tested independently on this same row shape.
    """
    import os
    from copilot import config
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "inbox.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_EXPLAINABLE_QUEUE", "true")
    now = datetime.now(timezone.utc)
    due = lambda delta: (now + delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    def case(case_id, status, deadline, amount="12.50", currency="USD"):
        row = {"dispute_id": case_id, "reason": "UNAUTHORISED", "status": status,
               "dispute_life_cycle_stage": "INQUIRY", "create_time": "2026-10-01T12:00:00Z",
               "update_time": "2026-10-30T12:00:00Z", "seller_response_due_date": deadline,
               "links": [], "disputed_transactions": []}
        if amount is not None:
            row["dispute_amount"] = {"value": amount, "currency_code": currency}
        return row

    cases = [case("PP-R-OLA2-0000001", "WAITING_FOR_SELLER_RESPONSE", due(timedelta(days=-1))),
             case("PP-R-OLA2-0000002", "WAITING_FOR_SELLER_RESPONSE", due(timedelta(days=1))),
             case("PP-R-OLA2-0000003", "WAITING_FOR_SELLER_RESPONSE", due(timedelta(days=3)), amount=None),
             case("PP-R-OLA2-0000004", "WAITING_FOR_SELLER_RESPONSE", None),
             case("PP-R-OLA2-0000005", "WAITING_FOR_BUYER_RESPONSE", due(timedelta(days=2))),
             case("PP-R-OLA2-0000006", "RESOLVED", None, "99.99")]

    class ReadOnlyFake:
        def __init__(self):
            self.calls = []

        def call(self, method, path, op, **kwargs):
            self.calls.append((method, op))
            assert method == "GET"
            assert op == "list_disputes"
            return {"items": cases, "links": []}

    fake = ReadOnlyFake()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))
    response = client.get("/disputes")
    assert response.status_code == 200, response.text
    html = response.text
    ids = ["PP-R-OLA2-0000001", "PP-R-OLA2-0000002", "PP-R-OLA2-0000003", "PP-R-OLA2-0000004",
           "PP-R-OLA2-0000005", "PP-R-OLA2-0000006"]
    positions = [html.index(f'data-dispute="{case_id}"') for case_id in ids]
    assert positions == sorted(positions)
    assert 'href="/disputes/PP-R-OLA2-0000001"' in html and "Respond to this dispute" in html
    partial = html.split('data-dispute="PP-R-OLA2-0000003"', 1)[1].split("</tr>", 1)[0]
    assert 'href="/disputes/PP-R-OLA2-0000003"' in partial and "Respond to this dispute" in partial
    assert "amount: not available" in partial and "No documents are stored" in partial
    blocked = html.split('data-dispute="PP-R-OLA2-0000005"', 1)[1].split("</tr>", 1)[0]
    assert "Nothing to do now" in blocked and "Respond to this dispute" not in blocked
    resolved = html.split('data-dispute="PP-R-OLA2-0000006"', 1)[1].split("</tr>", 1)[0]
    assert "Nothing to do now" in resolved
    assert fake.calls and all(method == "GET" for method, _ in fake.calls)
