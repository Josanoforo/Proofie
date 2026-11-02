"""Offline tests for step 5 (send + verify + errors). A stateful httpx.MockTransport plays PayPal; nothing leaves the PC.

The base dispute bodies are synthetic fixtures with the shape of real sandbox responses (fixtures/synthetic). The 'after sending' state is
derived by adding a SUBMITTED_BY_SELLER evidence entry in the shape documented in the OpenAPI spec; the real
shape is only confirmed by the live send.
"""
import copy
import json
import threading
import multiprocessing
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import step5_send_evidence as s5
from client import SANDBOX, SandboxClient


@pytest.fixture(autouse=True)
def isolated_attempts_dir(monkeypatch, tmp_path):
    """Every test gets its own send_attempts/ directory: tests reuse the same DID/package hash, and the
    real one lives under evidence/ in the repo (must never be touched by the offline suite)."""
    monkeypatch.setattr(s5, "ATTEMPTS_DIR", tmp_path / "send_attempts")

REC = Path(__file__).parent / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
# Sendable double only; keep step3's fixed-clock historical fixture unchanged.
WAITING["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
UNDER_REVIEW = json.loads((REC / "dispute_PP-R-DEMO-0000001_under_review.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
CAPTURE = "DEMOTXN0000000001"

MODEL_OUT = {
    "proposed_evidence": [{
        "evidence_type": "PROOF_OF_FULFILLMENT",
        "tracking_info": [{"carrier_name": "MEX_ESTAFETA", "tracking_number": "DEMO8800123456",
                           "source_ids": ["doc_shipping_tracking"]}],
        "notes": "The package was shipped with Estafeta and delivered on 2026-10-06.",
        "claims": [{"text": "x", "source_ids": ["doc_shipping_tracking"], "basis": "seller_statement"}]}],
    "missing_evidence": [], "conflicts": [], "recommended_next_step": "submit_evidence_for_human_approval",
    "reviewer_note_draft": "n"}


def make_pkg(out=MODEL_OUT):
    req = s5.build_request_input(out)
    return {"package_version": 1, "dispute_id": DID, "paypal_request_input": req,
            "paypal_request_sha256": s5.sha256_json(req)}


class FakePayPal:
    """Stateful stand-in: GET returns `state`; POST provide-evidence applies `on_post` (or fails)."""
    def __init__(self, state, post_status=200, post_error=None, record_evidence=True):
        self.state, self.post_status, self.post_error = copy.deepcopy(state), post_status, post_error
        self.record_evidence, self.requests = record_evidence, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method == "GET":
            return httpx.Response(200, json=self.state)
        if self.post_error:
            raise self.post_error
        if self.post_status >= 400:
            return httpx.Response(self.post_status, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg1",
                                                          "details": [{"issue": "SIMULATED"}]})
        if self.record_evidence:
            body = req.content.decode("utf-8", "replace")
            payload = json.loads(body[body.index("{"): body.rindex("}") + 1])
            ev = payload["evidences"][0]
            self.state["evidences"].append({"evidence_type": ev["evidence_type"], "source": "SUBMITTED_BY_SELLER",
                                            "evidence_info": ev["evidence_info"], "notes": ev["notes"],
                                            "date": "2026-10-05T03:00:00.000Z"})
        return httpx.Response(200, json={"links": [{"href": "x", "rel": "self", "method": "GET"}]})

    def posts(self):
        return [r for r in self.requests if r.method == "POST"]


@pytest.fixture
def mkclient(monkeypatch, tmp_path):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "x")
    monkeypatch.setattr(s5, "_expected_capture", lambda: CAPTURE)

    def make(fake):
        c = SandboxClient("unused_step5_probe")
        c._http = httpx.Client(base_url=SANDBOX, transport=httpx.MockTransport(fake))
        c._token, c._token_expiry = "x", time.time() + 3600
        c.log_path = tmp_path / "log.jsonl"
        return c
    return make


# ---- pure helpers ---------------------------------------------------------------------------------
def test_request_input_has_tracking_demo_prefix_and_no_other_fields():
    req = s5.build_request_input(MODEL_OUT)
    ev = req["evidences"][0]
    assert ev["evidence_type"] == "PROOF_OF_FULFILLMENT"
    assert ev["evidence_info"] == {"tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                      "tracking_number": "DEMO8800123456"}]}
    assert ev["notes"].startswith("DEMO DATA (sandbox test") and ev["notes"].endswith("delivered on 2026-10-06.")
    assert set(req) == {"evidences"} and set(ev) == {"evidence_type", "evidence_info", "notes"}


def test_request_input_needs_tracking_and_respects_notes_limit():
    no_trk = copy.deepcopy(MODEL_OUT)
    no_trk["proposed_evidence"][0]["tracking_info"] = []
    with pytest.raises(ValueError):
        s5.build_request_input(no_trk)
    long = copy.deepcopy(MODEL_OUT)
    long["proposed_evidence"][0]["notes"] = "x" * 2000
    with pytest.raises(ValueError):
        s5.build_request_input(long)


def test_request_id_is_deterministic_per_dispute_and_package():
    a = s5.request_id_for(DID, "abc")
    assert a == s5.request_id_for(DID, "abc")
    assert a != s5.request_id_for(DID, "abd") and a != s5.request_id_for("PP-R-OTHER", "abc")


def test_classify_retryable_vs_not():
    assert s5.classify(s5.PayPalError(503, "SERVICE_UNAVAILABLE", "d", {})) == "retryable"
    assert s5.classify(s5.PayPalError(429, "RATE_LIMIT_REACHED", "d", {})) == "retryable"
    assert s5.classify(s5.PayPalError(400, "VALIDATION_ERROR", "d", {})) == "not_retryable"


# ---- preconditions on real recorded states --------------------------------------------------------
def norm(raw):
    from datetime import datetime, timezone
    from step3_dispute import normalize
    return normalize(raw, datetime(2026, 10, 5, 2, 30, tzinfo=timezone.utc), CAPTURE)


def test_preconditions_pass_on_real_waiting_state():
    req = make_pkg()["paypal_request_input"]
    assert s5.check_preconditions(norm(WAITING), req, DID, CAPTURE) == []


def test_preconditions_fail_on_real_under_review_state():
    req = make_pkg()["paypal_request_input"]
    problems = " | ".join(s5.check_preconditions(norm(UNDER_REVIEW), req, DID, CAPTURE))
    assert "UNDER_REVIEW" in problems and "provide_evidence link" in problems


def test_preconditions_fail_if_already_submitted_or_wrong_dispute_or_capture():
    req = make_pkg()["paypal_request_input"]
    done = copy.deepcopy(WAITING)
    done["evidences"].append({"evidence_type": "PROOF_OF_FULFILLMENT", "source": "SUBMITTED_BY_SELLER"})
    assert any("already submitted" in p for p in s5.check_preconditions(norm(done), req, DID, CAPTURE))
    assert any("mismatch" in p for p in s5.check_preconditions(norm(WAITING), req, "PP-R-OTHER", CAPTURE))
    other = copy.deepcopy(WAITING)
    other["disputed_transactions"][0]["seller_transaction_id"] = "OTHERCAPTURE"
    assert any("expected capture" in p for p in s5.check_preconditions(norm(other), req, DID, CAPTURE))


def test_verify_after_requires_same_tracking_from_seller():
    req = make_pkg()["paypal_request_input"]
    ok, _ = s5.verify_after(WAITING, req)
    assert ok is False                                        # PayPal's own state before sending: not there
    sent = copy.deepcopy(WAITING)
    ev = req["evidences"][0]
    sent["evidences"].append({"evidence_type": "PROOF_OF_FULFILLMENT", "source": "SUBMITTED_BY_SELLER",
                              "evidence_info": ev["evidence_info"], "notes": ev["notes"]})
    assert s5.verify_after(sent, req)[0] is True
    sent["evidences"][-1]["evidence_info"] = {"tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                                 "tracking_number": "OTHER123456"}]}
    assert s5.verify_after(sent, req)[0] is False
    sent["evidences"][-1]["source"] = "SUBMITTED_BY_BUYER"
    assert s5.verify_after(sent, req)[0] is False


# ---- send flow ------------------------------------------------------------------------------------
def test_send_refuses_without_approval_and_makes_no_call(mkclient):
    fake = FakePayPal(WAITING)
    code, msg = s5.send(mkclient(fake), make_pkg(), "  ")
    assert code == 1 and "REFUSED" in msg and fake.requests == []


def test_send_refuses_modified_package(mkclient):
    pkg = make_pkg()
    pkg["paypal_request_input"]["evidences"][0]["notes"] += " tampered"
    fake = FakePayPal(WAITING)
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 1 and "hash mismatch" in msg and fake.requests == []


def test_send_refuses_on_under_review_and_never_posts(mkclient):
    fake = FakePayPal(UNDER_REVIEW)
    code, msg = s5.send(mkclient(fake), make_pkg(), "ok")
    assert code == 1 and "NOT SENT (preconditions)" in msg and fake.posts() == []


def test_send_happy_path_posts_multipart_to_paypals_own_link_and_verifies(mkclient):
    fake = FakePayPal(WAITING)
    pkg = make_pkg()
    code, msg = s5.send(mkclient(fake), pkg, "Jonas approved in chat: package X")
    assert code == 0 and msg.startswith("EVIDENCE SENT AND VERIFIED (http_ok=True")
    assert "content confirmed by GET" in msg and "status WAITING_FOR_SELLER_RESPONSE ->" in msg
    post = fake.posts()
    assert len(post) == 1
    r = post[0]
    assert str(r.url) == f"{SANDBOX}/v1/customer/disputes/{DID}/provide-evidence"
    assert r.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert r.headers["paypal-request-id"] == s5.request_id_for(DID, pkg["paypal_request_sha256"])
    body = r.content.decode("utf-8")
    assert 'name="input"' in body and "Content-Type: application/json" in body
    assert "DEMO8800123456" in body and "filename" not in body          # no file attached
    # order: GET (pre-check), POST, GET (verification)
    assert [x.method for x in fake.requests] == ["GET", "POST", "GET"]


def test_send_logs_approval_and_request_body(mkclient, tmp_path):
    fake = FakePayPal(WAITING)
    c = mkclient(fake)
    s5.send(c, make_pkg(), "approved: hash abc")
    recs = [json.loads(l) for l in c.log_path.read_text(encoding="utf-8").splitlines()]
    assert [r["op"] for r in recs] == ["pre_send_get", "approval_recorded", "provide_evidence", "post_send_get"]
    assert recs[1]["approval_text"] == "approved: hash abc"
    assert recs[2]["request_input"]["evidences"][0]["evidence_type"] == "PROOF_OF_FULFILLMENT"


def test_send_2xx_without_evidence_in_get_is_not_confirmed(mkclient):
    fake = FakePayPal(WAITING, record_evidence=False)
    code, msg = s5.send(mkclient(fake), make_pkg(), "ok")
    assert code == 2 and "NOT CONFIRMED" in msg and "SENT AND VERIFIED" not in msg


def test_send_paypal_error_is_not_sent(mkclient):
    fake = FakePayPal(WAITING, post_status=422)
    code, msg = s5.send(mkclient(fake), make_pkg(), "ok")
    assert code == 1 and msg.startswith("NOT SENT: PayPal 422") and "not_retryable" in msg
    assert [x.method for x in fake.requests] == ["GET", "POST"]            # no verification GET, nothing claimed


def test_send_timeout_is_unknown_never_sent(mkclient):
    fake = FakePayPal(WAITING, post_error=httpx.ReadTimeout("t"))
    pkg = make_pkg()
    c = mkclient(fake)
    code, msg = s5.send(c, pkg, "ok")
    assert code == 3 and "OUTCOME PENDING" in msg and s5.request_id_for(DID, pkg["paypal_request_sha256"]) in msg
    last = json.loads(c.log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["transport_error"] == "ReadTimeout" and last["outcome"] == "unknown"
    # the attempt record stays sent_unconfirmed on disk: genuinely unknown, no auto-retry happened
    attempt = s5._read_attempt(s5._attempt_path(DID, pkg["paypal_request_sha256"]))
    assert attempt["state"] == "sent_unconfirmed"


def test_retry_with_same_package_reuses_the_same_request_id(mkclient):
    pkg = make_pkg()
    ids = []
    for _ in range(2):
        fake = FakePayPal(WAITING, post_status=503)
        s5.send(mkclient(fake), pkg, "ok")
        ids.append(fake.posts()[0].headers["paypal-request-id"])
    assert ids[0] == ids[1]


# ---- negative paths (ERRDIS) ----------------------------------------------------------------------
@pytest.mark.parametrize("code,status,cls", [("ERRDIS047", 400, "not_retryable"), ("ERRDIS040", 503, "retryable")])
def test_negative_error_is_handled_and_nothing_is_marked_sent(mkclient, code, status, cls):
    fake = FakePayPal(WAITING, post_status=status)
    c = mkclient(fake)
    rc, msg = s5.negative(c, make_pkg(), code)
    assert rc == 0 and "NOT marked sent" in msg and cls in msg and "before=False after=False" in msg
    assert str(fake.posts()[0].url).endswith(f"/v1/customer/disputes/{code}/provide-evidence")


def test_negative_rejects_non_errdis_values_and_unexpected_success(mkclient):
    fake = FakePayPal(WAITING)
    assert s5.negative(mkclient(fake), make_pkg(), "../../x")[0] == 1 and fake.requests == []
    assert s5.negative(mkclient(FakePayPal(WAITING)), make_pkg(), "ERRDIS047")[0] == 1   # 2xx would be a bug


# ---- fresh-GET revalidation of reason/amount -------------------------------------------------
def test_send_refuses_if_reason_changed_since_build(mkclient):
    pkg = make_pkg()
    pkg["dispute_reason"] = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"  # WAITING fixture's real reason differs
    fake = FakePayPal(WAITING)
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 1 and "reason changed" in msg and fake.posts() == []


def test_send_refuses_if_amount_changed_since_build(mkclient):
    pkg = make_pkg()
    pkg["dispute_amount_cents"] = 999999
    fake = FakePayPal(WAITING)
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 1 and "amount changed" in msg and fake.posts() == []


# ---- review_flags gate and approval fingerprint ------------------------------------
def test_send_refuses_nonempty_review_flags_without_explicit_confirmation(mkclient):
    pkg = make_pkg()
    pkg["review_flags"] = ["1 conflict(s) reported"]
    fake = FakePayPal(WAITING)
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 1 and "review_flags is not empty" in msg and fake.requests == []


def test_send_proceeds_with_review_flags_when_explicitly_confirmed(mkclient):
    pkg = make_pkg()
    pkg["review_flags"] = ["1 conflict(s) reported"]
    fake = FakePayPal(WAITING)
    code, msg = s5.send(mkclient(fake), pkg, "ok", confirm_review_flags=True)
    assert code == 0 and len(fake.posts()) == 1


def test_send_logs_review_flags_confirmation(mkclient):
    pkg = make_pkg()
    pkg["review_flags"] = ["missing evidence listed"]
    fake = FakePayPal(WAITING)
    c = mkclient(fake)
    code, _ = s5.send(c, pkg, "ok", confirm_review_flags=True)
    assert code == 0
    approval_rec = next(json.loads(l) for l in c.log_path.read_text(encoding="utf-8").splitlines()
                        if json.loads(l)["op"] == "approval_recorded")
    assert approval_rec["review_flags"] == ["missing evidence listed"] and approval_rec["review_flags_confirmed"]


def test_send_refuses_if_approval_fingerprint_no_longer_matches(mkclient):
    pkg = make_pkg()
    pkg["approval_sha256"] = "0" * 64  # as if the package (or its target dispute) changed after approval
    fake = FakePayPal(WAITING)
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 1 and "approval fingerprint mismatch" in msg and fake.requests == []


def test_approval_fingerprint_changes_with_dispute_id_action_or_content():
    req = make_pkg()["paypal_request_input"]
    base = s5.approval_fingerprint(DID, "provide_evidence", req)
    assert base == s5.approval_fingerprint(DID, "provide_evidence", req)             # deterministic
    assert base != s5.approval_fingerprint("PP-R-OTHER", "provide_evidence", req)    # dispute target
    assert base != s5.approval_fingerprint(DID, "accept_claim", req)                 # action
    tampered = copy.deepcopy(req)
    tampered["evidences"][0]["notes"] += " tampered"
    assert base != s5.approval_fingerprint(DID, "provide_evidence", tampered)        # content


# ---- persistent attempt registry -------------------------------------------------------
def test_double_invocation_does_not_repost_second_time(mkclient):
    pkg = make_pkg()
    fake = FakePayPal(WAITING)
    c = mkclient(fake)
    code1, msg1 = s5.send(c, pkg, "ok")
    code2, msg2 = s5.send(c, pkg, "ok")
    assert code1 == 0 and code2 == 0
    assert len(fake.posts()) == 1                      # only the first call ever POSTed
    assert "ALREADY SENT AND VERIFIED" in msg2
    attempt = s5._read_attempt(s5._attempt_path(DID, pkg["paypal_request_sha256"]))
    assert attempt["state"] == "confirmed"


def test_concurrent_sends_only_post_once(mkclient):
    """Two 'processes' (threads sharing the same attempts dir and the same stateful fake) racing the exact
    same package: exactly one POST happens; the other reconciles or backs off, never double-sends."""
    pkg = make_pkg()

    entered = threading.Event()
    release = threading.Event()
    decision = threading.Event()

    class SlowFakePayPal(FakePayPal):
        def __call__(self, req):
            if req.method == "POST":
                if entered.is_set():
                    decision.set()
                entered.set()
                assert release.wait(10), "owner was not released"
            return super().__call__(req)

    fake = SlowFakePayPal(WAITING)
    clients = [mkclient(fake), mkclient(fake)]
    results = []

    def worker():
        results.append(s5.send(clients[0], pkg, "ok"))

    owner = threading.Thread(target=worker)
    owner.start()
    assert entered.wait(10)
    # Run the contender while the first POST is definitely still in flight.
    def compete():
        try:
            results.append(s5.send(clients[1], pkg, "ok"))
        finally:
            decision.set()
    contender = threading.Thread(target=compete)
    contender.start()
    # Baseline contender enters POST too; release both only after its lifecycle decision.
    assert decision.wait(10)
    release.set()
    owner.join(10)
    contender.join(10)
    assert not owner.is_alive() and not contender.is_alive()

    assert len(fake.posts()) == 1, "exactly one real POST must have happened"
    codes = sorted(r[0] for r in results)
    assert codes == [0, 0] or codes == [0, 3], results  # winner confirms; loser confirms-via-reconcile or backs off


def test_crash_between_post_and_get_is_reconciled_not_reposted(mkclient):
    """Simulates a process restart: a prior run's attempt record says sent_unconfirmed (it POSTed and died
    before the verification GET), and PayPal's real state (per the fresh GET) already has the evidence."""
    pkg = make_pkg()
    apath = s5._attempt_path(DID, pkg["paypal_request_sha256"])
    s5._write_attempt(apath, {"dispute_id": DID, "package_sha256": pkg["paypal_request_sha256"],
                              "state": "sent_unconfirmed", "ts": "2026-10-04T21:00:00-0600"})
    already_sent = copy.deepcopy(WAITING)
    req = pkg["paypal_request_input"]
    already_sent["evidences"].append({"evidence_type": "PROOF_OF_FULFILLMENT", "source": "SUBMITTED_BY_SELLER",
                                      "evidence_info": req["evidences"][0]["evidence_info"],
                                      "notes": req["evidences"][0]["notes"]})
    fake = FakePayPal(already_sent)
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 0 and "ALREADY SENT AND VERIFIED" in msg
    assert fake.posts() == [], "must reconcile from the fresh GET, never re-POST"
    assert s5._read_attempt(apath)["state"] == "confirmed"


def test_crash_before_post_landed_remains_unknown(mkclient):
    """An open link and absent evidence cannot prove an earlier POST never landed."""
    pkg = make_pkg()
    apath = s5._attempt_path(DID, pkg["paypal_request_sha256"])
    s5._write_attempt(apath, {"dispute_id": DID, "package_sha256": pkg["paypal_request_sha256"],
                              "state": "sent_unconfirmed", "ts": "2026-10-04T21:00:00-0600"})
    fake = FakePayPal(WAITING)  # real state: nothing received, provide_evidence still offered
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 3 and "OUTCOME PENDING" in msg and fake.posts() == []
    assert s5._read_attempt(apath)["state"] == "sent_unconfirmed"


@pytest.mark.parametrize("raw", ["", "{", "null", "{}", '{"state":"pending"}'])
def test_incomplete_attempt_never_allows_post(mkclient, raw):
    pkg = make_pkg()
    path = s5._attempt_path(DID, pkg["paypal_request_sha256"])
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    fake = FakePayPal(WAITING)
    code, _ = s5.send(mkclient(fake), pkg, "ok")
    assert code == 3 and fake.posts() == []
    assert path.read_text(encoding="utf-8") == raw


def test_failed_pending_publication_never_posts(mkclient, monkeypatch):
    fake = FakePayPal(WAITING)
    def fail(*args):
        raise OSError("synthetic writer failure")
    monkeypatch.setattr(s5, "_write_attempt", fail)
    with pytest.raises(OSError, match="synthetic writer"):
        s5.send(mkclient(fake), make_pkg(), "ok")
    assert fake.posts() == []


def _process_sender(directory, entered, release, results, owner):
    """Spawn-safe fake-only client; no HTTP client or credentials are needed."""
    s5.ATTEMPTS_DIR = Path(directory)
    s5._expected_capture = lambda: CAPTURE
    class Client:
        def _log(self, record):
            pass
        def call(self, method, path, op, **kwargs):
            if method == "POST":
                results.put("POST")
                entered.set()
                assert release.wait(10)
            return copy.deepcopy(WAITING)
    results.put((owner, s5.send(Client(), make_pkg(), "ok")[0]))


def test_two_processes_share_one_lifecycle(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    directory = str(tmp_path / "attempts")
    owner = ctx.Process(target=_process_sender, args=(directory, entered, release, results, "owner"))
    contender = ctx.Process(target=_process_sender, args=(directory, entered, release, results, "contender"))
    owner.start()
    try:
        assert entered.wait(10)
        assert results.get(timeout=10) == "POST"
        contender.start()
        assert results.get(timeout=10) == ("contender", 3)
    finally:
        release.set()
        owner.join(10)
        if contender.pid is not None:
            contender.join(10)
    assert owner.exitcode == 0 and contender.exitcode == 0
    assert results.get(timeout=10) == ("owner", 2)


def test_abandoned_owner_never_reclaimed(mkclient):
    pkg = make_pkg()
    path = s5._attempt_path(DID, pkg["paypal_request_sha256"]).with_suffix(".lock")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    fake = FakePayPal(WAITING)
    assert s5.send(mkclient(fake), pkg, "ok")[0] == 3
    assert fake.requests == [] and path.exists()



def test_ambiguous_restart_with_link_gone_is_pending_with_no_retry(mkclient):
    """Restart scenario where the link is no longer offered and the content is not visible either: the
    protocol forbids guessing, so this must come back pending with zero network writes attempted."""
    pkg = make_pkg()
    apath = s5._attempt_path(DID, pkg["paypal_request_sha256"])
    s5._write_attempt(apath, {"dispute_id": DID, "package_sha256": pkg["paypal_request_sha256"],
                              "state": "sent_unconfirmed", "ts": "2026-10-04T21:00:00-0600"})
    moved_on = copy.deepcopy(UNDER_REVIEW)  # no provide_evidence link, and no matching evidence either
    fake = FakePayPal(moved_on)
    code, msg = s5.send(mkclient(fake), pkg, "ok")
    assert code == 3 and "OUTCOME PENDING" in msg and fake.posts() == []
    assert s5._read_attempt(apath)["state"] == "sent_unconfirmed"  # untouched: no silent state change


# ---- _write_attempt must be safe under concurrent writers (Windows os.replace race)
def test_write_attempt_many_threads_same_path(tmp_path):
    path = tmp_path / "send_attempts" / "D__sha.json"
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def writer(n: int) -> None:
        try:
            barrier.wait()
            for i in range(50):
                s5._write_attempt(path, {"writer": n, "i": i, "state": "pending"})
        except BaseException as e:  # noqa: BLE001 - the test reports any failure
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["writer"] in range(8) and final["state"] == "pending"
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind


def test_write_attempt_temp_names_differ_between_threads(tmp_path, monkeypatch):
    path = tmp_path / "D__sha.json"
    srcs: list[str] = []
    real_replace = s5.os.replace

    def spy(src, dst):
        srcs.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(s5.os, "replace", spy)
    ts = [threading.Thread(target=s5._write_attempt, args=(path, {"n": n})) for n in range(2)]
    for t in ts:
        t.start()
        t.join()  # sequential on purpose: same pid; the unique temp name comes from uuid4, not from the thread id (CPython can reuse thread ids)
    assert len(srcs) == 2 and srcs[0] != srcs[1]
