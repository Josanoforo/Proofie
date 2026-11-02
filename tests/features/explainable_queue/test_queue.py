import pytest

from copilot.features.explainable_queue import service


def test_latest_snapshot_event_timeline_deadline_and_resolved_priority():
    import copy
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    data = copy.deepcopy(service.EXAMPLE)
    due = (datetime.now(timezone.utc) + timedelta(hours=30)).replace(second=0, microsecond=0)   # relative: the sample's own date moves into the past
    next(c for c in data["cases"] if c["id"] == "CASE-NEAR")["deadline"] = due.isoformat()
    rows = service.build(data)
    by = {r["id"]: r for r in rows}
    near = by["CASE-NEAR"]
    assert near["deadline_state"] == "soon"
    assert near["deadline_cdmx"].startswith(due.astimezone(ZoneInfo("America/Mexico_City")).strftime("%Y-%m-%d %H:%M"))
    assert [e["id"] for e in near["timeline"]] == ["evt-late", "evt-1"]
    assert near["timeline"][0]["late"] is True
    assert by["CASE-DONE"]["next_action"].startswith("No response")
    assert by["CASE-UNKNOWN"]["deadline_state"] == "unknown"
    assert rows[-1]["resolved"] is True


def test_duplicate_snapshots_select_latest_and_currency_not_compared():
    data = {"cases": [{"id": "A", "observed_at": "2026-01-01T00:00:00Z", "deadline": "2026-01-02T00:00:00Z", "status": "OPEN", "amount_cents": 1, "currency": "USD", "source":"recorded snapshot"}, {"id": "A", "observed_at": "2026-01-01T01:00:00Z", "deadline": "2026-01-03T00:00:00Z", "status": "OPEN", "amount_cents": 999999, "currency": "JPY", "source":"recorded snapshot"}, {"id": "B", "observed_at": "2026-01-01T00:00:00Z", "deadline": "2026-01-02T00:00:00Z", "status": "OPEN", "amount_cents": 1, "currency": "USD", "source":"recorded snapshot"}], "events": []}
    rows = service.build(data)
    assert len(rows) == 2
    assert next(r for r in rows if r["id"] == "A")["currency"] == "JPY"
    assert [r["id"] for r in rows] == ["B", "A"]  # B has the earlier deadline; amounts/currencies are not compared.


def test_absolute_deadline_order_ignores_different_snapshot_observation_times():
    data = {"cases": [
        {"id": "A", "observed_at": "2026-10-20T00:00:00Z", "deadline": "2026-10-24T00:00:00Z", "status": "OPEN", "source": "recorded snapshot"},
        {"id": "B", "observed_at": "2026-10-24T00:00:00Z", "deadline": "2026-10-25T00:00:00Z", "status": "OPEN", "source": "recorded snapshot"},
    ], "events": []}
    rows = service.build(data)
    assert [row["id"] for row in rows] == ["A", "B"]
    assert rows[0]["remaining_seconds"] < rows[1]["remaining_seconds"]


@pytest.mark.parametrize("data", [{"cases": ["x"], "events": []}, {"cases": [{"id": "x", "observed_at": "no zone"}], "events": []}, {"cases": [{"id": "x", "observed_at": "2026-01-01T00:00:00Z", "amount_cents": 1.5}], "events": []}, {"cases": [], "events": [{"id": "e", "case_id": "x"}]}])
def test_malformed_records_are_rejected(data):
    with pytest.raises(ValueError):
        service.build(data)
