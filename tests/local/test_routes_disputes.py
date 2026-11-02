"""Offline tests for the dispute list/detail routes. The real SandboxClient dependency is
overridden with a fake that replays synthetic sandbox-shaped responses (pilot/fixtures/synthetic/) - no
network, never the real sandbox. tests/sandbox/ (marked, not run by default) covers the real thing.
"""
import pytest
from _hosted import authenticated_client, reset_csrf
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.app import create_app
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.disputes import PayPalError
from copilot.web.routes_disputes import get_client

REC = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE1 = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"


class FakeClient:
    """Duck-types SandboxClient.call() for routes_disputes.py's needs: GET list and GET detail, plus the
    context lookups the automatic background preparation makes (empty, not an error, same as the other
    fake clients in this test suite - these tests are about the list/detail routes, not the context lookup's own logic)."""
    def __init__(self, list_body=None, detail_body=None, detail_raises=None, recovery_get_body=None):
        self.list_body, self.detail_body, self.detail_raises = list_body, detail_body, detail_raises
        self.recovery_get_body = recovery_get_body
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if op == "list_disputes":
            return self.list_body
        if op == "show_dispute":
            if self.detail_raises:
                raise self.detail_raises
            return self.detail_body
        if op == "recovery_get":
            return self.recovery_get_body
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _app_with_fake(fake: FakeClient):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return app


def _client(app):
    return authenticated_client(app, client=("127.0.0.1", 12345))


def test_list_page_shows_plain_language_status_and_amount():
    # LIST_PAGE1's dispute is WAITING_FOR_SELLER_RESPONSE, which schedules the background
    # preparation (copilot/web/routes_disputes.py); conftest.py's autouse _isolated_copilot_data fixture
    # already redirects every test's DB to its own tmp_path, so no per-test override is needed here.
    fake = FakeClient(list_body=LIST_PAGE1)
    c = _client(_app_with_fake(fake))
    r = c.get("/disputes")
    assert r.status_code == 200
    assert "Waiting for your response" in r.text   # not the raw WAITING_FOR_SELLER_RESPONSE
    assert "WAITING_FOR_SELLER_RESPONSE" not in r.text
    assert "did not receive the item" in r.text     # not the raw MERCHANDISE_OR_SERVICE_NOT_RECEIVED
    assert "MERCHANDISE_OR_SERVICE_NOT_RECEIVED" not in r.text
    assert DID in r.text
    assert "25.00" in r.text


def test_list_page_shows_checked_at_timestamp_and_refresh_link():
    fake = FakeClient(list_body=LIST_PAGE1)
    c = _client(_app_with_fake(fake))
    r = c.get("/disputes")
    assert "PayPal was read at" in r.text
    assert 'href="/disputes"' in r.text   # the Refresh link re-GETs the same page


def test_list_page_with_zero_items_says_so_plainly():
    fake = FakeClient(list_body={"items": [], "links": []})
    c = _client(_app_with_fake(fake))
    r = c.get("/disputes")
    assert r.status_code == 200 and "No disputes need your attention right now" in r.text


def test_detail_page_shows_plain_status_and_a_real_deadline():
    fake = FakeClient(detail_body=WAITING)
    c = _client(_app_with_fake(fake))
    r = c.get(f"/disputes/{DID}")
    assert r.status_code == 200
    assert "Waiting for your response" in r.text
    assert "No verified deadline available" not in r.text   # WAITING fixture has a real due date
    assert "days left" in r.text or "due date passed" in r.text or "Due in" in r.text or "Due today" in r.text
    assert 'id="tasks"' in r.text and "Tasks in this case" in r.text
    assert f'href="/disputes/{DID}/workspace/prepare#package"' in r.text
    assert 'id="documents"' not in r.text          # the documents moved to the Prepare view
    assert 'id="documents"' in c.get(f"/disputes/{DID}/workspace/prepare").text


def test_detail_page_offers_generate_button_only_when_paypal_offers_provide_evidence():
    fake = FakeClient(detail_body=WAITING)   # WAITING fixture has the provide_evidence link
    c = _client(_app_with_fake(fake))
    r = c.get(f"/disputes/{DID}")
    assert "Generate evidence package" not in r.text and "Start manually instead" in r.text    # no document yet: upload first
    from copilot.db import repo as _repo
    _repo.insert_document(config.db_path(), DID, "a.pdf", "application/pdf", 10, "sha-a", "Carrier: FedEx", True)
    assert "Generate evidence package" in c.get(f"/disputes/{DID}").text


def test_detail_page_never_leaks_the_raw_status_constant_into_the_main_text():
    fake = FakeClient(detail_body=WAITING)
    c = _client(_app_with_fake(fake))
    r = c.get(f"/disputes/{DID}")
    # the raw constant may appear inside the <details>/<pre> debug block, but not in the visible status line
    main_text_before_details = r.text.split("<details>")[0]
    assert "WAITING_FOR_SELLER_RESPONSE" not in main_text_before_details
    assert "MERCHANDISE_OR_SERVICE_NOT_RECEIVED" not in main_text_before_details


def test_unknown_dispute_id_shows_a_plain_404_not_a_crash():
    fake = FakeClient(detail_raises=PayPalError(404, "RESOURCE_NOT_FOUND", "dbg-1", {}))
    c = _client(_app_with_fake(fake))
    r = c.get("/disputes/PP-R-DOES-NOT-EXIST")
    assert r.status_code == 404
    assert "That dispute was not found" in r.text
    assert "RESOURCE_NOT_FOUND" not in r.text and "dbg-1" not in r.text   # no raw PayPal jargon/debug id


def test_a_malformed_dispute_id_is_rejected_before_any_network_call():
    """dispute_id validated for every web route with that path segment, not only send/check."""
    fake = FakeClient()
    c = _client(_app_with_fake(fake))
    r = c.get("/disputes/not-a-real-paypal-id")
    assert r.status_code == 400
    assert "That dispute reference is not valid." in r.text
    assert fake.calls == []


def test_paypal_outage_shows_a_plain_try_again_message_not_a_crash():
    fake = FakeClient(detail_raises=PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-2", {}))
    c = _client(_app_with_fake(fake))
    r = c.get(f"/disputes/{DID}")
    assert r.status_code == 502
    assert "try again" in r.text.lower()
    assert "SERVICE_UNAVAILABLE" not in r.text and "dbg-2" not in r.text


# ---- startup/crash recovery reconciles before the vendor acts on this dispute ----------------
REQ_INPUT = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "x"}]}
CONFIRMED_EVIDENCE = {"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                                    "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]},
                                    "notes": "x", "date": "2026-10-05"}]}


def test_visiting_a_dispute_recovers_a_stale_attempt_that_actually_went_through():
    db_path = config.db_path()
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "sha-1")
    # A genuine prior attempt by this app (request_id set) - recover_dispute() only ever confirms
    # content it knows THIS app actually POSTed.
    store.write(DID, "sha-1", {"state": "sent_unconfirmed", "request_id": "req-1", "request_input": REQ_INPUT})
    store.close()

    fake = FakeClient(detail_body=WAITING, recovery_get_body=CONFIRMED_EVIDENCE)
    r = _client(_app_with_fake(fake)).get(f"/disputes/{DID}")
    assert r.status_code == 200
    assert "has not been confirmed yet" not in r.text   # it WAS confirmed, just now, on entry

    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, "sha-1")["state"] == "confirmed"
    store.close()


def test_visiting_a_dispute_shows_a_late_confirmation_of_an_abandoned_attempt():
    """A row the vendor had marked abandoned, later shown by PayPal's GET on an
    ordinary visit, must say so plainly on the dispute page."""
    db_path = config.db_path()
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "sha-1")
    store.write(DID, "sha-1", {"state": "abandoned", "request_id": "req-1", "request_input": REQ_INPUT})
    store.close()

    fake = FakeClient(detail_body=WAITING, recovery_get_body=CONFIRMED_EVIDENCE)
    r = _client(_app_with_fake(fake)).get(f"/disputes/{DID}")
    assert r.status_code == 200
    assert "later confirmed by PayPal" in r.text

    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, "sha-1")["state"] == "confirmed"
    store.close()


def test_visiting_a_dispute_shows_a_banner_when_an_attempt_is_still_genuinely_unconfirmed():
    db_path = config.db_path()
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, "sha-1")
    store.write(DID, "sha-1", {"state": "sent_unconfirmed", "request_input": REQ_INPUT})
    store.close()

    fake = FakeClient(detail_body=WAITING, recovery_get_body={"evidences": []})   # still not there
    r = _client(_app_with_fake(fake)).get(f"/disputes/{DID}")
    assert r.status_code == 200
    assert "an earlier send is not confirmed yet" in r.text          # the paused step, also when no package exists yet

    store = SQLiteAttemptStore(db_path)
    assert store.read(DID, "sha-1")["state"] == "sent_unconfirmed"   # left honestly ambiguous
    store.close()


# ---- automatic background preparation, scheduled from the inbox -------------------------------
import copy

from copilot.core import auto_prep
from copilot.db import repo

EXCLUDED_ID = "PP-R-DEMO-EXCLUDED"
EXCLUDED_LIST_PAGE = copy.deepcopy(LIST_PAGE1)
EXCLUDED_LIST_PAGE["items"][0]["dispute_id"] = EXCLUDED_ID


def _iso_client_and_db(fake):
    return _client(_app_with_fake(fake)), config.db_path()


def test_visiting_the_inbox_schedules_and_completes_a_background_prep():
    c, db_path = _iso_client_and_db(FakeClient(list_body=LIST_PAGE1))
    r = c.get("/disputes")
    assert r.status_code == 200
    # TestClient runs BackgroundTasks synchronously before returning, so this is already settled.
    rec = repo.get_auto_prep(db_path, DID)
    assert rec is not None and rec["status"] in ("ready", "needs_documents", "failed")
    assert "Preparing" not in r.text or rec["status"] != "preparing"


def test_claiming_auto_prep_is_itself_audited(monkeypatch):
    """The claim is audited on its own, separate from the eventual outcome - a crashed background
    task (even auto_prep_crashed is best-effort) must never leave zero trace that a prep was attempted."""
    monkeypatch.setattr(auto_prep, "prepare_dispute",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    c, db_path = _iso_client_and_db(FakeClient(list_body=LIST_PAGE1))
    c.get("/disputes")
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_claimed" and e["detail"]["version_hash"] for e in entries)


def test_a_second_visit_with_nothing_changed_does_not_reschedule():
    """Checked by package count and audit entry count, not updated_at - a timestamp is only
    second-precision (two actual preparations in the same wall-clock second would share one anyway) and
    proves nothing about whether a SECOND background run actually happened."""
    fake = FakeClient(list_body=LIST_PAGE1)
    c, db_path = _iso_client_and_db(fake)
    c.get("/disputes")
    first = repo.get_auto_prep(db_path, DID)
    packages_after_first = len(repo.list_packages(db_path, DID))
    audit_entries_after_first = len(repo.list_audit(db_path, DID))

    c.get("/disputes")
    second = repo.get_auto_prep(db_path, DID)
    assert second["version_hash"] == first["version_hash"] and second["package_id"] == first["package_id"]
    assert len(repo.list_packages(db_path, DID)) == packages_after_first   # no new package was built
    assert len(repo.list_audit(db_path, DID)) == audit_entries_after_first   # no new prep steps logged
    # The second visit still does its own list/context GETs for rendering, but never schedules/claims
    # another background prep for the unchanged dispute.


def test_an_uploaded_document_triggers_a_fresh_preparation(monkeypatch):
    """propose_package is monkeypatched to fail fast (AI 'unavailable') - this test is about re-scheduling
    on a changed version, not about the AI path, and must never touch the real model/GPU preflight."""
    monkeypatch.setattr(auto_prep, "propose_package",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no GPU in tests")))
    fake = FakeClient(list_body=LIST_PAGE1)
    c, db_path = _iso_client_and_db(fake)
    c.get("/disputes")
    first = repo.get_auto_prep(db_path, DID)

    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    c.get("/disputes")
    second = repo.get_auto_prep(db_path, DID)
    assert second["version_hash"] != first["version_hash"]
    # A genuinely new prep ran (not just a timestamp refresh - _now() is only second-precision, so two
    # preparations in the same wall-clock second can share updated_at): a fresh package was built.
    assert second["package_id"] != first["package_id"]


def test_a_genuinely_unexpected_crash_is_recorded_and_the_inbox_says_so(monkeypatch):
    """_run_auto_prep's own last-resort handler (routes_disputes.py) - a real programming error
    escaping prepare_dispute's own try/excepts (not one of the already-caught, expected failure modes)
    must be recorded as 'failed'/audited as auto_prep_crashed, never silently lost or left 'preparing'
    forever, and the inbox must say "Could not prepare automatically" on the next visit."""
    repo.set_manual_fields(config.db_path(), DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    monkeypatch.setattr(auto_prep, "claims_for_dispute_images",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("genuinely unexpected bug")))
    c, db_path = _iso_client_and_db(FakeClient(list_body=LIST_PAGE1))
    c.get("/disputes")   # schedules and runs the background prep, which crashes

    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "failed"
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "auto_prep_crashed" and "genuinely unexpected bug" in e["detail"]["error"]
              for e in entries)

    r2 = c.get("/disputes")
    assert "Could not prepare automatically" in r2.text


def test_the_inbox_shows_preparing_then_needs_documents_on_the_next_visit():
    """Background tasks run AFTER the response for THIS request is already rendered (Starlette runs them
    once the response is on its way out, not before the template renders) - so the FIRST visit's HTML
    still says "Preparing…"; the real outcome only shows up on the next visit/refresh, matching the
    requested "al abrir o refrescar" wording."""
    c, db_path = _iso_client_and_db(FakeClient(list_body=LIST_PAGE1))
    r1 = c.get("/disputes")
    assert "Preparing" in r1.text
    r2 = c.get("/disputes")
    assert "Draft: needs documents" in r2.text          # The progress line under the "Upload documents" button


def test_the_inbox_shows_draft_started_for_a_claim_less_auto_manual_shell():
    """A manual tracking field alone creates an auto_manual shell with no claims yet - the inbox
    must say "Draft started — add details", not "Draft ready — review" (that needs real content)."""
    fake = FakeClient(list_body=LIST_PAGE1)
    c, db_path = _iso_client_and_db(fake)
    repo.set_manual_fields(db_path, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    c.get("/disputes")
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "ready"
    assert repo.list_claims(db_path, rec["package_id"]) == []

    r2 = c.get("/disputes")
    assert "Draft started. Details are missing." in r2.text and "Add the details" in r2.text
    assert "Draft ready" not in r2.text and "Review the draft" not in r2.text
    assert f"/disputes/{DID}/packages/{rec['package_id']}" in r2.text


@pytest.mark.local_spec
def test_the_inbox_links_to_the_draft_once_ready_with_real_content(monkeypatch):
    """Once the auto-prepared draft actually has claims, the inbox says "Draft ready — review"."""
    from copilot.core import auto_prep as auto_prep_module
    fake = FakeClient(list_body=LIST_PAGE1)
    c, db_path = _iso_client_and_db(fake)
    repo.insert_document(db_path, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(auto_prep_module, "propose_package", lambda *a, **kw: {
        "validation_ok": True, "review_flags": [], "model": "qwen3.5:9b", "prompt_sha256": "abc",
        "raw_output": "{}", "effective_next_step": "submit_evidence_for_human_approval",
        "_output": {"proposed_evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "n",
                                           "tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                              "tracking_number": "DEMO1"}],
                                           "claims": [{"text": "Shipped via Estafeta.",
                                                      "source_ids": ["doc_1"], "basis": "verified"}]}]}})
    c.get("/disputes")
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "ready"
    assert len(repo.list_claims(db_path, rec["package_id"])) == 1

    r2 = c.get("/disputes")
    assert "Review the draft" in r2.text and "Draft prepared. Check each statement." in r2.text
    assert "Draft ready" not in r2.text
    assert f"/disputes/{DID}/packages/{rec['package_id']}" in r2.text


def test_excluded_disputes_are_never_auto_prepared(monkeypatch):
    monkeypatch.setattr(config, "AUTOPREP_EXCLUDE_IDS", frozenset({EXCLUDED_ID}))
    c, db_path = _iso_client_and_db(FakeClient(list_body=EXCLUDED_LIST_PAGE))
    r = c.get("/disputes")
    assert r.status_code == 200
    assert repo.get_auto_prep(db_path, EXCLUDED_ID) is None
    assert repo.list_packages(db_path, EXCLUDED_ID) == []


def test_auto_prep_never_approves_or_sends_through_the_inbox_either():
    fake = FakeClient(list_body=LIST_PAGE1)
    c, db_path = _iso_client_and_db(fake)
    repo.set_manual_fields(db_path, DID, {"tracking_number": {"value": "DEMO1", "confidence": "read"}})
    c.get("/disputes")
    rec = repo.get_auto_prep(db_path, DID)
    assert rec["status"] == "ready"
    assert repo.get_latest_valid_approval(db_path, rec["package_id"]) is None


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


@pytest.mark.hosted_spec
def test_hosted_inbox_prepares_manual_facts_without_model_or_approval(monkeypatch):
    calls = []
    from copilot.core import evidence_model
    monkeypatch.setattr(evidence_model.ai, "preflight", lambda: calls.append("preflight"))
    monkeypatch.setattr(evidence_model.ai, "run_with_docs", lambda *a, **kw: calls.append("model"))
    repo.insert_document(config.db_path(), DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    fake = FakeClient(list_body=LIST_PAGE1)
    c = _client(_app_with_fake(fake))
    r = c.get("/disputes")
    assert r.status_code == 200 and DID in r.text
    prep = repo.get_auto_prep(config.db_path(), DID)
    assert prep is not None and prep["package_id"] and calls == []
    pkg = repo.get_package(config.db_path(), prep["package_id"])
    assert pkg["source"] == "auto_manual" and auto_prep.HOSTED_AI_OFF_FLAG in pkg["review_flags"]
    assert repo.get_latest_valid_approval(config.db_path(), pkg["id"]) is None
    assert all(method == "GET" for method, _path, _op in fake.calls)
