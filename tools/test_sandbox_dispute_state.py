"""Offline tests for tools/sandbox_dispute_state.py: a fake client, no network."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_dispute_state as sds  # noqa: E402

DID = "PP-R-ABC-10199999"


class FakeHttp:
    def __init__(self, base_url=sds.SANDBOX):
        self.base_url = base_url


class FakeClient:
    """Serves queued GET bodies, records every call, optionally refuses the POST."""

    def __init__(self, gets, *, post_error=None, base_url=sds.SANDBOX):
        self.gets = list(gets)
        self.calls = []
        self.logged = []
        self.post_error = post_error
        self._http = FakeHttp(base_url)

    def call(self, method, path, op, *, json_body=None, **kw):
        self.calls.append((method, path, op, json_body))
        if method == "GET":
            return self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]
        if self.post_error:
            raise self.post_error
        return {}

    def _log(self, record):
        self.logged.append(record)


def dispute(status, links=("self",), outcome=None, stage="CHARGEBACK"):
    body = {"dispute_id": DID, "status": status, "dispute_life_cycle_stage": stage,
            "links": [{"rel": rel, "href": f"https://x/{rel}"} for rel in links]}
    if outcome:
        body["dispute_outcome"] = {"outcome_code": outcome}
    return body


def run(client, side, **kw):
    return sds.adjudicate(client, DID, side, sleep=lambda _s: None, **kw)


def posts(client):
    return [c for c in client.calls if c[0] == "POST"]


def test_lose_posts_buyer_favor_between_two_reads_and_records_the_transition():
    client = FakeClient([dispute("UNDER_REVIEW", ("self", "adjudicate")),
                         dispute("RESOLVED", ("self",), "RESOLVED_BUYER_FAVOR")])
    code, message = run(client, "lose")
    assert code == 0 and "RESOLVED_BUYER_FAVOR" in message
    assert [c[0] for c in client.calls] == ["GET", "POST", "GET"]
    method, path, op, body = posts(client)[0]
    assert path == f"/v1/customer/disputes/{DID}/adjudicate"
    assert body == {"adjudication_outcome": "BUYER_FAVOR"}
    transition = client.logged[-1]
    assert transition["op"] == "state_transition" and transition["reached"] is True
    assert transition["before"]["status"] == "UNDER_REVIEW" and transition["after"]["outcome"] == "RESOLVED_BUYER_FAVOR"


def test_win_posts_seller_favor():
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",)), dispute("RESOLVED", (), "RESOLVED_SELLER_FAVOR")])
    code, _ = run(client, "win")
    assert code == 0
    assert posts(client)[0][3] == {"adjudication_outcome": "SELLER_FAVOR"}


@pytest.mark.parametrize("status,links", [("WAITING_FOR_SELLER_RESPONSE", ("self", "provide_evidence")),
                                          ("UNDER_REVIEW", ("self",)),
                                          ("RESOLVED", ("self",))])
def test_refuses_without_under_review_and_the_adjudicate_link_and_sends_nothing(status, links):
    client = FakeClient([dispute(status, links)])
    code, message = run(client, "lose")
    assert code == 1 and message.startswith("NOT SENT")
    assert posts(client) == []


def test_dry_run_reads_but_never_posts():
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",))])
    code, message = run(client, "lose", dry_run=True)
    assert code == 0 and message.startswith("DRY")
    assert posts(client) == []


def test_paypal_refusal_is_reported_and_not_retried():
    err = sds.PayPalError(400, "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE", "dbg", {})
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",))], post_error=err)
    code, message = run(client, "lose")
    assert code == 2 and "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE" in message
    assert len(posts(client)) == 1


def test_accepted_post_without_the_expected_state_is_not_reported_as_done():
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",)), dispute("UNDER_REVIEW", ("self",))])
    code, message = run(client, "lose", wait_s=10, poll_s=5)
    assert code == 3 and "expected RESOLVED" in message
    assert client.logged[-1]["reached"] is False


def test_wrong_outcome_in_the_read_back_is_not_done():
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",)), dispute("RESOLVED", (), "RESOLVED_SELLER_FAVOR")])
    code, _ = run(client, "lose", wait_s=0)
    assert code == 3


@pytest.mark.parametrize("base", ["https://api-m.paypal.com", "https://api.paypal.com", "https://example.com",
                                  "https://api-m.sandbox.paypal.com.evil.test", ""])
def test_host_guard_refuses_anything_but_the_sandbox_host(base):
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",))], base_url=base)
    with pytest.raises(sds.Refused):
        run(client, "lose")
    assert client.calls == []


def test_host_guard_accepts_the_sandbox_host():
    sds.check_host("https://api-m.sandbox.paypal.com")


@pytest.mark.parametrize("bad", ["", "PP-R-HJE", "pp-r-abc-10199999", "../PP-R-ABC-10199999", "PP-R-ABC-10199999/x", "12345"])
def test_dispute_id_shape_is_checked_before_any_call(bad):
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",))])
    with pytest.raises(sds.Refused):
        sds.adjudicate(client, bad, "lose")
    assert client.calls == []


def test_the_cuts_record_dispute_is_refused_before_any_call():
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",))])
    with pytest.raises(sds.Refused):
        sds.adjudicate(client, "PP-R-HJE-10194967", "win")
    assert client.calls == []


def test_the_module_names_no_live_host():
    text = Path(sds.__file__).read_text(encoding="utf-8")
    assert "api-m.paypal.com" not in text.replace("api-m.sandbox.paypal.com", "")


def act(client, name, **kw):
    return sds.perform(client, DID, name, sleep=lambda _s: None, **kw)


@pytest.mark.parametrize("name,body,final", [
    ("reset-seller", {"action": "SELLER_EVIDENCE"}, "WAITING_FOR_SELLER_RESPONSE"),
    ("need-buyer", {"action": "BUYER_EVIDENCE"}, "WAITING_FOR_BUYER_RESPONSE"),
])
def test_require_evidence_actions_post_the_documented_body_and_read_back(name, body, final):
    client = FakeClient([dispute("UNDER_REVIEW", ("self", "require_evidence")), dispute(final, ("self",))])
    code, _ = act(client, name)
    assert code == 0
    assert [c[0] for c in client.calls] == ["GET", "POST", "GET"]
    method, path, op, sent = posts(client)[0]
    assert path == f"/v1/customer/disputes/{DID}/require-evidence" and sent == body
    assert client.logged[-1]["reached"] is True and client.logged[-1]["action"] == name


@pytest.mark.parametrize("name", ["reset-seller", "need-buyer"])
@pytest.mark.parametrize("status,links", [("UNDER_REVIEW", ("self", "accept_claim", "provide_supporting_info")),
                                          ("WAITING_FOR_BUYER_RESPONSE", ("self", "require_evidence"))])
def test_require_evidence_is_refused_here_without_the_link_or_the_state(name, status, links):
    client = FakeClient([dispute(status, links)])
    code, message = act(client, name)
    assert code == 1 and message.startswith("NOT SENT") and "require_evidence" in message
    assert posts(client) == []


def test_rearm_escalates_a_case_waiting_for_the_buyer_and_waits_for_the_seller_link():
    client = FakeClient([dispute("WAITING_FOR_BUYER_RESPONSE", ("self", "escalate"), stage="INQUIRY"),
                         dispute("UNDER_REVIEW", ("self",)),
                         dispute("WAITING_FOR_SELLER_RESPONSE", ("self", "provide_evidence"))])
    code, message = act(client, "rearm")
    assert code == 0 and "WAITING_FOR_SELLER_RESPONSE" in message
    method, path, op, sent = posts(client)[0]
    assert path == f"/v1/customer/disputes/{DID}/escalate" and set(sent) == {"note"} and "DEMO DATA" in sent["note"]
    assert [c[0] for c in client.calls] == ["GET", "POST", "GET", "GET"]


def test_rearm_is_refused_without_the_escalate_link_and_a_status_without_the_link_is_not_done():
    client = FakeClient([dispute("WAITING_FOR_BUYER_RESPONSE", ("self",), stage="INQUIRY")])
    code, message = act(client, "rearm")
    assert code == 1 and message.startswith("NOT SENT") and posts(client) == []
    client = FakeClient([dispute("WAITING_FOR_BUYER_RESPONSE", ("self", "escalate")),
                         dispute("WAITING_FOR_SELLER_RESPONSE", ("self",))])
    code, _ = act(client, "rearm", wait_s=0)
    assert code == 3


def test_every_action_has_its_own_default_wait_and_a_documented_path():
    assert {a["path"] for a in sds.ACTIONS.values()} == {"adjudicate", "require-evidence", "escalate"}
    assert sds.ACTIONS["rearm"]["wait_s"] > sds.ACTIONS["lose"]["wait_s"]


def test_main_picks_the_action_from_the_flag(monkeypatch, capsys):
    seen = {}

    def fake_perform(client, dispute_id, name, **kw):
        seen.update(id=dispute_id, name=name, dry=kw["dry_run"], wait=kw["wait_s"])
        return 0, "ok"

    monkeypatch.setattr(sds, "SandboxClient", lambda _name: object())
    monkeypatch.setattr(sds, "perform", fake_perform)
    assert sds.main(["--need-buyer", DID, "--dry-run", "--wait", "7"]) == 0
    assert seen == {"id": DID, "name": "need-buyer", "dry": True, "wait": 7.0}
    assert sds.main(["--rearm", DID]) == 0 and seen["name"] == "rearm" and seen["wait"] is None


@pytest.mark.parametrize("status,links,flag", [
    ("WAITING_FOR_BUYER_RESPONSE", ("self", "escalate"), "--rearm"),
    ("UNDER_REVIEW", ("self", "require_evidence"), "--reset-seller"),
    ("WAITING_FOR_SELLER_RESPONSE", ("self", "provide_evidence"), None),
    ("RESOLVED", ("self",), None),
    ("UNDER_REVIEW", ("self", "accept_claim", "provide_supporting_info"), None),
])
def test_plan_names_the_flag_that_fits_the_case_or_none(status, links, flag):
    got, sentence = sds.plan_for(dispute(status, links))
    assert got == flag and status in sentence


def test_plan_is_read_only_and_allowed_on_the_record_dispute_but_not_on_another_host():
    client = FakeClient([dispute("WAITING_FOR_BUYER_RESPONSE", ("self", "escalate"), stage="INQUIRY")])
    code, message = sds.plan(client, "PP-R-HJE-10194967")
    assert code == 0 and "[flag --rearm]" in message
    assert [c[0] for c in client.calls] == ["GET"]
    with pytest.raises(sds.Refused):
        sds.plan(FakeClient([dispute("UNDER_REVIEW")], base_url="https://example.com"), DID)
    with pytest.raises(sds.Refused):
        sds.plan(FakeClient([dispute("UNDER_REVIEW")]), "not-an-id")


# The live HZY run (lane P1, 2026-11-01): POST 200 at 16:57:44, the adjudicate link gone at 16:59:17, RESOLVED at 17:01:20 with
# the outcome spelled RESOLVED_BUYER_FAVOUR. Only the fields the tool reads are kept here; no PayPal body is copied.
HZY_AFTER = {"status": "RESOLVED", "dispute_life_cycle_stage": "CHARGEBACK",
             "dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"}, "links": [{"rel": "self"}]}


def test_the_spelling_paypal_reads_back_is_accepted_for_lose_and_win():
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",)), HZY_AFTER])
    code, message = run(client, "lose")
    assert code == 0 and "RESOLVED_BUYER_FAVOUR" in message
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",)), dict(HZY_AFTER, dispute_outcome={"outcome_code": "RESOLVED_SELLER_FAVOUR"})])
    assert run(client, "win")[0] == 0


def test_the_other_side_in_either_spelling_is_not_done():
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",)), HZY_AFTER])
    assert run(client, "win", wait_s=0)[0] == 3
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",)), dict(HZY_AFTER, dispute_outcome={"outcome_code": "RESOLVED_SELLER_FAVOR"})])
    assert run(client, "lose", wait_s=0)[0] == 3


def test_the_default_wait_covers_the_observed_lag_and_a_vanished_link_is_not_a_failure():
    assert sds.ADJUDICATE_WAIT_S >= 300 and sds.POLL_S <= 30
    transition = dispute("UNDER_REVIEW", ("self", "provide_supporting_info"))     # the adjudicate link has gone, status still UNDER_REVIEW
    slept = []
    client = FakeClient([dispute("UNDER_REVIEW", ("adjudicate",))] + [transition] * 14 + [HZY_AFTER])
    code, _ = sds.perform(client, DID, "lose", sleep=slept.append)
    assert code == 0 and sum(slept) >= 210 and len(posts(client)) == 1
