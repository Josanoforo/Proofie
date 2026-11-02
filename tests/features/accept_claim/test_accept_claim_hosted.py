"""D-101 B3, hosted rule: on the hosted copy accepting a claim is allowed on the judge's OWN pool case and refused (403, the reason
shown, no PayPal call) on a record dispute, on another session's case, on a case outside the pool, and when judge mode is off.
After an accepted claim the pool list shows the case as used. Synthetic fakes only, no network."""
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.pool_scope        # the hosted bound is ON here (root conftest.py turns it off for other tests)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resolution"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "local"))
from _hosted import authenticated_client, configure_hosted, set_test_csrf  # noqa: E402
from resolution_fakes import DISPUTE_ID, FakePayPal, _links  # noqa: E402
from test_accept_claim import AcceptFake, CONSEQUENCE, PHRASE, TOKEN  # noqa: E402

from copilot import config  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.features.accept_claim import service as accept  # noqa: E402
from copilot.features.judge_pool import service as pool  # noqa: E402
from copilot.web.routes_disputes import get_client  # noqa: E402

MINE = "cookie-of-the-judge-who-owns-the-case"
OTHER = "cookie-of-another-visitor-session!"
RECORD = "PP-R-HJE-10194967"
OUTSIDE = "PP-R-OUT-0000009"
JOIN = "PP-R-TST-0000002"
JOIN2 = "PP-R-TST-0000003"
POOL3 = f"{DISPUTE_ID},{JOIN},{JOIN2}"          # the judge's case plus two unused ones: accepting hers leaves exactly the floor


class PoolFake(AcceptFake):
    """AcceptFake, and the other pool cases read as PayPal's open inquiries (escalate offered) unless listed in `spent`."""
    def __init__(self, *a, spent=(), **k):
        super().__init__(*a, **k)
        self.spent = set(spent)

    def call(self, method, path, op, **kw):
        did = path.rsplit("/", 1)[-1]
        if method == "GET" and did in (JOIN, JOIN2):
            self.calls.append((method, path, op))
            if did in self.spent:
                return {**self._raw("RESOLVED", ()), "dispute_id": did}
            return {**self._raw("WAITING_FOR_BUYER_RESPONSE", ("escalate",)), "dispute_id": did, "dispute_life_cycle_stage": "INQUIRY"}
        return super().call(method, path, op, **kw)


def _base(dispute):
    return f"/disputes/{dispute}/resolution/accept-claim"


def _hosted(monkeypatch, tmp_path, *, pool_ids=POOL3, spent=(), judge_flag="true", cookie=MINE, hold=True):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    for name in ("COPILOT_FEATURE_RESOLUTION", "COPILOT_FEATURE_RESOLUTION_WRITES", "COPILOT_FEATURE_ACCEPT_CLAIM",
                 "COPILOT_FEATURE_REARM_NOTE_IGNORED"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("COPILOT_FEATURE_JUDGE_POOL", judge_flag)
    monkeypatch.setenv("COPILOT_JUDGE_POOL", pool_ids)
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    configure_hosted(monkeypatch)
    fake = PoolFake(spent=spent)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    web.cookies.set("proofie_judge", cookie, domain="localhost.local", path="/")
    if hold and judge_flag == "true" and pool_ids:
        assert pool.take(DISPUTE_ID, pool.session_hash(MINE))        # the case is MINE's: she picked it in the pool
    set_test_csrf(web, TOKEN)
    return web, fake


def _check(web, dispute=DISPUTE_ID):
    r = web.post(_base(dispute) + "/preview", data={"csrf_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    page = web.get(r.headers["location"]).text
    set_test_csrf(web, TOKEN)
    return (re.search(r'name="token" value="([^"]*)"', page).group(1),
            re.search(r'data-state="request-sha256">([0-9a-f]{64})<', page).group(1), page)


def _refused_everywhere(web, fake, dispute):
    """All four routes answer 403 with a reason, none reaches PayPal (no GET, no POST), and the page shows no control."""
    for method, path, data in (("post", "/preview", {"csrf_token": TOKEN}),
                               ("post", "/register", {"csrf_token": TOKEN, "token": "x", "approval_sha256": "0" * 64, "phrase": PHRASE}),
                               ("get", "/check/x", None), ("get", "/sent/1", None)):
        set_test_csrf(web, TOKEN)
        r = getattr(web, method)(_base(dispute) + path, **({"data": data} if data else {}))
        assert r.status_code == 403, (path, r.status_code)
        assert "Nothing was sent to PayPal" in r.text
    assert fake.calls == [] and not fake.accept_posts()


# ---- the own case passes ---------------------------------------------------------------------------------------------------
def test_the_judges_own_pool_case_is_offered_checked_sent_once_and_read_back(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path)
    assert "accept-claim-option" in web.get(f"/disputes/{DISPUTE_ID}/resolution").text
    set_test_csrf(web, TOKEN)
    token, sha, page = _check(web)
    assert CONSEQUENCE in page and PHRASE in page and not fake.accept_posts()
    r = web.post(_base(DISPUTE_ID) + "/register", data={"csrf_token": TOKEN, "token": token, "approval_sha256": sha, "phrase": PHRASE},
                 follow_redirects=False)
    assert r.status_code == 303 and "/accept-claim/sent/" in r.headers["location"]
    assert len(fake.accept_posts()) == 1
    assert 'data-status="sent_confirmed"' in web.get(r.headers["location"]).text


# ---- the four refusals: 403, the reason, no PayPal call ----------------------------------------------------------------------
def test_a_record_dispute_is_refused_even_when_listed_and_held(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path, hold=False)
    monkeypatch.setenv("COPILOT_JUDGE_POOL", f"{POOL3},{RECORD}")
    _refused_everywhere(web, fake, RECORD)
    assert "accept-claim-option" not in web.get(f"/disputes/{RECORD}/resolution").text


def test_another_sessions_case_is_refused(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path, cookie=OTHER, hold=False)
    assert pool.take(DISPUTE_ID, pool.session_hash(MINE))            # held by a different visitor
    _refused_everywhere(web, fake, DISPUTE_ID)
    assert "accept-claim-option" not in web.get(f"/disputes/{DISPUTE_ID}/resolution").text
    assert not fake.accept_posts()


def test_a_visitor_without_the_judge_cookie_is_refused(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path)
    web.cookies.delete("proofie_judge", domain="localhost.local", path="/")
    _refused_everywhere(web, fake, DISPUTE_ID)


def test_a_case_outside_the_pool_is_refused(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path)
    _refused_everywhere(web, fake, OUTSIDE)
    assert "accept-claim-option" not in web.get(f"/disputes/{OUTSIDE}/resolution").text


@pytest.mark.parametrize("judge_flag,pool_ids", [("false", DISPUTE_ID), ("true", "")])
def test_judge_mode_off_refuses_everything(monkeypatch, tmp_path, judge_flag, pool_ids):
    web, fake = _hosted(monkeypatch, tmp_path, judge_flag=judge_flag, pool_ids=pool_ids)
    assert config.hosted_demo() and not pool.enabled()
    _refused_everywhere(web, fake, DISPUTE_ID)
    assert "accept-claim-option" not in web.get(f"/disputes/{DISPUTE_ID}/resolution").text


def test_the_service_refuses_without_a_session_and_makes_no_paypal_call(monkeypatch, tmp_path):
    _hosted(monkeypatch, tmp_path)
    fake = AcceptFake()
    assert accept.prepare(fake, DISPUTE_ID, None).ok is False and accept.prepare(fake, DISPUTE_ID, "someone-else").ok is False
    out = accept.register(fake, DISPUTE_ID, accept.approval_body(DISPUTE_ID, "40.00", "USD"), "0" * 64, PHRASE, None)
    assert out.status == "disabled" and fake.calls == []


# ---- the pool list ---------------------------------------------------------------------------------------------------------
def test_the_pool_list_shows_the_case_as_used_after_an_acceptance(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path)
    token, sha, _ = _check(web)
    web.post(_base(DISPUTE_ID) + "/register", data={"csrf_token": TOKEN, "token": token, "approval_sha256": sha, "phrase": PHRASE})
    assert fake.closed and len(fake.accept_posts()) == 1
    listing = web.get("/judge").text
    assert DISPUTE_ID in listing and ">Used<" not in listing and "Used" in listing
    assert f'href="/judge/cases/{DISPUTE_ID}"' not in listing          # used cases cannot be picked again


def test_local_installs_are_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COPILOT_FEATURE_ACCEPT_CLAIM", "true")
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    assert not config.hosted_demo() and accept.allowed("PP-R-ANY-0000001", None) and accept.enabled()


# ---- the hosted caps (D-112): floor 2, one per session, one per Mexico City day. 403, the reason, no accept-claim POST ------------
def _prior(session, dispute, days_ago=0):
    """An acceptance PayPal already confirmed on this install, `days_ago` days back."""
    row = accept.save_sent(dispute, "f" * 64, accept.STATUS_CONFIRMED, None, {}, None, session)
    conn = sqlite3.connect(str(config.db_path()))
    conn.execute("UPDATE accept_claim_sent SET created_at=? WHERE id=?", (time.time() - days_ago * 86400, row))
    conn.commit()
    conn.close()


def _register(web, token, sha):
    set_test_csrf(web, TOKEN)
    return web.post(_base(DISPUTE_ID) + "/register", data={"csrf_token": TOKEN, "token": token, "approval_sha256": sha, "phrase": PHRASE},
                    follow_redirects=False)


def test_the_pool_floor_refuses_an_acceptance_that_would_leave_fewer_than_two_unused_cases(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path, spent=(JOIN2,))        # one other case is still unused: accepting hers would leave 1
    assert accept.POOL_FLOOR == 2
    token, sha, _ = _check(web)
    r = _register(web, token, sha)
    assert r.status_code == 403 and "fewer than 2 unused cases" in r.text and "Nothing was sent to PayPal" in r.text
    assert not fake.accept_posts() and not fake.closed


def test_a_second_acceptance_from_the_same_session_is_refused(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path)
    token, sha, _ = _check(web)                                         # the check was made while the session was still free
    _prior(pool.session_hash(MINE), JOIN, days_ago=3)                   # an earlier day, so only the session cap can answer
    assert accept.ACCEPTS_PER_SESSION == 1
    r = _register(web, token, sha)
    assert r.status_code == 403 and "already accepted a claim in this session" in r.text and "Nothing was sent to PayPal" in r.text
    set_test_csrf(web, TOKEN)
    again = web.post(_base(DISPUTE_ID) + "/preview", data={"csrf_token": TOKEN}, follow_redirects=False)
    assert again.status_code == 403 and "in this session" in again.text
    assert not fake.accept_posts() and not fake.closed


def test_a_second_acceptance_on_the_same_mexico_city_day_is_refused_for_every_session(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path)
    token, sha, _ = _check(web)
    _prior(pool.session_hash(OTHER), JOIN)                              # another visitor accepted a claim today
    assert accept.ACCEPTS_PER_DAY == 1 and accept.DAY_ZONE == "America/Mexico_City"
    r = _register(web, token, sha)
    assert r.status_code == 403 and "already accepted on the hosted copy today" in r.text and "Nothing was sent to PayPal" in r.text
    set_test_csrf(web, TOKEN)
    again = web.post(_base(DISPUTE_ID) + "/preview", data={"csrf_token": TOKEN}, follow_redirects=False)
    assert again.status_code == 403 and "today" in again.text
    assert not fake.accept_posts() and not fake.closed


def test_the_day_is_the_mexico_city_calendar_day():
    noon_utc = 1_793_620_800            # 2026-11-02 12:00 UTC = 06:00 in Mexico City
    start = accept._day_start(noon_utc)
    assert noon_utc - start == 6 * 3600                                 # midnight there, six hours before
    assert accept._day_start(start - 1) == start - 24 * 3600            # one second earlier is the day before


def test_an_acceptance_still_goes_through_when_all_three_conditions_hold(monkeypatch, tmp_path):
    web, fake = _hosted(monkeypatch, tmp_path)                          # floor met (two unused others), no earlier acceptance today
    _prior(pool.session_hash(OTHER), "PP-R-OLD-0000001", days_ago=2)                 # an acceptance two days ago neither counts for the day nor for this session
    token, sha, _ = _check(web)
    r = _register(web, token, sha)
    assert r.status_code == 303 and "/accept-claim/sent/" in r.headers["location"]
    assert len(fake.accept_posts()) == 1 and fake.closed
    assert accept.cap_refusal(pool.session_hash(MINE)) is not None       # and the session and the day are now used


def test_local_installs_have_no_caps(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COPILOT_FEATURE_ACCEPT_CLAIM", "true")
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    _prior("a-session", "PP-R-ANY-0000001")
    fake = AcceptFake()
    body = accept.approval_body(DISPUTE_ID, "40.00", "USD")
    out = accept.register(fake, DISPUTE_ID, body, accept.approval_sha(body), PHRASE, None)
    assert out.status == "sent_confirmed" and len(fake.accept_posts()) == 1
