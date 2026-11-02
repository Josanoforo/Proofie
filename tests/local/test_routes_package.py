"""Offline tests for package generation/review/edit/approval. The real model
is never called here - propose_package is monkeypatched for the AI-generation tests; everything else
(manual mode, editing, approval gating) needs no model at all.
"""
from _hosted import authenticated_client, reset_csrf
import json
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web import routes_package
from copilot.web.routes_disputes import get_client
from copilot.web.security import CSRF_COOKIE_NAME

REC = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"


@pytest.mark.parametrize("country,expected", [("MX","Mexico, updated 13 January 2025"),
                                              ("US","United States, updated 26 January 2026")])
def test_dr1b_policy_source_is_shared_by_case_and_package(web_mode, monkeypatch, country, expected):
    from copilot.core import checklist_input
    monkeypatch.setattr(checklist_input, "RULES_COUNTRY", country)
    monkeypatch.setattr(checklist_input, "_RULES_CACHE", [])
    c, db, token = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", True, [])
    for path in (f"/disputes/{DID}/workspace/send",f"/disputes/{DID}/packages/{package}"):
        response = c.get(path)
        assert response.status_code == 200
        assert "PayPal Seller Protection, " + expected in response.text
        assert routes_package.policy_source()["url"] in response.text


@pytest.mark.parametrize("action", ["add", "edit"])
def test_dr1b_blank_statement_renders_error_and_preserves_saved_claim(web_mode, monkeypatch, action):
    c, db, token = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, package, [{"evidence_index":0,"claim_index":0,"text":"Keep this synthetic statement.",
                                   "source_ids":[],"basis":"seller_statement"}])
    claim = repo.list_claims(db, package)[0]
    url = f"/disputes/{DID}/packages/{package}/claims" + (f"/{claim['id']}" if action == "edit" else "")
    result = c.post(url, data={"csrf_token":token,"text":"   "})
    assert result.status_code == 400
    assert "There is a problem" in result.text and "Enter a statement before saving." in result.text
    assert "Error:" in result.text
    assert [x["text"] for x in repo.list_claims(db, package)] == ["Keep this synthetic statement."]


def test_dr1b_triple_date_form_saves_iso_and_review_echo(web_mode, monkeypatch):
    c, db, token = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", True, [])
    result = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": package,
        "tracking_number": "DEMO-TEST-1", "carrier": "FedEx", "delivery_date": "2026-10-14"},
        follow_redirects=False)
    assert result.status_code == 303
    assert repo.get_manual_fields(db, DID)["delivery_date"]["value"] == "2026-10-14"
    page = c.get(f"/disputes/{DID}/packages/{package}")
    assert "14 October 2026" in page.text and "Pick the date in the calendar" in page.text
    assert re.search(r'<input type="date" id="field-delivery_date" name="delivery_date" value="2026-10-14"', page.text)
    assert 'name="delivery_date_day"' not in page.text


def test_native_date_picker_value_saves_and_an_invalid_one_shows_the_error(web_mode, monkeypatch):
    """Expected before the run: a valid ISO value saves as that ISO date (303); an impossible or badly shaped value gives
    400, the summary link #field-<name> (no -day), the same sentence as before and nothing saved; the earlier three-field
    post still saves the same ISO date."""
    c, db, token = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", True, [])
    base = {"csrf_token": token, "package_id": package, "tracking_number": "DEMO-TEST-9", "carrier": "FedEx"}
    set_test_csrf(c, token)
    ok = c.post(f"/disputes/{DID}/manual-fields", data={**base, "ship_date": "2026-10-01"}, follow_redirects=False)
    assert ok.status_code == 303 and repo.get_manual_fields(db, DID)["ship_date"]["value"] == "2026-10-01"
    repo.set_manual_fields(db, DID, {})
    for bad in ("2026-13-01", "14/10/2026", "2026-1-1"):
        set_test_csrf(c, token)
        result = c.post(f"/disputes/{DID}/manual-fields", data={**base, "ship_date": bad})
        assert result.status_code == 400 and 'href="#field-ship_date"' in result.text and "-day" not in result.text
        assert "Enter a real date using day, month and year." in result.text and repo.get_manual_fields(db, DID) == {}
    set_test_csrf(c, token)
    legacy = c.post(f"/disputes/{DID}/manual-fields", data={**base, "ship_date_day": "2", "ship_date_month": "10",
                                                            "ship_date_year": "2026"}, follow_redirects=False)
    assert legacy.status_code == 303 and repo.get_manual_fields(db, DID)["ship_date"]["value"] == "2026-10-02"


def test_dr1b_bad_dates_keep_values_and_do_not_replace_saved_fields(web_mode, monkeypatch):
    c, db, token = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", True, [])
    before = {"tracking_number": {"value": "OLD-DEMO", "confidence": "read"}}
    repo.set_manual_fields(db, DID, before)
    result = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": package,
        "tracking_number": "NEW-DEMO", "carrier": "FedEx", "delivery_date": "2026-02-31"})
    assert result.status_code == 400
    assert "There is a problem" in result.text and "Error:" in result.text
    assert 'value="2026-02-31"' in result.text and 'href="#field-delivery_date"' in result.text
    assert "Enter a real date using day, month and year." in result.text
    assert repo.get_manual_fields(db, DID) == before


def test_dr1b_delivery_before_shipping_has_errors_beside_both_dates(web_mode, monkeypatch):
    c, db, token = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", True, [])
    fields = {"csrf_token": token, "package_id": package, "tracking_number": "DEMO-1", "carrier": "FedEx"}
    fields.update({"ship_date": "2026-10-15", "delivery_date": "2026-10-14"})
    result = c.post(f"/disputes/{DID}/manual-fields", data=fields)
    assert result.status_code == 400
    # One message per field, each naming both dates; the old duplicated line is gone.
    assert "Delivery date must not be before shipping date." not in result.text
    delivery_msg = "Delivery date (14 October 2026) is before the ship date (15 October 2026). Change one of the dates."
    ship_msg = "Ship date (15 October 2026) is after the delivery date (14 October 2026). Change one of the dates."
    assert result.text.count(delivery_msg) >= 2 and result.text.count(ship_msg) >= 2        # summary entry + beside the field
    assert 'href="#field-ship_date"' in result.text and 'href="#field-delivery_date"' in result.text
    assert repo.get_manual_fields(db, DID) == {}


def test_dr1b_legacy_typed_date_is_rejected_not_guessed(web_mode, monkeypatch):
    c, db, token = _client(monkeypatch=monkeypatch)
    package = repo.insert_package(db, DID, "human_edit", True, [])
    response = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": package,
        "tracking_number": "DEMO-1", "carrier": "FedEx", "delivery_date": "14/10/2026"})
    assert response.status_code == 400 and "day, month and year" in response.text
    assert repo.get_manual_fields(db, DID) == {}


class FakeClient:
    """Also answers context.py's two GET lookups - empty, not an error, so fetch_context
    records a plain limitation rather than raising; these tests are about the package/approval flow, not
    about its own transaction-search/tracking logic (that is tests/local/test_context.py's job)."""
    def __init__(self, detail_body=WAITING):
        self.detail_body = detail_body

    def call(self, method, path, op, **kw):
        if op == "show_dispute":
            return self.detail_body
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _client(tmp_path=None, monkeypatch=None):
    # tmp_path/monkeypatch are accepted (unused) so every existing call site stays unchanged; conftest.py's
    # autouse _isolated_copilot_data fixture already redirects config.db_path()/uploads_dir() for us.
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return c, config.db_path(), token


# ---- manual mode: never calls the model -------------------------------------------------------
def test_manual_package_creation_never_calls_propose_package(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(routes_package, "propose_package", lambda *a, **kw: called.append(1))
    c, db_path, token = _client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and called == []
    pkgs = repo.list_packages(db_path, DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "human_edit"


def test_generate_with_no_readable_documents_falls_back_to_manual_without_calling_the_model(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(routes_package, "propose_package", lambda *a, **kw: called.append(1))
    c, db_path, token = _client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and called == []
    pkgs = repo.list_packages(db_path, DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "human_edit"


@pytest.mark.local_spec
def test_generate_falls_back_to_manual_when_the_model_is_unavailable(tmp_path, monkeypatch):
    """Found while implementing it - unlike auto_prep.py's background path, this
    route never caught propose_package() raising at all (preflight()'s RuntimeError when busy, or
    httpx's own exception when Ollama is simply unreachable) - a vendor explicitly clicking 'Generate
    evidence package' would have hit a raw 500, which the design explicitly forbids."""
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(routes_package, "propose_package",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("GPU/Ollama busy")))
    r = c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    pkg = repo.list_packages(db_path, DID)[0]
    assert pkg["source"] == "human_edit"
    assert pkg["review_flags"] == ["The AI assistant is not available right now. Continuing in manual "
                                   "mode - type the values yourself, or try again in a moment."]
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "start_manual_package_ai_unavailable" and e["detail"]["error"] == "GPU/Ollama busy"
              for e in entries)


@pytest.mark.local_spec
@pytest.mark.live_model
def test_generate_falls_back_to_manual_when_ollama_is_really_unreachable(tmp_path, monkeypatch):
    """The same fallback, end to end through the REAL propose_package()/pilot pipeline - COPILOT_OLLAMA_URL
    points at a closed local port (127.0.0.1:1, refused instantly), never the real Ollama - confirming the
    real on-screen text without ever touching or stopping anything real. Marked live_model so conftest.py's
    network block does not intercept this deliberate real-but-unreachable connection attempt."""
    monkeypatch.setenv("COPILOT_OLLAMA_URL", "http://127.0.0.1:1")
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    r = c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    pkg = repo.list_packages(db_path, DID)[0]
    assert pkg["source"] == "human_edit"
    assert "AI assistant is not available right now" in pkg["review_flags"][0]
    review = c.get(f"/disputes/{DID}/packages/{pkg['id']}")
    assert "The AI assistant is not available right now" in review.text


# ---- Vision suggestions for image documents, wired into both package-creation
# branches (AI-from-text, and the manual-shell fallback when there is no readable text at all) ----------
@pytest.mark.local_spec
def test_generate_with_a_text_document_also_attaches_vision_claims_from_an_image(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha2", None, False)
    monkeypatch.setattr(routes_package, "propose_package", lambda *a, **kw: _fake_rec())
    monkeypatch.setattr(routes_package, "claims_for_dispute_images",
                        lambda *a, **kw: [{"evidence_index": kw.get("evidence_index", 1), "claim_index": 0,
                                          "text": "A FedEx tracking label is visible.",
                                          "source_ids": ["doc_2"], "basis": "model_interpretation"}])
    c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = repo.list_packages(db_path, DID)[0]["id"]
    claims = repo.list_claims(db_path, pkg_id)
    assert any(c["text"] == "A FedEx tracking label is visible." and c["basis"] == "model_interpretation"
              for c in claims)
    review = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "A FedEx tracking label is visible." in review.text
    assert "Suggested" in review.text and "check against the source" in review.text


@pytest.mark.local_spec
def test_generate_with_only_an_image_still_attaches_vision_claims_to_the_manual_shell(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha2", None, False)
    monkeypatch.setattr(routes_package, "claims_for_dispute_images",
                        lambda *a, **kw: [{"evidence_index": 0, "claim_index": 0,
                                          "text": "A tracking number is partially visible.",
                                          "source_ids": ["doc_1"], "basis": "model_interpretation"}])
    r = c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    pkg = repo.list_packages(db_path, DID)[0]
    assert pkg["source"] == "human_edit"   # still a manual shell - vision only ever suggests, never AI-drafts
    claims = repo.list_claims(db_path, pkg["id"])
    assert any(c["text"] == "A tracking number is partially visible." for c in claims)


# ---- Confirming a vision reading unblocks a delivery
# assertion citing that image - a real "Entregado" screenshot the vendor has looked at must be able to
# support delivery, exactly like any other legible document, never a shortcut around the gating.
def test_confirm_reading_is_rejected_without_csrf(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha1", None, False)
    r = c.post(f"/disputes/{DID}/documents/{doc_id}/confirm-reading",
              data={"csrf_token": "forged", "field_name": "delivery_status", "value": "Entregado"})
    assert r.status_code == 403
    assert repo.get_confirmed_readings(db_path, doc_id) == {}


def test_confirm_reading_rejects_an_unrecognised_field_name(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha1", None, False)
    r = c.post(f"/disputes/{DID}/documents/{doc_id}/confirm-reading",
              data={"csrf_token": token, "field_name": "price_tier", "value": "gold"})
    assert r.status_code == 400
    assert repo.get_confirmed_readings(db_path, doc_id) == {}


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.local_spec
def test_confirming_a_vision_reading_unblocks_a_delivery_assertion_citing_that_image(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha1", None, False)
    monkeypatch.setattr(routes_package, "claims_for_dispute_images",
                        lambda *a, **kw: [{"evidence_index": 0, "claim_index": 0,
                                          "text": "The status reads Entregado.", "source_ids": [f"doc_{doc_id}"],
                                          "basis": "model_interpretation", "suggested_field": "delivery_status",
                                          "suggested_value": "Entregado"}])
    c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = repo.list_packages(db_path, DID)[0]["id"]

    review = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Confirm this reading" in review.text and "Check it against the image." in review.text
    token2 = review.cookies.get(CSRF_COOKIE_NAME)

    # Unconfirmed: the claim asserts delivery but cites only the (not yet citable) image - blocked.
    blocked = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token2})
    assert blocked.status_code == 400
    assert "Statement 1" in blocked.text and "Change the wording" in blocked.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None

    confirmed = c.post(f"/disputes/{DID}/documents/{doc_id}/confirm-reading",
                       data={"csrf_token": token2, "field_name": "delivery_status", "value": "Entregado",
                             "package_id": str(pkg_id)}, follow_redirects=False)
    assert confirmed.status_code == 303
    readings = repo.get_confirmed_readings(db_path, doc_id)
    assert readings["delivery_status"]["value"] == "Entregado"
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "confirm_vision_reading" and e["actor"] == "vendor"
              and e["detail"]["field_name"] == "delivery_status" for e in entries)

    review2 = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Confirmed by you: Delivery status Entregado." in review2.text
    token3 = review2.cookies.get(CSRF_COOKIE_NAME)

    # Now confirmed: the SAME claim is no longer unsupported - approval succeeds (the package's own
    # unrelated "no readable documents" notice from manual-mode creation still needs its own override
    # note, same as any other review_flag - that gate is not what this test is about).
    allowed = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
                     data={"csrf_token": token3, "sufficiency_override": "Reviewed the image myself"},
                     follow_redirects=False)
    assert allowed.status_code == 303, allowed.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.local_spec
def test_re_approving_after_a_confirmation_invalidates_the_prior_approval_dispute_wide(tmp_path, monkeypatch):
    """confirm-reading is not scoped to one package - any already-valid approval across the whole
    dispute no longer reflects what was actually reviewed, the same reasoning as attach_document."""
    c, db_path, token = _client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha1", None, False)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha2", "Carrier: Estafeta", True)
    monkeypatch.setattr(routes_package, "propose_package", lambda *a, **kw: _fake_rec())
    c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = repo.list_packages(db_path, DID)[0]["id"]
    approved = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token},
                      follow_redirects=False)
    assert approved.status_code == 303, approved.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None

    c.post(f"/disputes/{DID}/documents/{doc_id}/confirm-reading",
          data={"csrf_token": token, "field_name": "delivery_status", "value": "Entregado",
                "package_id": str(pkg_id)})
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None


def test_create_package_without_csrf_is_rejected(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": "forged"})
    assert r.status_code == 403
    assert repo.list_packages(db_path, DID) == []


# ---- AI generation (model monkeypatched - never the real Ollama) ------------------------------------
GOOD_OUT = {"proposed_evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "shipped",
                                  "tracking_info": [{"carrier_name": "MEX_ESTAFETA", "tracking_number": "DEMO1",
                                                     "source_ids": ["doc_1"]}],
                                  "claims": [{"text": "Shipped via Estafeta.", "source_ids": ["doc_1"],
                                             "basis": "verified"}]}],
           "missing_evidence": [], "conflicts": [], "recommended_next_step": "submit_evidence_for_human_approval",
           "reviewer_note_draft": "n"}


def _fake_rec(validation_ok=True, review_flags=None, output=GOOD_OUT):
    return {"validation_ok": validation_ok, "review_flags": review_flags or [], "model": "qwen3.5:9b",
           "prompt_sha256": "abc123", "raw_output": json.dumps(output), "effective_next_step":
           "submit_evidence_for_human_approval", "_output": output, "unsupported_delivery_assertions": []}


@pytest.mark.local_spec
def test_generate_with_a_readable_document_calls_the_model_and_persists_claims(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(routes_package, "propose_package", lambda *a, **kw: _fake_rec())
    r = c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    pkgs = repo.list_packages(db_path, DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "ai" and pkgs[0]["validation_ok"] is True
    claims = repo.list_claims(db_path, pkgs[0]["id"])
    assert len(claims) == 1 and claims[0]["text"] == "Shipped via Estafeta."


# ---- Full review: "an injection in a document" closes the loop end-to-end through the
# real routes (mirrors c03_other_order's own route-level test above) - pilot/test_step4_validation.py
# already proves the model/validator never obeys an injected instruction and flags it in review_flags;
# this proves that flag actually reaches the human on the review screen and blocks approval, the same way
# any other review_flag does (the hard block is generic, not injection-specific).
@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.local_spec
def test_an_ai_review_flag_from_a_document_injection_attempt_reaches_the_screen_and_blocks_approval(
        tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    injection_flag = "injection attempt detected in source documents (1 sentence(s))"
    monkeypatch.setattr(routes_package, "propose_package",
                        lambda *a, **kw: _fake_rec(review_flags=[injection_flag]))
    c.post(f"/disputes/{DID}/packages", data={"csrf_token": token})
    pkg_id = repo.list_packages(db_path, DID)[0]["id"]
    assert repo.get_package(db_path, pkg_id)["review_flags"] == [injection_flag]

    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert injection_flag in r.text   # the human sees exactly why, in plain text, not just a generic warning
    token = r.cookies.get(CSRF_COOKIE_NAME)   # the GET above issued a fresh one; use the one a real page would

    blocked = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token})
    assert blocked.status_code == 400
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None

    approved = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
                      data={"csrf_token": token, "sufficiency_override": "Reviewed the document myself"},
                      follow_redirects=False)
    assert approved.status_code == 303
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None


# ---- Full review: "an incomplete or illegible document" - the whole-document case
# (an image: document_ingest.py never OCRs in v1, so extraction_ok=False) through the real routes. Field-
# level illegibility (a document that DOES extract, but one field's value is garbled - checklist.py's own
# "present_unverifiable" branch, copilot/fixtures/m1/c06_illegible.json) is covered at the checklist unit
# level already; this is the other half (a document that extracts nothing at all) end to end: it must
# never crash, never be silently treated as if it said nothing was wrong, and never block manual entry.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32   # real magic bytes; content after them is irrelevant here


def test_an_unreadable_image_document_never_crashes_and_the_checklist_says_illegible_not_verified(
        tmp_path, monkeypatch):
    """A whole unreadable document makes
    its required fields illegible (present_unverifiable), never "missing" - "missing" would wrongly claim
    the document said nothing, when really we just could not read it."""
    c, db_path, token = _client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("photo.png", PNG_BYTES, "image/png")},
              data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    doc = repo.list_documents(db_path, DID)[0]
    assert doc["content_type"] == "image/png" and doc["extraction_ok"] is False

    monkeypatch.setattr(routes_package, "propose_package", lambda *a, **kw: called.append(1))
    called = []
    r = c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and called == []   # an unreadable document is never sent to the model
    pkg_id = repo.list_packages(db_path, DID)[0]["id"]
    assert repo.list_packages(db_path, DID)[0]["source"] == "human_edit"

    review = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert review.status_code == 200
    assert "photo.png" in review.text   # the vendor can still see it was uploaded
    assert "Proofie could not read the text in photo.png. Type the details yourself, or upload a PDF with selectable text." in review.text
    assert "not legible" in review.text   # checklist.py's own per-field message (unmodified)
    assert "No tracking number found" not in review.text   # never the "missing" wording for this case

    token2 = review.cookies.get(CSRF_COOKIE_NAME)
    r = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token2, "package_id": str(pkg_id),
                                                       "tracking_number": "DEMO1"}, follow_redirects=False)
    assert r.status_code == 303


# ---- review screen ------------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_review_screen_shows_checks_and_claims(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(routes_package, "propose_package", lambda *a, **kw: _fake_rec())
    c.post(f"/disputes/{DID}/packages", data={"csrf_token": token})
    pkg_id = repo.list_packages(db_path, DID)[0]["id"]
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert r.status_code == 200
    assert "Shipped via Estafeta." in r.text
    assert "Tracking number" in r.text   # from the checklist stub


def test_review_keeps_claim_source_excerpt_original_and_send_recipient_together(monkeypatch):
    import hashlib
    from copilot.core.raw_storage import write_raw
    c, db_path, token = _client(monkeypatch=monkeypatch)
    original = b"synthetic original receipt bytes"
    sha = hashlib.sha256(original).hexdigest()
    document_id = repo.insert_document(db_path, DID, "receipt.pdf", "application/pdf", len(original), sha,
                                      "Carrier accepted parcel at depot", True)
    write_raw(config.uploads_dir(), sha, original)
    package_id = repo.insert_package(db_path, DID, "human_edit", True, [])
    repo.insert_claims(db_path, package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "The carrier accepted the parcel.", "source_ids": [f"doc_{document_id}"], "basis": "verified"}])
    page = c.get(f"/disputes/{DID}/packages/{package_id}")
    assert page.status_code == 200
    assert "The carrier accepted the parcel." in page.text
    assert "Extracted text excerpt — not the original" in page.text
    assert "Carrier accepted parcel at depot" in page.text
    assert (f"/disputes/{DID}/documents/{document_id}/original" in page.text) == (not config.hosted_demo())   # hidden in the hosted demo
    assert f"To PayPal · dispute {DID} · package v1" in page.text


def test_review_screen_for_missing_package_says_so(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    r = c.get(f"/disputes/{DID}/packages/99999")
    assert r.status_code == 404 and "could not be found" in r.text


# ---- editing a claim invalidates any prior approval --------------------------------------------------
def test_editing_a_claim_updates_text_and_invalidates_prior_approval(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0, "text": "original",
                                          "source_ids": [], "basis": "seller_statement"}])
    claim_id = repo.list_claims(db_path, pkg_id)[0]["id"]
    approval_id = repo.insert_approval(db_path, pkg_id, "fp1", "vendor")
    repo.log_action(db_path, "vendor", "approve_package", DID, {"package_id": pkg_id})
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/claims/{claim_id}",
              data={"text": "edited text", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert repo.list_claims(db_path, pkg_id)[0]["text"] == "edited text"
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None   # invalidated
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert page.status_code == 200
    assert page.text.count('data-state="approval-cleared"') == 1
    assert 'data-state="unsupported-delivery-approval-block"' not in page.text
    assert 'data-state="approval-blocked-and-cleared"' not in page.text


def test_add_claim_manually_appends_as_seller_statement(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=True, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/claims",
              data={"text": "I shipped it myself on 2026-10-01.", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    claims = repo.list_claims(db_path, pkg_id)
    assert len(claims) == 1 and claims[0]["basis"] == "seller_statement"


# ---- approval gating: the hard block (unsupported delivery assertion) vs. advisory (review_flags) -----
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_approve_is_blocked_by_an_unsupported_delivery_assertion_never_overridable(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1",
                         "Carrier: Estafeta\n2026-09-20: picked up", True)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0,
                                          "text": "Delivered on 2026-09-20.", "source_ids": [],
                                          "basis": "seller_statement"}])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "sufficiency_override": "I say it's fine"})
    assert r.status_code == 400
    assert "Statement 1 says “Delivered”, but no document it cites shows that." in r.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None   # override does NOT bypass this


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_review_shows_unsupported_delivery_block_beside_approval_with_cited_original(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1",
                                 "Carrier: Estafeta\n2026-09-20: picked up", True)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "Delivered on 2026-09-20.", "source_ids": [f"doc_{doc_id}"],
        "basis": "seller_statement"}])
    r = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert r.status_code == 200
    assert 'data-state="unsupported-delivery-approval-block"' in r.text
    assert "Approval is blocked until these delivery statements are corrected" in r.text
    assert "no document it cites shows that" in r.text
    assert "Source file: label.pdf" in r.text
    assert (f"/documents/{doc_id}/original" in r.text) == (not config.hosted_demo())
    assert r.text.index('id="approval"') < r.text.index('data-state="unsupported-delivery-approval-block"')
    assert r.text.count('data-state="unsupported-delivery-approval-block"') == 1
    assert 'data-state="approval-cleared"' not in r.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None


def test_blocked_and_cleared_approval_is_one_panel_with_prior_version_and_source(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.delenv("COPILOT_FEATURE_ACTIVE_APPROVAL", raising=False)      # this test drives the plain approve POST; the active-approval page has its own tests
    from copilot.core.raw_storage import write_raw
    c, db_path, token = _client(tmp_path, monkeypatch)
    original = b"synthetic receipt: carrier accepted parcel at depot"
    sha = hashlib.sha256(original).hexdigest()
    doc_id = repo.insert_document(db_path, DID, "receipt.pdf", "application/pdf", len(original), sha,
                                  "Carrier accepted parcel at depot", True)
    write_raw(config.uploads_dir(), sha, original)
    # Three persisted package versions make the prior approval's version visible as v3.
    repo.insert_package(db_path, DID, "human_edit", True, [])
    repo.insert_package(db_path, DID, "human_edit", True, [])
    pkg_id = repo.insert_package(db_path, DID, "human_edit", True, [])
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "The carrier accepted the parcel.", "source_ids": [f"doc_{doc_id}"],
        "basis": "verified"}])
    repo.set_manual_fields(db_path, DID, {
        "tracking_number": {"value": "DEMO-FDX-000003", "confidence": "read"},
        "carrier": {"value": "FedEx", "confidence": "read"}})
    approved = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token},
                      follow_redirects=False)
    assert approved.status_code == 303, approved.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None

    claim_id = repo.list_claims(db_path, pkg_id)[0]["id"]
    edited = c.post(f"/disputes/{DID}/packages/{pkg_id}/claims/{claim_id}",
                    data={"text": "Delivered on 2026-09-20.", "csrf_token": token},
                    follow_redirects=False)
    assert edited.status_code == 303
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert page.status_code == 200
    assert page.text.count('data-state="approval-blocked-and-cleared"') == 1
    assert 'data-state="unsupported-delivery-approval-block"' not in page.text
    assert 'data-state="approval-cleared"' not in page.text
    panel = page.text.split('data-state="approval-blocked-and-cleared"', 1)[1].split("</section>", 1)[0]
    assert "approval for package v3 was cleared because the content changed" in panel
    assert "no document it cites shows that" in panel
    assert "This block cannot be overridden" in panel
    assert "approve again" in panel
    assert "Source file: receipt.pdf" in page.text
    assert (f"/disputes/{DID}/documents/{doc_id}/original" in page.text) == (not config.hosted_demo())
    assert page.text.index('id="approval"') < page.text.index('data-state="approval-blocked-and-cleared"')


# ---- raw-HTML error responses must escape attacker/model-influenced text, not interpolate it -----
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_approve_unsupported_assertion_page_escapes_the_claim_text(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1",
                         "Carrier: Estafeta\n2026-09-20: picked up", True)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0,
                                          "text": "Delivered <script>evil</script> on 2026-09-20.",
                                          "source_ids": [], "basis": "seller_statement"}])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "sufficiency_override": "I say it's fine"})
    assert r.status_code == 400
    assert "<script>evil</script>" not in r.text                     # The statement text is no longer echoed at all
    assert "Statement 1 says “Delivered”" in r.text


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_approve_conflict_page_escapes_a_malicious_filename(tmp_path, monkeypatch):
    """A filename reaching this far (bypassing routes_documents.sanitize_filename) must still be escaped -
    defense in depth, the same reasoning as the dispute id validation in copilot/web/security.py."""
    c, db_path, token = _c03_client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, C03_DID, "<script>evil</script>.pdf", "application/pdf", 10,
                                  "sha1", C03_OTHER_ORDER_TEXT, True)
    pkg_id = repo.insert_package(db_path, C03_DID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0,
                                          "text": "Shipped, tracking as attached.",
                                          "source_ids": [f"doc_{doc_id}"], "basis": "seller_statement"}])
    r = c.post(f"/disputes/{C03_DID}/packages/{pkg_id}/approve", data={"csrf_token": token})
    assert r.status_code == 400
    assert "conflicts with PayPal" in r.text   # confirms the conflict branch (with the filename) ran
    assert "<script>evil</script>" not in r.text
    assert "&lt;script&gt;evil&lt;/script&gt;" in r.text


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_approve_requires_an_override_note_when_review_flags_are_present(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="ai", validation_ok=True,
                                 review_flags=["PayPal usually asks for proof of delivery"])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token})
    assert r.status_code == 400
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_approve_succeeds_with_an_override_note_and_it_is_audited(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="ai", validation_ok=True,
                                 review_flags=["PayPal usually asks for proof of delivery"])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "sufficiency_override": "Buyer confirmed by phone"},
              follow_redirects=False)
    assert r.status_code == 303
    approval = repo.get_latest_valid_approval(db_path, pkg_id)
    assert approval is not None and approval["sufficiency_override"] == "Buyer confirmed by phone"
    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "approve_package" and e["detail"]["sufficiency_override"] is True
              for e in entries)


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_approve_succeeds_cleanly_with_no_flags_and_no_override_needed(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/approve", data={"csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None


# ---- manual shipment-fields capture, feeding the real checklist ---------------------------
def test_manual_fields_without_csrf_is_rejected(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/manual-fields",
              data={"csrf_token": "forged", "package_id": pkg_id, "tracking_number": "DEMO1"})
    assert r.status_code == 403
    assert repo.get_manual_fields(db_path, DID) == {}


def test_manual_fields_are_saved_and_feed_into_the_real_checklist(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/manual-fields",
              data={"csrf_token": token, "package_id": pkg_id, "tracking_number": "DEMO-MANUAL-1",
                   "carrier": "", "delivery_status": ""},
              follow_redirects=False)
    assert r.status_code == 303
    saved = repo.get_manual_fields(db_path, DID)
    assert saved == {"tracking_number": {"value": "DEMO-MANUAL-1", "confidence": "read"}}

    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert page.status_code == 200
    assert "DEMO-MANUAL-1" in page.text


# ---- Never present a manual/confirmed-image value as if it were
# independently read - the checklist's own message (unmodified) is augmented here,
# in the template/route only, with who actually supplied the value ----------------------------------
def test_manual_field_check_shows_entered_by_you(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    c.post(f"/disputes/{DID}/manual-fields",
          data={"csrf_token": token, "package_id": pkg_id, "tracking_number": "DEMO-MANUAL-1"})
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Entered by you" in page.text


def test_confirmed_image_check_shows_confirmed_by_you_from_the_filename(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 10, "sha1", None, False)
    repo.confirm_document_reading(db_path, doc_id, "delivery_status", "Entregado")
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Confirmed by you from photo.png" in page.text


def test_manual_fields_form_prefills_with_previously_saved_values(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    repo.set_manual_fields(db_path, DID, {"carrier": {"value": "Estafeta", "confidence": "read"}})
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert 'value="Estafeta"' in page.text


# ---- attach/detach a document to a package's send ------------------------------------------
PDF_BYTES = b"%PDF-1.7\nnot a fully valid pdf body, just enough to be detected by its magic header\n"


def test_attach_and_detach_a_document_round_trips_and_is_audited(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    attachments = repo.list_attachments(db_path, pkg_id)
    assert len(attachments) == 1 and attachments[0]["filename"] == "label.pdf"
    page = c.get(f"/disputes/{DID}/packages/{pkg_id}")
    assert "Remove from the response" in page.text and "Detach from the response" not in page.text
    token = page.cookies.get(CSRF_COOKIE_NAME)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/detach/{doc_id}", data={"csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    assert repo.list_attachments(db_path, pkg_id) == []

    entries = repo.list_audit(db_path, DID)
    assert any(e["action"] == "attach_document" for e in entries)
    assert any(e["action"] == "detach_document" for e in entries)


def test_attaching_a_document_invalidates_a_prior_approval(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
          data={"csrf_token": token}, follow_redirects=False)
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    repo.insert_approval(db_path, pkg_id, "fp1", "vendor")
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None

    c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token})
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None


def test_attach_without_csrf_is_rejected(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
          data={"csrf_token": token})
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": "forged"})
    assert r.status_code == 403
    assert repo.list_attachments(db_path, pkg_id) == []


def test_attach_unknown_document_is_a_plain_404(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/999999", data={"csrf_token": token})
    assert r.status_code == 404


def test_attach_refuses_when_the_combined_total_exceeds_50mb(tmp_path, monkeypatch):
    c, db_path, token = _client(tmp_path, monkeypatch)
    c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
          data={"csrf_token": token}, follow_redirects=False)
    doc_id = repo.list_documents(db_path, DID)[0]["id"]
    # A second, already-attached document sized so adding even this tiny PDF pushes the combined total
    # over PayPal's 50 MB per-call limit.
    big_id = repo.insert_document(db_path, DID, "big.pdf", "application/pdf", 50 * 1024 * 1024 - 50,
                                  "sha-big", "x", True)
    pkg_id = repo.insert_package(db_path, DID, source="human_edit", validation_ok=False, review_flags=[])
    repo.attach_document(db_path, pkg_id, big_id)

    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/attach/{doc_id}", data={"csrf_token": token})
    assert r.status_code == 400
    assert "Cannot attach" in r.text
    assert len(repo.list_attachments(db_path, pkg_id)) == 1   # the new one was never added


# ---- a checklist conflict (c03_other_order scenario) through the real routes ----------------
# Mirrors copilot/fixtures/m1/c03_other_order.json: a document whose tracking number, carrier and postal
# code belong to a DIFFERENT order than the one PayPal has on file for this dispute.
C03_DID = "PP-D-DEMO-0001"
C03_RAW_DISPUTE = {
    "dispute_id": C03_DID, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "INQUIRY",
    "dispute_amount": {"currency_code": "MXN", "value": "4500.00"},
    "disputed_transactions": [{"seller_transaction_id": "DEMOTXN0000000001"}],
    # This fake models a sendable case, not an expired dispute. Keep its deadline
    # in the future by code; dedicated overdue tests retain their explicit dates.
    "create_time": "2026-09-25T16:00:00Z", "seller_response_due_date": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    "links": [{"rel": "provide_evidence",
              "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{C03_DID}/provide-evidence",
              "method": "POST"}],
    "evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}],
}
C03_TRANSACTION_SEARCH = {"transaction_details": [{
    "transaction_info": {"transaction_id": "DEMOTXN0000000001",
                         "transaction_initiation_date": "2026-09-10T15:00:00+0000",
                         "transaction_amount": {"currency_code": "MXN", "value": "4500.00"}},
    "shipping_info": json.loads(
        (Path(__file__).resolve().parents[2] / "copilot" / "fixtures" / "m1" / "c03_other_order.json")
        .read_text(encoding="utf-8")
    )["paypal"]["transactions"]["transaction_details"][0]["shipping_info"]}]}
C03_TRACKING = {"trackers": [{"transaction_id": "DEMOTXN0000000001", "tracking_number": "DEMO-FDX-000001",
                             "carrier": "FEDEX", "status": "DELIVERED"}]}
# "Another order"'s document: real tracking/carrier/postal labels, but values that belong to a DIFFERENT
# transaction than the one PayPal has on file above (same shape as doc_estafeta_capture.txt's labels).
C03_OTHER_ORDER_TEXT = ("SYNTHETIC. Estafeta capture for another order.\n"
                       "Guia: DEMO-EST-999999\nEstatus: Entregado\nFecha de envio: 2026-09-11\n"
                       "Fecha de entrega: 2026-09-14\nCodigo postal destino: 03100\nCaptura: 2026-09-30\n")


class C03FakeClient:
    def call(self, method, path, op, **kw):
        if op == "show_dispute":
            return C03_RAW_DISPUTE
        if op == "context_transaction_search":
            return C03_TRANSACTION_SEARCH
        if op == "context_tracking":
            return C03_TRACKING
        raise AssertionError(f"unexpected op {op}")


def _minimal_pdf_with_text(text):
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode("latin-1")
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
           b"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 100]/Contents 5 0 R>>",
           b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
           b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)


def _c03_client(tmp_path=None, monkeypatch=None):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: C03FakeClient()
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    return c, config.db_path(), token


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_c03_other_order_conflict_gating_end_to_end(tmp_path, monkeypatch):
    c, db_path, token = _c03_client(tmp_path, monkeypatch)
    pdf = _minimal_pdf_with_text(C03_OTHER_ORDER_TEXT.replace("\n", "  "))
    r = c.post(f"/disputes/{C03_DID}/documents", files={"file": ("other_order.pdf", pdf, "application/pdf")},
              data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    doc_id = repo.list_documents(db_path, C03_DID)[0]["id"]

    r = c.post(f"/disputes/{C03_DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])

    # Step 1: an honest, non-delivery claim citing the conflicting document - the checklist-conflict gate applies, the delivery-assertion gate does not.
    repo.insert_claims(db_path, pkg_id, [{"evidence_index": 0, "claim_index": 0,
                                          "text": "Shipped via Estafeta, tracking DEMO-EST-999999.",
                                          "source_ids": [f"doc_{doc_id}"], "basis": "verified"}])

    # The checklist must actually show the conflicts, as the fixture expects.
    review = c.get(f"/disputes/{C03_DID}/packages/{pkg_id}")
    assert "conflict" in review.text
    token = review.cookies.get(CSRF_COOKIE_NAME)   # the GET above issued a fresh one; use the one a real page would

    # Approving without reviewing the conflict is refused.
    r = c.post(f"/disputes/{C03_DID}/packages/{pkg_id}/approve", data={"csrf_token": token})
    assert r.status_code == 400
    assert "conflicts with PayPal" in r.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None

    # Choosing "approve anyway, noting the discrepancy" (one of the three explicit options) succeeds.
    # (A manual package also starts with its own unrelated "nothing entered yet" notice - sufficiency_
    # override clears that pre-existing gate, independently of conflict_note clearing the conflict gate.)
    r = c.post(f"/disputes/{C03_DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "conflict_note": "Tracking may be misfiled; shipped status only.",
                   "sufficiency_override": "Manual package reviewed by the vendor."},
              follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None
    entries = repo.list_audit(db_path, C03_DID)
    assert any(e["action"] == "approve_despite_conflict" and "tracking_number" in e["detail"]["conflicts"]
              for e in entries)
    claim_texts = [cl["text"] for cl in repo.list_claims(db_path, pkg_id)]
    assert any("misfiled" in t for t in claim_texts)   # the note is recorded in what would actually be sent

    # Step 2: editing that same claim to assert delivery, still citing the conflicting
    # document, is blocked - unconditionally, even with a conflict_note this time.
    claim_id = repo.list_claims(db_path, pkg_id)[0]["id"]
    c.post(f"/disputes/{C03_DID}/packages/{pkg_id}/claims/{claim_id}",
          data={"text": "Delivered on 2026-09-14.", "csrf_token": token}, follow_redirects=False)
    r = c.post(f"/disputes/{C03_DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "conflict_note": "I know, sending anyway."})
    assert r.status_code == 400
    assert "Statement 1 relies on a document that does not match PayPal's data for the tracking number." in __import__("html").unescape(r.text)
    assert repo.get_latest_valid_approval(db_path, pkg_id) is None

    # Step 3: resolve for real - remove the conflicting document AND fix the wording (removing the
    # document alone does not un-ring a claim that still asserts delivery with no source at all).
    r = c.post(f"/disputes/{C03_DID}/documents/{doc_id}/exclude", data={"csrf_token": token},
              follow_redirects=False)
    assert r.status_code == 303
    c.post(f"/disputes/{C03_DID}/packages/{pkg_id}/claims/{claim_id}",
          data={"text": "No longer shipped via this document; see updated shipment details.",
               "csrf_token": token}, follow_redirects=False)
    r = c.post(f"/disputes/{C03_DID}/packages/{pkg_id}/approve",
              data={"csrf_token": token, "sufficiency_override": "Resolved and re-reviewed."},
              follow_redirects=False)
    assert r.status_code == 303, r.text
    assert repo.get_latest_valid_approval(db_path, pkg_id) is not None


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf


def test_dr1b_delivery_before_payment_keeps_fields_and_links_both_dates(web_mode, monkeypatch):
    c, db, token = _client(monkeypatch=monkeypatch)
    package=repo.insert_package(db,DID,"human_edit",True,[])
    before={"tracking_number":{"value":"OLD-DEMO","confidence":"read"}}
    repo.set_manual_fields(db,DID,before)
    from copilot.core.context import TransactionContext, Field
    monkeypatch.setattr(routes_package,"fetch_context",lambda *a:TransactionContext({"payment_date":Field("2026-10-15T12:00:00Z","paypal_transaction_search")}))
    response=c.post(f"/disputes/{DID}/manual-fields",data={"csrf_token":token,"package_id":package,
        "tracking_number":"NEW-DEMO-2","carrier":"FedEx","delivery_date":"2026-10-14"})
    assert response.status_code == 400
    assert 'href="#field-delivery_date"' in response.text and 'href="#field-payment_date"' in response.text
    assert response.text.count("Delivery date must not be before payment date.") >= 3
    assert 'value="2026-10-14"' in response.text and 'value="NEW-DEMO-2"' in response.text
    assert repo.get_manual_fields(db,DID) == before


@pytest.mark.parametrize("action,show",[("check_status",True),("none",False),("add_document",False)])
def test_dr1b_b7_delivery_scan_hint_uses_existing_check_not_a_new_gate(monkeypatch,action,show):
    from copilot.core.context import TransactionContext
    from copilot.core.checklist import Check
    c,db,token=_client(monkeypatch=monkeypatch)
    pid=repo.insert_package(db,DID,"human_edit",False,[])
    checks=[Check("delivery_status","Delivery status","missing" if action!="none" else "verified",
                  "app_quality_rule","Synthetic",[],action,False,"Synthetic existing status check.")]
    monkeypatch.setattr(routes_package,"_checks_for",lambda *args:(checks,TransactionContext()))
    page=c.get(f"/disputes/{DID}/packages/{pid}")
    assert page.status_code == 200
    hint="If the deadline allows, wait for the carrier's delivery scan before sending."
    assert (hint in page.text) is show
    assert repo.get_latest_valid_approval(db,pid) is None
