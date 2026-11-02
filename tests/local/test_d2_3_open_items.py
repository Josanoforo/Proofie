"""Open items of the full review (a missing document row, Check again, a late confirmation, a pending attempt and the first
run without credentials). Each test checks what the seller sees (DOM) and the
effect at the "PayPal" double (calls and POSTs), and each was run first WITHOUT its fix to show it fails.
"""
import pytest
from _hosted import authenticated_client, reset_csrf
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_integral_review as I
from _dom import actions_in, parse, state
from copilot import config
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import build_request_input, pick_tracking, sha256_json
from copilot.core.checklist_input import extracted_docs_for
from copilot.db import repo

DID = I.DID
_no_real_waiting = I._no_real_waiting          # same autouse fixture: the 5/15/30 s delays never wait


class ScriptedGets(I.PayPalDouble):
    """PayPalDouble whose recovery GETs answer from a script, one entry per call: "ok" or "fail" (PayPal unreachable
    for that one request). Anything past the script succeeds. Everything else behaves as PayPalDouble."""

    def __init__(self, script=(), **kw):
        super().__init__(**kw)
        self.script = list(script)

    def call(self, method, path, op, **kw):
        if op == "recovery_get" and self.script:
            if self.script.pop(0) == "fail":
                self.calls_log.append(op)
                raise httpx.ConnectError("PayPal unreachable for this one GET (double)")
        return super().call(method, path, op, **kw)


def _approved_manual_package(c, text="Shipped via FedEx on 2026-09-11."):
    I._upload(c, "01_coherent_delivery.pdf")
    pkg_id = I._manual_package(c)
    assert I._post(c, f"/disputes/{DID}/packages/{pkg_id}/claims", {"text": text}).status_code == 303
    assert I._approve(c, pkg_id).status_code == 303
    return pkg_id


def _check(c, pkg_id):
    return I._post(c, f"/disputes/{DID}/packages/{pkg_id}/check")


def _current_sha(pkg_id):
    claims = repo.list_claims(config.db_path(), pkg_id)
    tracking, carrier = pick_tracking(extracted_docs_for(config.db_path(), DID))
    return sha256_json(build_request_input(claims, tracking, carrier))


# ==== a missing document row never makes the page offer a send ==========================================
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_o2_a_missing_document_row_never_makes_the_page_offer_a_send_the_server_would_refuse():
    paypal = I.PayPalDouble()
    c = I._client(paypal)
    doc_id = I._upload(c, "01_coherent_delivery.pdf")
    pkg_id = I._manual_package(c)
    # the tracking number is ALSO typed by the seller, so deleting the document leaves the package sendable and the
    # only thing that changes is the attachment list (otherwise the missing tracking would mask the bug)
    assert I._post(c, f"/disputes/{DID}/manual-fields", {"package_id": str(pkg_id), "tracking_number": "DEMO-FDX-000001",
                                                         "carrier": "FedEx"}).status_code == 303
    assert I._post(c, f"/disputes/{DID}/packages/{pkg_id}/claims", {"text": "Shipped via FedEx on 2026-09-11."}).status_code == 303
    assert I._post(c, f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}").status_code == 303
    assert I._approve(c, pkg_id).status_code == 303
    page = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    assert any(f.attrs.get("action", "").endswith("/send") for f in page.find_all("form"))     # fine while it exists

    # the attached document's row disappears (the attachment row survives, as it would after an out-of-band edit)
    raw = sqlite3.connect(config.db_path())
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    raw.commit()
    raw.close()
    assert repo.list_attachments(config.db_path(), pkg_id) == []                              # the send path's view
    assert len(repo.list_attachment_sha256s(config.db_path(), pkg_id)) == 1                   # the other view (the bug)

    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    assert state(dom, "approval-cleared") is not None                                         # the page agrees with the server
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]
    refused = I._send(c, pkg_id)
    assert refused.status_code == 400 and "out of date" in refused.text.lower() and paypal.posts == 0
    # approving again must be coherent with the send path, so the loop ends (before: the new approval still
    # included the vanished attachment and every send stayed "out of date")
    assert I._approve(c, pkg_id).status_code == 303
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    assert any(f.attrs.get("action", "").endswith("/send") for f in dom.find_all("form"))
    sent = I._send(c, pkg_id)
    assert sent.status_code == 200 and paypal.posts == 1
    assert paypal.provide_evidence_calls[0]["log_extra"]["attachments"] == []


# ==== check again says could not check when THIS attempts get failed ========================================
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_o3a_check_again_says_could_not_check_when_THIS_attempts_get_failed_even_if_another_attempts_succeeded():
    paypal = ScriptedGets(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _approved_manual_package(c)
    assert I._send(c, pkg_id).status_code == 202                                              # this package's attempt (now)
    # ANOTHER package's attempt, older, with a stored request: recover_dispute() GETs it first
    other_req = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                "evidence_info": {"tracking_info": [{"tracking_number": "OTHER-1"}]}, "notes": "o"}]}
    # same local-offset format the registry itself stamps (list_by_dispute orders by that string, so an UTC-formatted
    # stamp could sort AFTER the real attempt and silently flip which GET comes first)
    old = (datetime.now().astimezone() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(config.db_path())
    store.claim(DID, "another-packages-sha")
    store.write(DID, "another-packages-sha", {"state": "sent_unconfirmed", "request_id": "req-other",
                                             "request_input": other_req, "ts": old})
    store.close()
    paypal.hide_stored = False                                                                # PayPal itself is fine...
    paypal.script = ["ok", "fail"]                                                            # ...but THIS attempt's GET fails
    posts_before, gets_before = paypal.posts, paypal.calls_log.count("recovery_get")
    r = _check(c, pkg_id)
    dom = parse(r.text)
    assert r.status_code == 502
    box = state(dom, "paypal-unreachable")
    assert box is not None and "Could not check" == dom.find("h1").text()
    assert state(dom, "result-uncertain") is None and "Still not confirmed" not in dom.text()
    assert any(f.attrs["action"].endswith("/check") for f in box.find_all("form"))            # Try again (GET only)
    assert paypal.calls_log.count("recovery_get") - gets_before == 2                          # both attempts were asked
    assert paypal.posts == posts_before == 1                                                  # and nothing was posted


def test_o3a_the_mapping_of_gets_to_attempts_follows_recover_disputes_one_get_per_qualifying_attempt():
    """Pins the contract the attempt-to-GET pairing relies on (copilot/core/send_recovery.py is not touched): one GET per attempt that
    is 'sent_unconfirmed' or 'abandoned' WITH a stored request, in list_by_dispute order. If that function ever
    changes this, this test fails before the page can mis-attribute a GET."""
    from copilot.core.send_recovery import recover_dispute
    paypal = I.PayPalDouble()
    req = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                          "evidence_info": {"tracking_info": [{"tracking_number": "T"}]}, "notes": "n"}]}
    store = SQLiteAttemptStore(config.db_path())
    for name, state_name, with_request in (("a", "sent_unconfirmed", True), ("b", "sent_unconfirmed", False),
                                           ("c", "abandoned", True), ("d", "failed", True), ("e", "confirmed", True)):
        store.claim(DID, name)
        body = {"state": state_name, "request_id": f"req-{name}"}
        if with_request:
            body["request_input"] = req
        store.write(DID, name, body)
    qualifying = [a["package_sha256"] for a in store.list_by_dispute(DID)
                  if a["state"] in ("sent_unconfirmed", "abandoned") and a.get("request_input") is not None]
    recover_dispute(paypal, store, DID, config.db_path())
    store.close()
    assert qualifying == ["a", "c"] and paypal.calls_log == ["recovery_get"] * len(qualifying)


# ==== an earlier version still waiting is said so never nothing pending =====================================
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_o3b_an_earlier_version_still_waiting_is_said_so_never_nothing_pending():
    paypal = I.PayPalDouble(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _approved_manual_package(c)
    assert I._send(c, pkg_id).status_code == 202                                              # version 1: unconfirmed
    first_sha = _current_sha(pkg_id)
    claim_id = repo.list_claims(config.db_path(), pkg_id)[0]["id"]
    assert I._post(c, f"/disputes/{DID}/packages/{pkg_id}/claims/{claim_id}",
                   {"text": "Shipped via FedEx on 2026-09-11, tracking DEMO-FDX-000001."}).status_code == 303
    assert _current_sha(pkg_id) != first_sha                                                  # a NEW version
    assert I._approve(c, pkg_id).status_code == 303
    gets_before, posts_before = paypal.calls_log.count("recovery_get"), paypal.posts
    r = _check(c, pkg_id)
    dom = parse(r.text)
    box = state(dom, "earlier-version-pending")
    assert box is not None and "banner-warn" in box.attrs["class"]
    assert dom.find("h1").text() == "An earlier version of this package is still waiting for PayPal"
    assert "Nothing pending" not in dom.text()
    assert any(f.attrs["action"].endswith("/check") for f in box.find_all("form"))            # it can be checked again
    assert paypal.calls_log.count("recovery_get") > gets_before and paypal.posts == posts_before == 1   # asked by GET only


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_o3b_another_packages_pending_attempt_is_still_neutral_for_this_package():
    paypal = I.PayPalDouble()
    c = I._client(paypal)
    pkg_id = _approved_manual_package(c)
    store = SQLiteAttemptStore(config.db_path())
    store.claim(DID, "another-packages-sha")
    store.write(DID, "another-packages-sha", {"state": "sent_unconfirmed", "request_id": "req-other",
                                             "request_input": {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                             "evidence_info": {"tracking_info": [{"tracking_number": "OTHER"}]}, "notes": "o"}]}})
    store.close()
    dom = parse(_check(c, pkg_id).text)
    assert state(dom, "nothing-pending") is not None and state(dom, "earlier-version-pending") is None


# ==== late confirmed is green because it is paypals own get confirming ==================================
def test_o4_late_confirmed_is_green_because_it_is_paypals_own_get_confirming_the_content():
    req = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                          "evidence_info": {"tracking_info": [{"tracking_number": "LATE"}]}, "notes": "late"}]}
    paypal = I.PayPalDouble()
    paypal.stored.append({"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                          "evidence_info": req["evidences"][0]["evidence_info"], "notes": "late", "documents": []})
    c = I._client(paypal)
    store = SQLiteAttemptStore(config.db_path())
    sha = sha256_json(req)
    store.claim(DID, sha)
    store.write(DID, sha, {"state": "abandoned", "request_id": "req-late", "request_input": req})
    store.close()
    box = state(parse(c.get(f"/disputes/{DID}").text), "late-confirmed")
    assert box is not None and "banner-ok" in box.attrs["class"] and "banner-error" not in box.attrs["class"]
    assert paypal.posts == 0                                                                  # confirmed by GET, never by a send


# ======================================================================= first run without credentials (clean install)
def test_a_fresh_clone_without_sandbox_credentials_gets_a_plain_setup_page_not_a_bare_500(monkeypatch):
    """Found by a clean install: with no .env the first page returned "Internal Server Error". SandboxClient is
    replaced by one that raises the same RuntimeError the real one raises when the credentials are missing (the
    real one would read this machine's .env, which a test must never do)."""
    from copilot.web import routes_disputes

    class NoCredentials:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET missing in .env")

    monkeypatch.setattr(routes_disputes, "SandboxClient", NoCredentials)
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)                  # The page is decided by the empty
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)              # environment, so the test sets it explicitly
    c = authenticated_client(I.create_app(), client=("127.0.0.1", 12345))          # NO dependency override: the real get_client()
    for path in ("/disputes", f"/disputes/{DID}"):
        r = c.get(path)
        dom = parse(r.text)
        box = state(dom, "hosted-no-credentials" if config.hosted_demo() else "credentials-missing")
        assert r.status_code == 503 and box is not None and "banner-error" in box.attrs["class"], path
        if config.hosted_demo():
            assert ".env" not in box.text() and "come back later" in box.text()
        else:
            assert ".env.example" in box.text()
        assert "Nothing was sent to PayPal" in box.text()
        assert "Traceback" not in r.text and "RuntimeError" not in r.text and "PAYPAL_CLIENT" not in r.text
        assert state(dom, "footer") is not None


def test_other_runtime_errors_in_get_client_are_not_disguised_as_missing_credentials(monkeypatch):
    from copilot.web import routes_disputes

    class Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("something unrelated broke")

    monkeypatch.setattr(routes_disputes, "SandboxClient", Broken)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "set-by-this-test")             # credentials ARE present: not a setup problem
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "set-by-this-test")
    c = authenticated_client(I.create_app(), client=("127.0.0.1", 12345), raise_server_exceptions=False)
    r = c.get("/disputes")
    assert r.status_code == 500 and state(parse(r.text), "credentials-missing") is None


# ==== a pending attempt shows the exact pending wording on the send page ================================
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c12_a_pending_attempt_shows_the_exact_pending_wording_on_the_send_page_and_the_unconfirmed_banner_on_detail():
    from copilot.web.routes_send import UNCERTAIN_HEADLINE
    paypal = ScriptedGets(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _approved_manual_package(c)
    sent = I._send(c, pkg_id)
    # The pending outcome's headline is now "Send attempted - not confirmed by PayPal"
    assert sent.status_code == 202 and parse(sent.text).find("h1").text() == UNCERTAIN_HEADLINE
    assert state(parse(sent.text), "result-uncertain") is not None
    store = SQLiteAttemptStore(config.db_path())
    assert [a["state"] for a in store.list_by_dispute(DID)][-1] in ("pending", "sent_unconfirmed")
    store.close()
    detail = c.get(f"/disputes/{DID}")
    # The pending send is the next-step panel (was the "including any pending send" bar label)
    assert detail.status_code == 200 and "PayPal has not confirmed it yet" in detail.text
    dom = parse(detail.text)
    assert any(state(dom, s) is not None for s in ("send-unconfirmed", "recheck-not-possible", "recheck-failed"))


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
