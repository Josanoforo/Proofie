"""RC4.3 W1-4b: in hosted mode the inbox marks each case 'available for a full walk', 'already answered by a visitor' or,
when PayPal is not asking for evidence, says so in the dispute page's own sentence.

Offline only. PayPal's list does not carry a case's evidences or action links (recorded sandbox lists have only the `self`
link), so the mark reads one detail per case through the request's client, with the same two functions the dispute page
uses (`next_step.seller_evidence_listed`, `send_service.provide_evidence_offered`). Written first; red for the right reason
(`build()` had no `detail_reader`, rows had no `walk`).
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _dom import parse
from _hosted import authenticated_client, configure_hosted
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.web import inbox_view, routes_disputes
from copilot.web.routes_disputes import get_client
from pilot.step3_dispute import normalize

REC = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
BASE = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
LISTED_TEXT = "PayPal already has evidence from you."
NOT_ASKING_TEXT = "PayPal is not asking for evidence on this dispute now."
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SCENARIOS = {"open": dict(provide=True, listed=False), "answered": dict(provide=True, listed=True),
             "answered-no-link": dict(provide=False, listed=True), "closed": dict(provide=False, listed=False)}
EXPECTED = {"open": "open", "answered": "answered", "answered-no-link": "closed", "closed": "closed"}   # the page decides "not asking" first


def detail(did, *, provide, listed, update_time=None):
    body = copy.deepcopy(BASE)
    body["dispute_id"] = did
    body["links"] = [l for l in body["links"] if provide or l["rel"] != "provide_evidence"]
    body["evidences"] = [e for e in body["evidences"] if e["source"] != "SUBMITTED_BY_SELLER"]
    if listed:
        body["evidences"].append({"evidence_type": "PROOF_OF_FULFILLMENT", "source": "SUBMITTED_BY_SELLER",
                                  "date": "2026-10-19T01:00:00.000Z"})
    if update_time:
        body["update_time"] = update_time
    return body


def list_item(did, update_time="2026-10-05T02:16:52.000Z"):
    """What PayPal's list gives: no evidences, only the self link."""
    body = detail(did, provide=False, listed=False)
    body.pop("evidences", None)
    body["links"] = [l for l in body["links"] if l["rel"] == "self"]
    body["update_time"] = update_time
    return body


class Reader:
    """A fake request client: show_dispute answers from `details`; an exception value is raised."""

    def __init__(self, details):
        self.details = details
        self.calls = []

    def call(self, method, path, op, **kw):
        assert method == "GET" and op == "show_dispute", (method, op)   # the mark only reads
        did = path.rsplit("/", 1)[1]
        self.calls.append(did)
        value = self.details[did]
        if isinstance(value, Exception):
            raise value
        return value


def rows_for(ids, **kw):
    out = []
    for did in ids:
        raw = list_item(did, **kw)
        out.append({"norm": normalize(raw, NOW), "raw": raw, "auto_prep": None})
    return out


@pytest.fixture(autouse=True)
def fresh_cache():
    inbox_view.clear_walk_cache()
    yield
    inbox_view.clear_walk_cache()


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "test-hosted-access-code")


def walks(view):
    return {r["norm"]["dispute_id"]: r["walk"] for r in view["rows"]}


def test_each_scenario_gets_its_mark(hosted, tmp_path):
    ids = {name: f"PP-R-WALK-{n:07d}" for n, name in enumerate(SCENARIOS, 1)}
    reader = Reader({ids[name]: detail(ids[name], **spec) for name, spec in SCENARIOS.items()})
    view = inbox_view.build(rows_for(ids.values()), tmp_path / "x.db", NOW, detail_reader=reader)
    marks = walks(view)
    for name in SCENARIOS:
        assert marks[ids[name]]["state"] == EXPECTED[name], (name, marks[ids[name]])
    assert marks[ids["open"]]["text"] == "Available for a full walk."
    assert marks[ids["answered"]]["text"] == "Already answered by a visitor."
    assert marks[ids["closed"]]["text"] == NOT_ASKING_TEXT
    assert sorted(reader.calls) == sorted(ids.values())             # one read per case


def test_no_reader_or_local_mode_marks_nothing_and_reads_nothing(monkeypatch, tmp_path):
    did = "PP-R-WALK-0000001"
    reader = Reader({did: detail(did, provide=True, listed=False)})
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    assert walks(inbox_view.build(rows_for([did]), tmp_path / "x.db", NOW))[did] is None          # hosted, no reader
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    assert walks(inbox_view.build(rows_for([did]), tmp_path / "x.db", NOW, detail_reader=reader))[did] is None   # local
    assert reader.calls == []


def test_a_failed_read_marks_that_case_nothing_and_the_others_still_get_theirs(hosted, tmp_path):
    good, bad = "PP-R-WALK-0000001", "PP-R-WALK-0000002"
    reader = Reader({good: detail(good, provide=True, listed=False), bad: RuntimeError("PayPal unreachable")})
    marks = walks(inbox_view.build(rows_for([good, bad]), tmp_path / "x.db", NOW, detail_reader=reader))
    assert marks[good]["state"] == "open" and marks[bad] is None


def test_the_read_is_kept_per_case_and_update_time_and_read_again_when_the_case_changes(hosted, tmp_path):
    did = "PP-R-WALK-0000001"
    reader = Reader({did: detail(did, provide=True, listed=False)})
    inbox_view.build(rows_for([did]), tmp_path / "x.db", NOW, detail_reader=reader)
    inbox_view.build(rows_for([did]), tmp_path / "x.db", NOW, detail_reader=reader)
    assert reader.calls == [did]                                     # second inbox load: no second read
    reader.details[did] = detail(did, provide=True, listed=True)
    marks = walks(inbox_view.build(rows_for([did], update_time="2026-10-19T01:05:00.000Z"), tmp_path / "x.db", NOW,
                                   detail_reader=reader))
    assert reader.calls == [did, did] and marks[did]["state"] == "answered"


def test_a_malformed_detail_marks_nothing(hosted, tmp_path):
    did = "PP-R-WALK-0000001"
    reader = Reader({did: ["not", "a", "dispute"]})
    assert walks(inbox_view.build(rows_for([did]), tmp_path / "x.db", NOW, detail_reader=reader))[did] is None


# --- through the page: the same rule as the dispute page --------------------------------------------------------------
class PageClient:
    def __init__(self, details):
        self.details = details

    def call(self, method, path, op, **kw):
        if op == "list_disputes":
            return {"items": [list_item(d) for d in self.details], "links": []}
        if op == "show_dispute":
            return self.details[path.rsplit("/", 1)[1]]
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _app(monkeypatch, client, inject):
    configure_hosted(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: client
    real = inbox_view.build
    if inject:      # what the route does once it passes the request's client (works whether or not it already does)
        monkeypatch.setattr(inbox_view, "build", lambda rows, db, now, **kw: real(rows, db, now, **{**kw, "detail_reader": client}))
    else:           # a route that passes no reader (whatever the route now does)
        monkeypatch.setattr(inbox_view, "build", lambda rows, db, now, **kw: real(
            rows, db, now, **{k: v for k, v in kw.items() if k != "detail_reader"}))
    return authenticated_client(app)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_inbox_mark_never_disagrees_with_the_dispute_page(monkeypatch, hosted, name):
    did = "PP-R-WALK-0000001"
    client = PageClient({did: detail(did, **SCENARIOS[name])})
    web = _app(monkeypatch, client, inject=True)
    inbox = web.get("/disputes")
    assert inbox.status_code == 200
    mark = parse(inbox.text).find("tr", data_dispute=did).find(data_state="walk-mark")
    assert mark is not None and mark.attrs["data-walk"] == EXPECTED[name], inbox.text[:200]
    page = web.get(f"/disputes/{did}").text
    if EXPECTED[name] == "answered":
        assert LISTED_TEXT in page and NOT_ASKING_TEXT not in page
    elif EXPECTED[name] == "closed":
        assert NOT_ASKING_TEXT in page and LISTED_TEXT not in page          # also when evidence is listed but no action is offered
        assert NOT_ASKING_TEXT in mark.text()                       # the mark uses the page's own sentence
    else:
        assert LISTED_TEXT not in page and NOT_ASKING_TEXT not in page


def test_local_mode_inbox_has_no_mark(monkeypatch):
    did = "PP-R-WALK-0000001"
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    client = PageClient({did: detail(did, provide=True, listed=False)})
    web = _app(monkeypatch, client, inject=True)
    html = web.get("/disputes").text
    assert did in html and 'data-state="walk-mark"' not in html


def test_without_the_route_passing_a_reader_the_hosted_inbox_shows_no_mark(monkeypatch, hosted):
    did = "PP-R-WALK-0000001"
    client = PageClient({did: detail(did, provide=True, listed=False)})
    web = _app(monkeypatch, client, inject=False)
    html = web.get("/disputes").text
    assert did in html and 'data-state="walk-mark"' not in html
