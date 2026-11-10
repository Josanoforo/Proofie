"""D-114 item 5: the hosted inbox shows the judge pool and the record case, nothing else. The sandbox account's other disputes are
the operator's own history and must never reach a judge: not in the list, not in a count, not by typing a dispute URL. The record
case (PP-R-HJE-10194967) is the cut's exhibit: judges see it, read-only. Offline: a fake PayPal whose list holds six disputes
while the pool names two."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "local"))
import test_routes_send as send_tests  # noqa: E402
from _hosted import authenticated_client, configure_hosted, set_test_csrf  # noqa: E402

from copilot import config
from copilot.core import disputes
from copilot.db import repo
from copilot.web.routes_disputes import get_client

POOL_A, POOL_B = "PP-R-POA-10000001", "PP-R-POB-10000002"
OTHER = ["PP-R-OPS-10000011", "PP-R-OPS-10000012", "PP-R-OPS-10000013"]
RECORD = "PP-R-HJE-10194967"
REPLAY = "PP-R-DEMO-0000001"
pytestmark = pytest.mark.pool_scope
NOT_PART = "not part of this demo"
READ_ONLY = "it is read-only"
TOKEN = "t" * 43


def raw_case(did):
    body = json.loads(json.dumps(send_tests.WAITING))
    body["dispute_id"] = did
    return body


class FakePayPal:
    """list returns every case; GET returns one. Every call is kept so a test can say what PayPal was asked."""
    def __init__(self, ids):
        self.cases = {did: raw_case(did) for did in ids}
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if path.split("?")[0] == "/v1/customer/disputes":
            return {"items": [json.loads(json.dumps(c)) for c in self.cases.values()], "links": []}
        did = path.split("/")[4]
        if method == "GET" and did in self.cases:
            return json.loads(json.dumps(self.cases[did]))
        return {}

    def read_ids(self):
        return {p.split("/")[4] for m, p, _op in self.calls if m == "GET" and p.count("/") >= 4}

    def posts(self):
        return [c for c in self.calls if c[0] != "GET"]


def build(monkeypatch, tmp_path, *, hosted="true", pool=f"{POOL_A},{POOL_B}", flag="true", replay="false", ids=None):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "evidence"))
    (tmp_path / "evidence").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", hosted)
    monkeypatch.setenv("COPILOT_FEATURE_JUDGE_POOL", flag)
    monkeypatch.setenv("COPILOT_FEATURE_REPLAY", replay)
    monkeypatch.setenv("COPILOT_JUDGE_POOL", pool)
    configure_hosted(monkeypatch)
    from copilot.app import create_app
    paypal = FakePayPal(ids or [POOL_A, POOL_B, RECORD, *OTHER])
    app = create_app()
    app.dependency_overrides[get_client] = lambda: paypal
    return authenticated_client(app), paypal


# ------------------------------------------------------------------------------------------------------ 1. the list
def test_list_is_pool_plus_the_record_case(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path)
    page = web.get("/disputes")
    assert page.status_code == 200
    for did in (POOL_A, POOL_B, RECORD):
        assert did in page.text
    for did in OTHER:
        assert did not in page.text
    assert not set(OTHER) & paypal.read_ids()               # a non-pool case is not even read for a walk mark


def test_counts_derived_from_the_inbox_match_the_pool_and_the_record_case(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path)
    assert "3 disputes need your response" in web.get("/disputes").text
    items, _pages = disputes.fetch_all_disputes(paypal)      # the one reader the other list consumers share
    assert sorted(i["dispute_id"] for i in items) == sorted([POOL_A, POOL_B, RECORD])


# ------------------------------------------------------------------------------------------- 2. the record case, read-only
def test_record_case_page_is_read_only(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path)
    page = web.get(f"/disputes/{RECORD}")
    assert page.status_code == 200
    assert 'data-state="record-case-read-only"' in page.text
    assert "<form" not in page.text.lower() and 'type="submit"' not in page.text.lower()
    assert f'href="/disputes/{RECORD}/' not in page.text                       # no link into its task pages
    assert "accept-claim-option" not in page.text and "/appeal" not in page.text and "/documents" not in page.text
    assert not paypal.posts()
    assert web.get(f"/disputes/{POOL_A}").text.lower().count("<form") > 0     # the pool case keeps its controls


@pytest.mark.parametrize("method,path", [
    ("get", "/disputes/{id}/resolution"), ("get", "/disputes/{id}/outcome"), ("get", "/disputes/{id}/workspace/send"),
    ("get", "/disputes/{id}/buyer-note"), ("get", "/disputes/{id}/accept-claim/check/x"), ("get", "/disputes/{id}/history"),
    ("post", "/disputes/{id}/documents"), ("post", "/disputes/{id}/packages"), ("post", "/disputes/{id}/accept-claim/preview"),
    ("post", "/disputes/{id}/accept-claim/register"), ("post", "/disputes/{id}/outcome/appeal/register"),
    ("post", "/disputes/{id}/local-preparation"), ("post", "/disputes/{id}/packages/1/send")])
def test_record_case_write_routes_refused(monkeypatch, tmp_path, method, path):
    web, paypal = build(monkeypatch, tmp_path)
    set_test_csrf(web, TOKEN)
    kw = {"data": {"csrf_token": TOKEN}} if method == "post" else {}
    response = getattr(web, method)(path.format(id=RECORD), **kw)
    assert response.status_code == 403
    assert READ_ONLY in response.text and "Nothing was sent to PayPal" in response.text
    assert paypal.calls == []                                                # refused before any PayPal read or write


def test_no_auto_prep_claim_for_the_record_case(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path)
    web.get("/disputes")
    db = config.db_path()
    assert repo.get_auto_prep(db, POOL_A) is not None                         # a pool case is claimed ...
    assert repo.get_auto_prep(db, RECORD) is None                             # ... the record case never
    for did in OTHER:
        assert repo.get_auto_prep(db, did) is None


# ----------------------------------------------------------------------------------------- 3. any other id: refused
@pytest.mark.parametrize("path", ["/disputes/{id}", "/disputes/{id}/documents", "/disputes/{id}/send", "/disputes/{id}/history",
                                  "/disputes/{id}/workspace/send", "/disputes/{id}/anything-else"])
def test_a_non_pool_dispute_url_is_refused_without_reading_paypal(monkeypatch, tmp_path, path):
    web, paypal = build(monkeypatch, tmp_path)
    response = web.get(path.format(id=OTHER[0]))
    assert response.status_code == 403 and NOT_PART in response.text
    assert OTHER[0] not in paypal.read_ids()
    assert web.get(f"/disputes/{POOL_A}").status_code == 200                  # a pool case still opens


@pytest.mark.parametrize("path", ["/disputes/{id}/documents", "/disputes/{id}/packages", "/disputes/{id}/accept-claim/register",
                                  "/disputes/{id}/outcome/appeal/register", "/disputes/{id}/local-preparation"])
def test_write_routes_of_a_non_pool_dispute_are_refused_before_any_handler(monkeypatch, tmp_path, path):
    web, paypal = build(monkeypatch, tmp_path)
    set_test_csrf(web, TOKEN)
    response = web.post(path.format(id=OTHER[0]), data={"csrf_token": TOKEN})
    assert response.status_code == 403 and NOT_PART in response.text
    assert paypal.calls == []


# ------------------------------------------------------------------------------------------------------ 5. pool unset
def test_pool_unset_lists_nothing_says_why_and_keeps_the_record_case_read_only_by_url(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path, pool="")
    page = web.get("/disputes")
    assert page.status_code == 200
    for did in (POOL_A, POOL_B, RECORD, *OTHER):
        assert did not in page.text
    assert "judge pool" in page.text and "no case is listed" in page.text.lower()
    assert not paypal.calls                                                   # nothing to show, so PayPal is not read
    assert web.get(f"/disputes/{OTHER[0]}").status_code == 403
    record = web.get(f"/disputes/{RECORD}")
    assert record.status_code == 200 and "<form" not in record.text.lower()


def test_judge_mode_off_in_hosted_behaves_like_an_unset_pool(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path, flag="false")
    page = web.get("/disputes")
    for did in (POOL_A, POOL_B, RECORD, *OTHER):
        assert did not in page.text
    assert web.get(f"/disputes/{POOL_A}").status_code == 403
    assert web.get(f"/disputes/{RECORD}").status_code == 200


def test_pool_set_but_none_of_its_cases_in_the_list_still_lists_the_record_case(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path, ids=[RECORD, *OTHER])
    page = web.get("/disputes")
    assert RECORD in page.text and not any(did in page.text for did in OTHER)
    web2, _ = build(monkeypatch, tmp_path / "b", ids=OTHER)
    page2 = web2.get("/disputes")
    assert "none of them is in the list" in page2.text and "Open the judge cases" in page2.text


# ------------------------------------------------------------------------------------------------------ 6. local mode
def test_local_mode_lists_everything_and_opens_everything(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path, hosted="false", flag="false", pool="")
    page = web.get("/disputes")
    for did in (POOL_A, POOL_B, RECORD, *OTHER):
        assert did in page.text
    assert web.get(f"/disputes/{OTHER[0]}").status_code == 200
    record = web.get(f"/disputes/{RECORD}")
    assert record.status_code == 200 and "record-case-read-only" not in record.text and "<form" in record.text.lower()


# ------------------------------------------------------------------------------------------------------ the rest
def test_the_replay_case_stays_when_the_replay_flag_is_on(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path, replay="true")
    assert web.get(f"/replay/{REPLAY}").status_code == 200
    assert NOT_PART not in web.get(f"/disputes/{REPLAY}").text                # the replay id passes the pool bound
    assert web.get(f"/disputes/{OTHER[0]}").status_code == 403
    web2, _ = build(monkeypatch, tmp_path / "b", replay="false")
    assert NOT_PART in web2.get(f"/disputes/{REPLAY}").text


def test_the_scoped_client_reads_the_pool_and_the_record_case_and_writes_only_the_pool(monkeypatch, tmp_path):
    web, paypal = build(monkeypatch, tmp_path)
    scoped = disputes.ScopedClient(paypal)
    for did in (POOL_A, RECORD):
        assert scoped.call("GET", f"/v1/customer/disputes/{did}", "x")["dispute_id"] == did
    scoped.call("POST", f"/v1/customer/disputes/{POOL_A}/send-message", "x")
    scoped.call("GET", "/v1/customer/disputes", "list")           # the list itself is cut by fetch_all_disputes, not here
    before = len(paypal.calls)
    for method, path in (("GET", f"/v1/customer/disputes/{OTHER[0]}"), ("POST", f"/v1/customer/disputes/{OTHER[0]}/send-message"),
                         ("POST", f"/v1/customer/disputes/{RECORD}/provide-evidence"),
                         ("POST", f"/v1/customer/disputes/{RECORD}/accept-claim")):
        with pytest.raises(disputes.PayPalError):
            scoped.call(method, path, "x")
    assert len(paypal.calls) == before


@pytest.mark.parametrize("raw", ["", f"{POOL_A}", f"{POOL_A}, junk ,{POOL_A},{RECORD},../x,{POOL_B}", RECORD, "PP-R-AB-1,PP-R-ABCD-100000"])
def test_the_core_copy_of_the_allowlist_reads_like_the_judge_pool_service(monkeypatch, raw):
    from copilot.core import pool_scope
    from copilot.features.judge_pool import service
    monkeypatch.setenv("COPILOT_JUDGE_POOL", raw)
    assert pool_scope.pool_ids() == service.pool_ids()
    assert pool_scope.RECORD_DISPUTES == service.RECORD_DISPUTES and pool_scope.POOL_ID.pattern == service.DISPUTE_ID.pattern
