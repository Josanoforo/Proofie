"""Judge mode on the hosted demo: pool rules, the one PayPal write (escalate, to PayPal's own link), the locks, the limits, the
evidence lines, and the refusal of every other write. Offline: a fake PayPal that keeps its cases, a temp database per test."""
import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "local"))
import test_routes_send as send_tests  # noqa: E402
from _hosted import authenticated_client, configure_hosted  # noqa: E402

from copilot import config
from copilot.core import disputes
from copilot.features.judge_pool import routes, service
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[3]
BUYER, SELLER, REAL = "PP-R-AAA-10000001", "PP-R-AAA-10000002", "PP-R-AAA-10000003"
POOL3 = f"{BUYER},{SELLER},{REAL}"
RECORD = "PP-R-HJE-10194967"
HOST = "https://api-m.sandbox.paypal.com"


def link(did, rel):
    return {"rel": rel, "href": f"{HOST}/v1/customer/disputes/{did}/{rel.replace('_', '-')}", "method": "POST"}


def case(did, status="WAITING_FOR_BUYER_RESPONSE", stage="INQUIRY", rels=("escalate", "accept_claim", "send_message"), evidences=()):
    body = json.loads(json.dumps(send_tests.WAITING))
    body.update(dispute_id=did, status=status, dispute_life_cycle_stage=stage, evidences=list(evidences),
                links=[{"rel": "self", "href": f"{HOST}/v1/customer/disputes/{did}", "method": "GET"}] + [link(did, r) for r in rels])
    return body


def poof():
    return {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT", "notes": "DEMO DATA shipped", "date": "2026-10-12T00:00:00Z"}


class FakePayPal:
    """GET returns the case; POST escalate moves it the way PayPal did on 2026-11-01 and leaves the helper's note."""
    def __init__(self, cases):
        self.cases = {c["dispute_id"]: c for c in cases}
        self.calls, self.logged = [], []

    def call(self, method, path, op, *, json_body=None, log_extra=None, **kw):
        self.calls.append((method, path, op, json_body))
        did = path.split("/")[4]
        if method == "GET":
            return json.loads(json.dumps(self.cases[did]))
        assert path.endswith("/escalate") and method == "POST", path
        row = {"op": op, "method": method, "path": path, "status": 200, **(log_extra or {})}
        folder = Path(__import__("os").environ["PROOFIE_EVIDENCE_DIR"])
        with (folder / f"{service.LOG_NAME}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        moved = case(did, "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK", ("provide_evidence", "accept_claim"),
                     evidences=[{"source": "SUBMITTED_BY_SELLER", "evidence_type": "OTHER", "notes": json_body["note"], "date": "x"}])
        self.cases[did] = moved
        return {}

    def _log(self, record):
        self.logged.append(record)
        folder = Path(__import__("os").environ["PROOFIE_EVIDENCE_DIR"])
        with (folder / f"{service.LOG_NAME}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "evidence"))
    (tmp_path / "evidence").mkdir()
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_FEATURE_JUDGE_POOL", "true")
    monkeypatch.setenv("COPILOT_FEATURE_REARM_NOTE_IGNORED", "true")
    monkeypatch.setenv("COPILOT_JUDGE_POOL", POOL3)
    configure_hosted(monkeypatch)
    return tmp_path


def fake(*extra):
    return FakePayPal([case(BUYER), case(SELLER, "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK", ("provide_evidence", "accept_claim")),
                       case(REAL, evidences=[poof()]), *extra])


def web(paypal, monkeypatch):
    from copilot.app import create_app
    app = create_app()
    app.dependency_overrides[get_client] = lambda: paypal
    monkeypatch.setattr(routes, "write_client", lambda: paypal)
    return authenticated_client(app)


# ----------------------------------------------------------------------------------------------------- pool and states
def test_the_allowlist_drops_bad_ids_duplicates_and_the_record_dispute(env, monkeypatch):
    monkeypatch.setenv("COPILOT_JUDGE_POOL", f"{BUYER}, junk ,{BUYER},{RECORD},../x,{SELLER}")
    assert service.pool_ids() == [BUYER, SELLER]


@pytest.mark.parametrize("hosted,flag,pool", [("false", "true", POOL3), ("true", "false", POOL3), ("true", "true", ""), ("true", "true", RECORD)])
def test_judge_mode_exists_only_hosted_with_the_flag_and_a_usable_allowlist(env, monkeypatch, hosted, flag, pool):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", hosted)
    monkeypatch.setenv("COPILOT_FEATURE_JUDGE_POOL", flag)
    monkeypatch.setenv("COPILOT_JUDGE_POOL", pool)
    assert service.enabled() is False


def norm_of(raw):
    from datetime import datetime, timezone
    return disputes.normalize(raw, datetime.now(timezone.utc))


def test_case_states_come_from_paypals_read(env):
    ok = service.case_state
    assert ok(norm_of(case(BUYER))) == service.START
    assert ok(norm_of(case(SELLER, "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK", ("provide_evidence",)))) == service.ANSWER
    assert ok(norm_of(case(REAL, evidences=[poof()]))) == service.USED
    assert ok(norm_of(case(BUYER, rels=("accept_claim",)))) == service.UNAVAILABLE                      # no escalate link
    assert ok(norm_of(case(BUYER, "UNDER_REVIEW", "CHARGEBACK", ("escalate",)))) == service.UNAVAILABLE   # wrong status
    assert ok(norm_of(case(BUYER, stage="CHARGEBACK"))) == service.UNAVAILABLE                          # wrong stage
    assert ok(norm_of(case("PP-R-ZZZ-10009999"))) == service.UNAVAILABLE                                # not in the pool
    assert ok(norm_of(case(BUYER)), holder="someone", session="me") == service.TAKEN
    assert ok(norm_of(case(BUYER)), holder="me", session="me") == service.START


def test_our_own_rearm_note_does_not_make_a_case_used_but_a_real_send_does(env):
    paypal = fake()
    service.start_case(paypal, paypal, BUYER, "me")
    assert service.case_state(norm_of(paypal.cases[BUYER]), holder="me", session="me") == service.ANSWER
    mixed = case(SELLER, "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK", ("provide_evidence",),
                 evidences=[paypal.cases[BUYER]["evidences"][0], poof()])
    assert service.case_state(norm_of(mixed)) == service.USED


# ----------------------------------------------------------------------------------------------------- the one write
def test_the_start_posts_escalate_to_the_link_paypal_gave_and_reads_back(env):
    paypal = fake()
    result = service.start_case(paypal, paypal, BUYER, "sess1")
    assert result["started"] is True and result["status"] == "WAITING_FOR_SELLER_RESPONSE"
    method, path, op, body = paypal.posts()[0]
    assert (method, path, op, body) == ("POST", f"/v1/customer/disputes/{BUYER}/escalate", disputes.REARM_OP, {"note": disputes.REARM_NOTE})
    assert [c[0] for c in paypal.calls] == ["GET", "POST", "GET"]
    assert paypal.logged[-1]["op"] == "judge_pool_readback" and paypal.logged[-1]["status"] == "WAITING_FOR_SELLER_RESPONSE"


@pytest.mark.parametrize("mutate", [
    lambda c: c["links"].remove(next(l for l in c["links"] if l["rel"] == "escalate")),
    lambda c: next(l for l in c["links"] if l["rel"] == "escalate").update(href="https://api-m.paypal.com/v1/customer/disputes/x/escalate"),
    lambda c: next(l for l in c["links"] if l["rel"] == "escalate").update(href=f"{HOST}/v1/customer/disputes/PP-R-ZZZ-10009999/escalate"),
    lambda c: next(l for l in c["links"] if l["rel"] == "escalate").update(href=f"{HOST}/v1/customer/disputes/{BUYER}/escalate?x=1"),
    lambda c: next(l for l in c["links"] if l["rel"] == "escalate").update(method="GET"),
])
def test_without_a_usable_escalate_link_nothing_is_sent(env, mutate):
    paypal = fake()
    mutate(paypal.cases[BUYER])
    with pytest.raises(service.Refused):
        service.start_case(paypal, paypal, BUYER, "sess1")
    assert paypal.posts() == []


@pytest.mark.parametrize("did,why", [(SELLER, "already waiting for the seller: nothing to start"), (REAL, "real seller evidence"),
                                     (RECORD, "the record dispute"), ("PP-R-ZZZ-10009999", "not in the pool")])
def test_each_rule_alone_refuses_the_start(env, monkeypatch, did, why):
    monkeypatch.setenv("COPILOT_JUDGE_POOL", f"{POOL3},{RECORD}")
    paypal = fake(case(RECORD))
    if did == SELLER:
        assert service.start_case(paypal, paypal, did, "s")["started"] is False
    else:
        with pytest.raises(service.Refused):
            service.start_case(paypal, paypal, did, "s")
    assert paypal.posts() == [], why


def test_the_record_dispute_is_refused_even_when_an_operator_lists_it(env, monkeypatch):
    monkeypatch.setenv("COPILOT_JUDGE_POOL", f"{RECORD},{BUYER}")
    paypal = fake(case(RECORD))
    assert RECORD not in service.pool_ids()
    with pytest.raises(service.Refused):
        service.start_case(paypal, paypal, RECORD, "s")
    client = web(paypal, monkeypatch)
    assert client.get(f"/judge/cases/{RECORD}").status_code == 404
    assert RECORD not in client.get("/judge").text
    assert paypal.posts() == []


def test_a_second_start_of_the_same_case_makes_no_call(env):
    paypal = fake()
    service.start_case(paypal, paypal, BUYER, "s1")
    paypal.cases[BUYER] = case(BUYER)                     # even if PayPal showed the link again, the write guard holds
    with pytest.raises(service.Refused):
        service.start_case(paypal, paypal, BUYER, "s2")
    assert len(paypal.posts()) == 1


def test_a_paypal_refusal_is_reported_and_frees_the_key_for_a_later_try(env):
    paypal = fake()
    real_call = paypal.call

    def refusing(method, path, op, **kw):
        if method == "POST":
            raise disputes.PayPalError(400, "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE", "dbg", {})
        return real_call(method, path, op, **kw)

    paypal.call = refusing
    with pytest.raises(service.Refused):
        service.start_case(paypal, paypal, BUYER, "s1")
    paypal.call = real_call
    assert service.start_case(paypal, paypal, BUYER, "s1")["started"] is True


# ----------------------------------------------------------------------------------------------------- locks and limits
def test_two_sessions_one_winner_and_an_expired_lock_is_taken_over(env):
    assert service.take(BUYER, "a", now=1000.0) is True
    assert service.take(BUYER, "b", now=1001.0) is False
    assert service.take(BUYER, "a", now=1002.0) is True              # the holder asking again keeps the case, from that moment
    assert service.take(BUYER, "b", now=1002.0 + service.LOCK_TTL_SECONDS - 1) is False
    assert service.take(BUYER, "b", now=1002.0 + service.LOCK_TTL_SECONDS + 1) is True
    assert service.holder_of(BUYER, now=1002.0 + service.LOCK_TTL_SECONDS + 2) == "b"


def test_a_session_holds_one_case_and_moves_the_lock_only_before_a_start(env):
    paypal = fake()
    assert service.take(BUYER, "a", now=1.0) and service.take(SELLER, "a", now=2.0)
    assert service.holder_of(BUYER, now=3.0) is None and service.holder_of(SELLER, now=3.0) == "a"
    service.start_case(paypal, paypal, BUYER, "a")
    with pytest.raises(service.Refused):
        service.take(SELLER, "a")


def test_start_gap_session_day_and_global_limits(env):
    now = 10_000_000.0
    service.record_start("a", BUYER, now - service.START_GAP_SECONDS + 60)
    with pytest.raises(service.Refused):
        service.check_limits("a", now)
    service.check_limits("b", now)
    for n in range(2):
        service.record_start("c", f"PP-R-AAA-1000010{n}", now - 3600 * (n + 1))
    service.check_limits("c", now)
    service.record_start("c", "PP-R-AAA-10000109", now - 20000)
    with pytest.raises(service.Refused):
        service.check_limits("c", now)
    for n in range(service.STARTS_PER_DAY_GLOBAL):
        service.record_start(f"x{n}", f"PP-R-AAA-1000020{n % 10}", now - 100 - n)
    with pytest.raises(service.Refused):
        service.check_limits("fresh", now)


def test_restart_empties_the_tables_and_the_list_is_rederived_from_paypal(env, monkeypatch):
    paypal = fake()
    service.start_case(paypal, paypal, BUYER, "a")
    config.db_path().unlink()                             # a host restart empties the app's own tables
    assert service.holders() == {}
    assert service.case_state(norm_of(paypal.cases[BUYER])) == service.ANSWER      # PayPal's read says what the case is


# ----------------------------------------------------------------------------------------------------- evidence lines
def test_every_hosted_write_line_carries_the_session_hash_and_never_the_cookie(env, monkeypatch):
    paypal = fake()
    client = web(paypal, monkeypatch)
    assert client.get("/judge").status_code == 200
    cookie = client.cookies.get(routes.COOKIE)
    assert cookie
    page = client.get(f"/judge/cases/{BUYER}")
    assert page.status_code == 200 and "escalate" in page.text and disputes.REARM_NOTE in page.text
    token = client.cookies.get("csrf_token")
    done = client.post(f"/judge/cases/{BUYER}/start", data={"csrf_token": token})
    assert done.status_code == 200 and "Check again" in done.text
    lines = [json.loads(l) for l in (env / "evidence" / f"{service.LOG_NAME}.jsonl").read_text(encoding="utf-8").splitlines()]
    sessions = {l["judge_session"] for l in lines if "judge_session" in l}
    assert sessions == {service.session_hash(cookie)} and len(next(iter(sessions))) == 12
    assert {l["op"] for l in lines} >= {disputes.REARM_OP, "judge_pool_readback"}
    assert cookie not in (env / "evidence" / f"{service.LOG_NAME}.jsonl").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------------------------- the pages
def test_the_list_pick_start_check_and_redirect_flow(env, monkeypatch):
    paypal = fake()
    client = web(paypal, monkeypatch)
    listing = client.get("/judge").text
    assert "Ready to start" in listing and "Ready to answer" in listing
    assert f"{REAL} - Used" in listing and f'href="/judge/cases/{REAL}"' not in listing       # a used case is named, not offered
    assert client.get(f"/judge/cases/{SELLER}", follow_redirects=False).headers["location"] == f"/disputes/{SELLER}"
    assert client.get(f"/judge/cases/{BUYER}").status_code == 200
    token = client.cookies.get("csrf_token")
    assert client.post(f"/judge/cases/{BUYER}/start", data={"csrf_token": "wrong"}).status_code == 403
    assert paypal.posts() == []
    assert client.post(f"/judge/cases/{BUYER}/start", data={"csrf_token": token}).status_code == 200
    check = client.get(f"/judge/cases/{BUYER}/check", follow_redirects=False)
    assert check.status_code == 303 and check.headers["location"] == f"/disputes/{BUYER}"


def test_the_judge_cookie_is_httponly_strict_and_secure(env, monkeypatch):
    client = web(fake(), monkeypatch)
    first = client.get("/judge")
    header = next(h for h in first.headers.get_list("set-cookie") if h.startswith(routes.COOKIE))
    assert "HttpOnly" in header and "SameSite=strict" in header and "Secure" in header


def test_two_visitors_never_share_a_case(env, monkeypatch):
    paypal = fake()
    judge_a, judge_b = web(paypal, monkeypatch), web(paypal, monkeypatch)
    assert judge_a.get(f"/judge/cases/{BUYER}").status_code == 200
    assert judge_b.get(f"/judge/cases/{BUYER}").status_code == 409
    assert "Taken by another visitor" in judge_b.get("/judge").text
    token = judge_b.cookies.get("csrf_token")
    assert judge_b.post(f"/judge/cases/{BUYER}/start", data={"csrf_token": token}).status_code == 409
    assert paypal.posts() == []


def test_an_empty_pool_says_so_and_names_the_local_path(env, monkeypatch):
    paypal = FakePayPal([case(BUYER, rels=("accept_claim",)), case(SELLER, "UNDER_REVIEW", "CHARGEBACK", ("accept_claim",)),
                         case(REAL, evidences=[poof()])])
    page = web(paypal, monkeypatch).get("/judge").text
    assert "No case is ready right now" in page and "local path" in page and "/judge/cases/" not in page


def test_outside_the_hosted_demo_every_judge_page_is_404(env, monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    from copilot.app import create_app
    from _hosted import authenticated_client as client_for
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake()
    local = client_for(app)
    assert all(local.get(path).status_code == 404 for path in ("/judge", f"/judge/cases/{BUYER}", f"/judge/cases/{BUYER}/check"))
    assert local.post(f"/judge/cases/{BUYER}/start", data={"csrf_token": "x"}).status_code in (403, 404, 405)


# ----------------------------------------------------------------------------------------------------- the refusal of the rest
def test_the_hosted_app_has_no_adjudicate_or_require_evidence_path(env, monkeypatch):
    from copilot.app import create_app
    app = create_app()
    names = " ".join(f"{getattr(r, 'path', '')} {getattr(r, 'name', '')} {getattr(getattr(r, 'endpoint', None), '__name__', '')}".lower()
                     for r in app.routes)
    assert "adjudicate" not in names and "require" not in names and "require-evidence" not in names
    banned = ("adjudicate", "require_evidence", "require-evidence", "buyer_favor", "seller_favor")
    for path in (ROOT / "copilot/features/judge_pool").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                text = node.value
            elif isinstance(node, ast.Name):
                text = node.id
            elif isinstance(node, ast.Attribute):
                text = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                text = node.name
            assert text is None or not any(word in text.lower() for word in banned), (path.name, text)
    for path in (ROOT / "copilot/web/templates/features/judge_pool").glob("*.html"):
        assert not any(word in path.read_text(encoding="utf-8").lower() for word in banned), path.name


def test_the_modules_only_paypal_write_is_the_escalate_call(env):
    tree = ast.parse((ROOT / "copilot/features/judge_pool/service.py").read_text(encoding="utf-8"))
    posts = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "call"
             and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value in ("POST", "PATCH", "PUT", "DELETE")]
    assert len(posts) == 1 and ast.unparse(posts[0].args[2]) == "OP" and ast.unparse(posts[0].args[1]) == "path"
    assert service.OP == "dispute_state_escalate_rearm" and service.escalate_path.__doc__.startswith("The path of the escalate link")
    assert "escalate" in " ".join(ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "escalate_path")


# the hosted bound is ON for this whole module (root conftest.py turns it off for other tests); at the end so no line above moves
pytestmark = pytest.mark.pool_scope
