"""Synthetic adversarial timelines, crash/replay, rate and privacy contracts; no real transport."""
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pytest

from copilot import config
from copilot.features.events.driver import Driver
from copilot.features.events.evlog import EventLogClient, Puller, metadata, LOG_FACTS, EVENT_TYPES, IncompleteWindow
from copilot.features.events.machine import snapshot, choose, eligible
from copilot.features.events.rate import Budget, BudgetUnavailable, ReadClient, SHARED_BUDGET
from copilot.features.events.store import Store, view

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
DID = "PP-R-DEMO-0000001"
EVENT = json.loads((Path(__file__).parents[3] / "copilot/features/events/fixtures/dispute-event.json").read_text(encoding="utf-8"))


def body(seconds=0, status="OPEN", did=DID, due=None):
    value = {"dispute_id": did, "status": status, "update_time": (NOW + timedelta(seconds=seconds)).isoformat()}
    if due is not None:
        value["seller_response_due_date"] = due
    return value


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_EVENTS", "true")
    return Store(tmp_path / "events.db")


class Fake:
    def __init__(self, detail=None, events=None):
        self.detail, self.events, self.calls, self.error = detail or body(), events or [], [], None
    def call(self, method, path, op, **kwargs):
        assert method == "GET"
        self.calls.append((path, op))
        if self.error:
            raise self.error
        if op == "evlog_events_list":
            kind = parse_qs(urlsplit(path).query)["event_type"][0]
            return {"events": [copy.deepcopy(e) for e in self.events if e["event_type"] == kind]}
        return {**self.detail, "dispute_id": path.rsplit("/", 1)[-1]}


def driver(store, client):
    return Driver(store, client, Budget(clock=lambda: 0), clock=lambda: NOW, monotonic=lambda: 0)


def test_versions_only_accept_backwards_status_and_ignore_stale():
    one = snapshot(body(0, "UNDER_REVIEW"), "sweep", NOW)
    two = snapshot(body(19, "WAITING_FOR_SELLER_RESPONSE"), "log", NOW)
    selected, result = choose(one, two)
    assert result == "accepted" and selected.status == "WAITING_FOR_SELLER_RESPONSE" and selected.review_version is None
    assert choose(two, one) == (two, "stale")
    assert choose(two, two) == (two, "duplicate")
    different = snapshot({**body(19), "summary": "DEMO other producer disagreed"}, "webhook", NOW)
    assert choose(two, different) == (two, "conflict")


@pytest.mark.parametrize("change", [{"update_time": "2026-10-07T12:00:00"}, {"update_time": "broken"}, {"dispute_id": "foreign"}, {"status": "arbitrary prose"}])
def test_invalid_versions_or_ids_cannot_advance(change):
    with pytest.raises(ValueError):
        snapshot({**body(), **change}, "sweep", NOW, DID)


def test_offset_versions_order_by_instant_not_lexical_text():
    a = snapshot(body(), "sweep", NOW)
    b = snapshot({**body(), "update_time": "2026-10-07T06:00:00-06:00"}, "log", NOW)
    assert choose(a, b)[1] == "conflict"  # same instant, different body hash; never replaces


def test_under_review_persists_over_restart_and_elapsed_time(store):
    store.commit_get(snapshot(body(4, "UNDER_REVIEW"), "sweep", NOW))
    reopened = Store(store.path)
    old = reopened.get(DID)
    assert old.review_version == old.version
    reopened.commit_get(snapshot(body(4, "UNDER_REVIEW"), "sweep", NOW + timedelta(hours=1)))
    assert reopened.get(DID).review_version == old.version
    reopened.commit_get(snapshot(body(19, "WAITING_FOR_BUYER_RESPONSE"), "sweep", NOW))
    assert reopened.get(DID).review_version is None


def test_422_timeline_never_ends_review_or_permits_an_automatic_resend(store):
    # Synthetic sequence: first GET observes review, second write is refused, later GET changes version.
    store.commit_get(snapshot(body(4, "UNDER_REVIEW"), "sweep", NOW))
    review = store.get(DID).review_version
    class Refused(Exception):
        status = 422
    fake = Fake(body(4, "UNDER_REVIEW"))
    fake.error = Refused()
    with pytest.raises(Refused):
        driver(store, fake).get(DID, "sweep")
    assert Store(store.path).get(DID).review_version == review
    with pytest.raises(ValueError):
        ReadClient(fake).call("POST", "/demo", "demo-refused-write")
    fake.error = None
    fake.detail = body(19, "WAITING_FOR_BUYER_RESPONSE")
    driver(store, fake).get(DID, "sweep")
    assert store.get(DID).review_version is None and len(fake.calls) == 2


@pytest.mark.parametrize("landed", [True, False])
def test_lost_ack_requires_get_and_human_and_never_resends(store, landed):
    store.commit_get(snapshot(body(), "sweep", NOW))
    store.mark_unknown(DID, "UNDER_REVIEW")
    reopened = Store(store.path)
    with pytest.raises(ValueError):
        reopened.human_review(DID, reopened.get(DID).version)
    fake = Fake(body(19, "UNDER_REVIEW") if landed else body())
    reader = driver(reopened, fake)
    reader.get(DID, "sweep")
    unknown = reopened.unknown(DID)
    assert unknown["human_reviewed"] == 0
    assert unknown["observation"] == ("observed_match" if landed else "not_confirmed")
    reopened.human_review(DID, unknown["observed_version"])
    assert reopened.unknown(DID)["human_reviewed"] == 1
    assert len(fake.calls) == 1  # GET only; unchanged read does not prove a write failed


def test_human_review_rejects_obsolete_get_after_new_version(store):
    store.commit_get(snapshot(body(), "sweep", NOW))
    store.mark_unknown(DID, "UNDER_REVIEW")
    store.commit_get(snapshot(body(4, "UNDER_REVIEW"), "sweep", NOW))
    old = store.unknown(DID)["observed_version"]
    store.commit_get(snapshot(body(19, "WAITING_FOR_BUYER_RESPONSE"), "sweep", NOW))
    with pytest.raises(ValueError):
        store.human_review(DID, old)


def receiver_table(store):
    with store.connection() as c:
        c.execute("CREATE TABLE webhook_events (id INTEGER PRIMARY KEY, event_id TEXT,event_type TEXT,dispute_id TEXT,received_at TEXT,body_sha256 TEXT,verification TEXT,readback_status TEXT)")
        c.execute("INSERT INTO webhook_events VALUES (1,?,?,?,?,'DEMO-HASH','verified','RESOLVED')", (EVENT["id"], EVENT["event_type"], DID, NOW.isoformat()))


def test_receiver_event_payload_and_readback_status_never_advance_state(store):
    receiver_table(store)
    fake = Fake(body(0, "OPEN"), [EVENT])
    reader = driver(store, fake)
    assert reader.receiver() == 1
    assert store.get(DID).status == "OPEN" and store.meta("receiver_row") == "1"
    assert reader.pull() == 1  # duplicate log id across the producer spaces, no second detail GET
    assert sum(op == "events_detail_get" for _, op in fake.calls) == 1
    assert store.meta("duplicate_events") == "1"


def test_failed_receiver_get_retains_pending_row_for_restart(store):
    receiver_table(store)
    fake = Fake()
    fake.error = RuntimeError("DEMO lost read")
    with pytest.raises(RuntimeError):
        driver(store, fake).receiver()
    assert store.meta("receiver_row") is None and not store.has_event(EVENT["id"])
    fake.error = None
    assert driver(Store(store.path), fake).receiver() == 1


def test_watermark_reads_more_than_100_and_replays_old_verified_replacement(store):
    receiver_table(store)
    with store.connection() as c:
        c.execute("UPDATE webhook_events SET verification='failed' WHERE id=1")
        for n in range(2, 104):
            c.execute("INSERT INTO webhook_events VALUES (?,?,?,?,?,'DEMO-HASH','verified','RESOLVED')", (n, "DEMO-EVENT-" + str(n), EVENT["event_type"], DID, NOW.isoformat()))
    store.set_meta("receiver_row", 103)
    assert len(store.receiver_rows()) == 102
    with store.connection() as c:
        c.execute("UPDATE webhook_events SET verification='verified' WHERE id=1")
    assert store.receiver_rows()[0]["id"] == 1


def test_transaction_rolls_back_snapshot_event_and_watermark_together(store):
    receiver_table(store)
    with store.connection() as c:
        c.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON event_log BEGIN SELECT RAISE(ABORT,'DEMO crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        driver(store, Fake()).receiver()
    assert store.get(DID) is None and not store.has_event(EVENT["id"]) and store.meta("receiver_row") is None


def test_due_order_nonterminal_and_known_appeal_window(store):
    for n, due, status in ((1, 5, "OPEN"), (2, 1, "UNDER_REVIEW"), (3, 2, "RESOLVED"), (4, None, "OPEN")):
        did = "PP-R-DEMO-" + str(n)
        raw = body(did=did, status=status, due=(NOW + timedelta(days=due)).isoformat() if due else None)
        store.commit_get(snapshot(raw, "sweep", NOW))
    fake = Fake()
    assert driver(store, fake).sweep() == 3
    assert [p.rsplit("/", 1)[-1] for p, _ in fake.calls] == ["PP-R-DEMO-2", "PP-R-DEMO-1", "PP-R-DEMO-4"]
    appeal = snapshot({**body(1, "RESOLVED"), "appeal_response_due_date": (NOW + timedelta(days=1)).isoformat()}, "sweep", NOW)
    assert eligible([appeal], NOW) == [appeal]
    assert eligible([appeal], NOW + timedelta(days=2)) == []


def test_local_core_case_references_are_discovered_without_core_writes(store):
    with store.connection() as c:
        c.execute("CREATE TABLE documents (dispute_id TEXT)")
        c.execute("INSERT INTO documents VALUES (?)", (DID,))
    fake = Fake()
    assert driver(store, fake).sweep() == 1 and store.get(DID).source == "sweep"
    with store.connection() as c:
        assert c.execute("SELECT * FROM documents").fetchall()[0][0] == DID


def test_leases_refuse_second_reader_and_recover_after_expiry(store):
    with store.lease("get:" + DID, 0):
        with pytest.raises(RuntimeError):
            with Store(store.path).lease("get:" + DID, 1):
                pytest.fail("second reader entered")
        with Store(store.path).lease("get:" + DID, 181):
            pass
    with store.lease("get:" + DID, 182):
        pass


def test_shared_budget_refuses_51st_and_rolling_refill_burst():
    clock = [0.0]
    budget = Budget(clock=lambda: clock[0])
    a, b = ReadClient(Fake(), budget), ReadClient(Fake(), budget)
    accepted = 0
    for n in range(51):
        try:
            (a if n % 2 else b).call("GET", "/demo", "demo")
            accepted += 1
        except BudgetUnavailable:
            assert n >= 45
    assert accepted == 45
    clock[0] = 59
    with pytest.raises(BudgetUnavailable):
        a.call("GET", "/demo", "demo")
    clock[0] = 60
    a.call("GET", "/demo", "demo")
    assert ReadClient(Fake()).budget is SHARED_BUDGET
    with pytest.raises(ValueError):
        a.call("POST", "/demo", "demo")


def test_429_full_jitter_defers_all_producers_no_retry():
    class Refused(Exception):
        status = 429
    fake, clock, bounds = Fake(), [0.0], []
    fake.error = Refused()
    def jitter(low, high):
        bounds.append((low, high))
        return high / 2
    budget = Budget(clock=lambda: clock[0], jitter=jitter)
    with pytest.raises(Refused):
        ReadClient(fake, budget).call("GET", "/demo", "demo")
    with pytest.raises(BudgetUnavailable):
        ReadClient(Fake(), budget).call("GET", "/demo", "demo")
    assert bounds == [(0, 2)] and len(fake.calls) == 1
    clock[0] = 1
    ReadClient(Fake(), budget).call("GET", "/demo", "demo")


def test_empty_event_log_and_overlap_watermark_not_retention_claim(store):
    fake = Fake()
    reader = driver(store, fake)
    assert reader.pull() == 0 and store.meta("log_end") == NOW.isoformat()
    later = NOW + timedelta(days=2)
    reader.clock = lambda: later
    reader.pull()
    query = parse_qs(urlsplit(fake.calls[3][0]).query)
    assert datetime.fromisoformat(query["start_time"][0]) == NOW - timedelta(minutes=10)
    assert query["end_time"] == [later.isoformat()] and query["page_size"] == ["100"]
    assert set(query) == {"start_time", "end_time", "page_size", "event_type"}
    assert store.meta("retention_coverage") == "unknown" and store.meta("log_gap_seconds") == str(int(timedelta(days=2).total_seconds()))


def test_full_window_splits_without_offset_and_dedupes_boundary(store):
    class Full(Fake):
        def call(self, method, path, op, **kwargs):
            if op == "evlog_events_list":
                assert method == "GET"
                self.calls.append((path, op))
                q = parse_qs(urlsplit(path).query)
                duration = (datetime.fromisoformat(q["end_time"][0]) - datetime.fromisoformat(q["start_time"][0])).total_seconds()
                return {"events": [copy.deepcopy(EVENT)] * 100 if duration > 300 else [copy.deepcopy(EVENT)]}
            return super().call(method, path, op, **kwargs)
    fake = Full()
    driver(store, fake).pull()
    assert store.has_event(EVENT["id"])
    assert sum(op == "events_detail_get" for _, op in fake.calls) == 1
    assert all("offset" not in p and "move_to" not in p for p, _ in fake.calls)


def test_incomplete_or_budget_exhausted_pull_keeps_last_successful_watermark(store):
    class Full(Fake):
        def call(self, method, path, op, **kwargs):
            assert method == "GET"
            return {"events": [EVENT] * 100}
    store.set_meta("log_end", (NOW - timedelta(minutes=1)).isoformat())
    previous = store.meta("log_end")
    with pytest.raises(IncompleteWindow):
        driver(store, Full()).pull()
    assert store.meta("log_end") == previous
    d = Driver(store, Fake(), Budget(limit=1, clock=lambda: 0), clock=lambda: NOW)
    with pytest.raises(BudgetUnavailable):
        d.pull()
    assert store.meta("log_end") == previous


def test_failed_detail_in_log_never_commits_window_or_event(store):
    class Bad(Fake):
        def call(self, method, path, op, **kwargs):
            if op == "events_detail_get":
                raise RuntimeError("DEMO read failed")
            return super().call(method, path, op, **kwargs)
    with pytest.raises(RuntimeError):
        driver(store, Bad(events=[EVENT])).pull()
    assert store.meta("log_end") is None and not store.has_event(EVENT["id"])


def test_pending_log_get_survives_restart_even_when_remote_event_is_no_longer_listed(store):
    class Bad(Fake):
        def call(self, method, path, op, **kwargs):
            if op == "events_detail_get":
                raise RuntimeError("DEMO read interrupted")
            return super().call(method, path, op, **kwargs)
    with pytest.raises(RuntimeError):
        driver(store, Bad(events=[EVENT])).pull()
    reopened = Store(store.path)
    assert len(reopened.pending("log")) == 1
    fake = Fake(body(19, "UNDER_REVIEW"), events=[])
    assert driver(reopened, fake).pull() == 0
    assert reopened.get(DID).status == "UNDER_REVIEW" and reopened.has_event(EVENT["id"])
    assert reopened.pending() == []
    assert fake.calls[0][1] == "events_detail_get"


def test_event_log_whitelist_applied_before_evidence_line(tmp_path):
    # Bypass the credential-reading constructor; the real base logger writes this synthetic record only.
    instance = object.__new__(EventLogClient)
    instance.log_path = tmp_path / "synthetic.jsonl"
    event = copy.deepcopy(EVENT)
    event["summary"] = "DEMO-PRIVATE-TEXT"
    event["resource"]["description"] = "DEMO-PRIVATE-TEXT"
    instance._log({"op": "evlog_events_list", "method": "GET", "path": "/v1/notifications/webhooks-events", "status": 200,
                   "body": {"events": [event]}, "extra_headers": {"demo": "DEMO-PRIVATE-TEXT"}})
    line = instance.log_path.read_text(encoding="utf-8")
    assert "DEMO-PRIVATE-TEXT" not in line and "https:" not in line and "summary" not in line and "description" not in line
    parsed = json.loads(line)["body"]["events"][0]
    assert set(parsed) == {"id", "event_type", "create_time", "resource_type", "resource_version", "event_version", "link_rels", "resource"}
    assert parsed["resource"] == {"dispute_id": DID}
    assert parsed["link_rels"] == ["resend", "self"]


def test_plain_real_client_is_refused_before_logging(store):
    from copilot.core.disputes import SandboxClient
    plain = object.__new__(SandboxClient)
    with pytest.raises(ValueError):
        Puller(ReadClient(plain, Budget()), store, lambda: NOW)


def test_synthetic_postman_shape_and_measured_sr89_words(store):
    assert set(EVENT) == {"create_time", "event_type", "event_version", "id", "links", "resource", "resource_type", "resource_version", "summary"}
    assert EVENT["resource"]["status"] == "OPEN" and EVENT["resource"]["dispute_channel"] == "INTERNAL"
    import re
    assert "1.4 hours observed" in LOG_FACTS
    assert "were still listed two minutes after its deletion in the sandbox. Retention beyond 1.4 hours was not measured." in LOG_FACTS
    assert not re.search(r"\b(our|we|pending)\b", LOG_FACTS, re.I)
    assert "have not been observed" not in LOG_FACTS
    event = metadata(EVENT)
    store.commit_get(snapshot(body(), "log", NOW), event)
    with store.connection() as c:
        assert [r[1] for r in c.execute("PRAGMA table_info(event_log)")] == ["event_id", "event_type", "create_time", "dispute_id", "sha256"]


def test_readonly_page_state_never_creates_database_or_tables(tmp_path, monkeypatch):
    monkeypatch.delenv("COPILOT_FEATURE_EVENTS", raising=False)
    path = tmp_path / "missing.db"
    assert view(path) == {"rows": [], "counters": {}, "unknown_count": 0}
    with pytest.raises(RuntimeError):
        Store(path)
    assert not path.exists()


def test_dr1b_populated_events_state_formats_time_and_keeps_raw_record(web_mode, monkeypatch, store):
    from copilot.app import create_app
    from copilot import config
    from _hosted import authenticated_client
    from _dom import parse
    store.commit_get(snapshot(body(4, "UNDER_REVIEW"), "sweep", NOW))
    monkeypatch.setattr(config, "db_path", lambda: store.path)
    before = view(store.path)
    web = authenticated_client(create_app(), client=("127.0.0.1", 12345))
    response = web.get("/events/state")
    assert response.status_code == 200
    dom = parse(response.text)
    status = dom.find("td", data_label="Status read from PayPal")
    assert status is not None and "Under review by PayPal" in status.text()
    assert status.find("span", class_="tag--na") is not None          # The one status vocabulary (copilot.web.status)
    checked = dom.find("td", data_label="Last checked")
    assert "(Mexico City time)" in checked.text() and "2026-10-07T" not in checked.text()
    assert checked.find("time") is not None and checked.find("time").attrs["datetime"].endswith("-06:00")
    record = dom.find("details", data_state="event-record")
    assert record.find("summary").text() == "Technical details"
    for raw in ["UNDER_REVIEW", "sweep", before["rows"][0]["version"], before["rows"][0]["checked_at"]]:
        assert raw in record.text()
    assert view(store.path) == before
