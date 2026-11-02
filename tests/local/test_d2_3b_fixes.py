"""The fixes after the full review: injected or illegible
documents can never source a delivery assertion; Check again pairs each recovery GET to its own attempt;
the MCP approval state; the conflict half of the generation-time flag (UnknownCountryError path); and the
setup page does not depend on the client's error text). DOM + DB + effect at the PayPal double; no PayPal write,
no `.env` access (the real SandboxClient is replaced wherever the app would build one).
"""
from _hosted import authenticated_client, reset_csrf
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_d2_3_open_items as O
import test_integral_review as I
from _dom import parse, state
from copilot import config
from copilot.core import auto_prep
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist import UnknownCountryError
from copilot.core.disputes import normalize
from copilot.core.send import approval_fingerprint_with_attachments, build_request_input, sha256_json
from copilot.db import repo
from copilot.web import routes_package, routes_send

DID = I.DID
_no_real_waiting = I._no_real_waiting
TRUST_FLAG = "Delivery assertion citing a document the checks do not trust"


def _flags(pkg_id) -> list:
    return repo.get_package(config.db_path(), pkg_id)["review_flags"]


def _generate_by_hand(c, monkeypatch, tmp_path, doc_name, claims, notes, **output_kw):
    I._upload(c, doc_name)
    I._install_model(monkeypatch, tmp_path, lambda docs: I._output(docs, claims, notes, **output_kw))
    return I._pkg_id(I._post(c, f"/disputes/{DID}/packages"))


def _generate_by_auto_prep(paypal, monkeypatch, tmp_path, doc_name, claims, notes):
    c = I._client(paypal)
    I._upload(c, doc_name)
    I._install_model(monkeypatch, tmp_path, lambda docs: I._output(docs, claims, notes))
    db = config.db_path()
    norm = normalize(paypal.call("GET", "x", "show_dispute"), datetime.now().astimezone())
    version = auto_prep.compute_version_hash(db, norm)
    token = repo.claim_auto_prep(db, DID, version)
    auto_prep.prepare_dispute(DID, norm, paypal, db, version, token)
    prep = repo.get_auto_prep(db, DID)
    assert prep["status"] == "ready" and prep["package_id"], prep
    return c, prep["package_id"]


# ==== a delivery claim citing the injection document is flagged and can ============================
@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.parametrize("how", ["by_hand", "auto_prep"])
@pytest.mark.local_spec
def test_f1a_a_delivery_claim_citing_the_injection_document_is_flagged_and_can_never_be_approved(how, monkeypatch, tmp_path):
    """"The package was delivered." cites 04_injected_instruction.pdf, whose injected sentence contains the word
    "delivered". Before the fix that literal match made the claim pass; now the document is untrusted (its
    document_integrity check is not verified) so it can not source the assertion at all."""
    paypal = I.PayPalDouble()
    claims = [("The package was delivered.", "seller_statement")]
    if how == "by_hand":
        c = I._client(paypal)
        pkg_id = _generate_by_hand(c, monkeypatch, tmp_path, "04_injected_instruction.pdf", claims, "FedEx proof attached.")
    else:
        c, pkg_id = _generate_by_auto_prep(paypal, monkeypatch, tmp_path, "04_injected_instruction.pdf", claims, "FedEx proof attached.")
    flag = f'{TRUST_FLAG} (it contains text that reads like instructions, or could not be read): "The package was delivered."'
    assert flag in _flags(pkg_id)                                                   # told in words at generation
    box = state(parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text), "review-flags")
    assert box is not None and "The package was delivered." in box.text()
    for note in ("", "I reviewed it myself and want to proceed."):                  # not overridable
        refused = I._post(c, f"/disputes/{DID}/packages/{pkg_id}/approve", {"sufficiency_override": note})
        assert refused.status_code == 400, note
    assert repo.get_latest_valid_approval(config.db_path(), pkg_id) is None         # DB: no approval row
    assert I._send(c, pkg_id).status_code == 400 and paypal.posts == 0              # effect: nothing sent


# ==== notes that assert delivery with only the injection document behind them ======================
@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.parametrize("how", ["by_hand", "auto_prep"])
@pytest.mark.local_spec
def test_f1b_notes_that_assert_delivery_with_only_the_injection_document_behind_them_are_flagged(how, monkeypatch, tmp_path):
    """The model's `notes` are checked against a corpus; that corpus no longer includes untrusted documents. (The notes
    field itself is not stored or sent - what is sent is built from the stored claims - so the contract here is the
    generation-time flag and the open-notice gate: approval without a justification is refused.)"""
    paypal = I.PayPalDouble()
    claims = [("Shipped via FedEx.", "seller_statement")]
    notes = "Marked as delivered."
    if how == "by_hand":
        c = I._client(paypal)
        pkg_id = _generate_by_hand(c, monkeypatch, tmp_path, "04_injected_instruction.pdf", claims, notes)
    else:
        c, pkg_id = _generate_by_auto_prep(paypal, monkeypatch, tmp_path, "04_injected_instruction.pdf", claims, notes)
    assert any("Marked as delivered." in f and "delivery" in f.lower() for f in _flags(pkg_id)), _flags(pkg_id)
    box = state(parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text), "review-flags")
    assert box is not None and "Marked as delivered." in box.text()
    assert I._post(c, f"/disputes/{DID}/packages/{pkg_id}/approve").status_code == 400
    assert repo.get_latest_valid_approval(config.db_path(), pkg_id) is None
    assert paypal.posts == 0


# ==== a genuine verified document that says delivered still supports the claim =====================
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_f1c_a_genuine_verified_document_that_says_delivered_still_supports_the_claim(monkeypatch, tmp_path):
    paypal = I.PayPalDouble()
    c = I._client(paypal)
    pkg_id = _generate_by_hand(c, monkeypatch, tmp_path, "01_coherent_delivery.pdf",
                               [("The package was delivered.", "seller_statement")], "FedEx delivery proof attached.")
    assert not [f for f in _flags(pkg_id) if "delivery assertion" in f.lower()]     # no false block
    assert I._approve(c, pkg_id).status_code == 303
    assert repo.get_latest_valid_approval(config.db_path(), pkg_id) is not None
    assert paypal.posts == 0


# ==== honest partial claims citing the injection document stay approvable ==========================
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_f1d_honest_partial_claims_citing_the_injection_document_stay_approvable(monkeypatch, tmp_path):
    paypal = I.PayPalDouble()
    c = I._client(paypal)
    pkg_id = _generate_by_hand(c, monkeypatch, tmp_path, "04_injected_instruction.pdf",
                               [("Shipped via FedEx on 2026-09-11.", "seller_statement"),
                                ("The package is in transit.", "seller_statement")], "FedEx tracking shows it in transit.")
    assert not [f for f in _flags(pkg_id) if "delivery assertion" in f.lower()]     # partial claims are never blocked
    # (this document says "in transit" while PayPal's tracker says delivered, so the checklist conflict that is
    # unrelated to the injected document asks for its own explicit note, exactly as it always did)
    approved = I._post(c, f"/disputes/{DID}/packages/{pkg_id}/approve",
                       {"sufficiency_override": "Reviewed.", "conflict_note": "The document shows it in transit."})
    assert approved.status_code == 303, approved.text
    assert repo.get_latest_valid_approval(config.db_path(), pkg_id) is not None
    assert paypal.posts == 0


# ============================================================================== the approval gate itself
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_f1_approval_refuses_a_stored_delivery_claim_citing_an_untrusted_document_even_when_typed_by_hand():
    """Not only AI drafts: a package whose stored claim cites the injection document (however it got there) is blocked
    at approval, unconditionally, and the same wording about a verified document is not."""
    paypal = I.PayPalDouble()
    c = I._client(paypal)
    injected = I._upload(c, "04_injected_instruction.pdf")
    genuine = I._upload(c, "01_coherent_delivery.pdf")
    bad, good = I._manual_package(c), I._manual_package(c)
    repo.insert_claims(config.db_path(), bad, [{"evidence_index": 0, "claim_index": 0, "text": "Delivered on 2026-09-14.",
                                                 "source_ids": [f"doc_{injected}"], "basis": "verified"}])
    repo.insert_claims(config.db_path(), good, [{"evidence_index": 0, "claim_index": 0, "text": "Delivered on 2026-09-14.",
                                                  "source_ids": [f"doc_{genuine}"], "basis": "verified"}])
    refused = I._approve(c, bad, "Reviewed.")
    assert refused.status_code == 400 and "Statement 1 cites 04_injected_instruction.pdf" in refused.text.replace("&#34;", '"')
    assert repo.get_latest_valid_approval(config.db_path(), bad) is None
    # (the injected document also says "in transit" while PayPal's tracker says delivered: that checklist conflict has
    # nothing to do with the injected document and asks for its own explicit note, as it always did)
    ok = I._post(c, f"/disputes/{DID}/packages/{good}/approve",
                 {"sufficiency_override": "Reviewed.", "conflict_note": "One document shows it in transit."})
    assert ok.status_code == 303, ok.text
    assert paypal.posts == 0


# ==== two attempts ===============================================================================
def _two_attempts(paypal, c):
    """THIS package's attempt (real send, lost response) plus another package's older attempt with a stored request."""
    pkg_id = O._approved_manual_package(c)
    assert I._send(c, pkg_id).status_code == 202
    other_req = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                "evidence_info": {"tracking_info": [{"tracking_number": "OTHER-1"}]}, "notes": "o"}]}
    old = (datetime.now().astimezone() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    store = SQLiteAttemptStore(config.db_path())
    store.claim(DID, "another-packages-sha")
    store.write(DID, "another-packages-sha", {"state": "sent_unconfirmed", "request_id": "req-other",
                                             "request_input": other_req, "ts": old})
    store.close()
    return pkg_id


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.parametrize("script,expected", [(["ok", "fail"], "unreachable"), (["fail", "ok"], "answered")])
def test_f2_each_get_is_paired_to_its_own_attempt_in_either_order(script, expected):
    paypal = O.ScriptedGets(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _two_attempts(paypal, c)
    paypal.script = list(script)
    dom = parse(O._check(c, pkg_id).text)
    if expected == "unreachable":                   # THIS attempt's GET (the second one) failed
        assert state(dom, "paypal-unreachable") is not None and dom.find("h1").text() == "Could not check"
    else:                                           # THIS attempt's GET answered; the other attempt's failure is not ours
        assert dom.find("h1").text() == "Still not confirmed by PayPal"
        assert state(dom, "result-uncertain") is not None
    assert paypal.posts == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_f2_recover_dispute_iterates_the_snapshot_the_route_read_not_a_newer_listing(monkeypatch):
    """An attempt that appears between the route's read and recover_dispute's read must not shift the pairing: the route
    hands recover_dispute a store proxy whose list_by_dispute returns the route's own snapshot. Without the proxy this
    arrangement gives a length mismatch and, conservatively, "Could not check" even though THIS attempt's GET answered."""
    paypal = O.ScriptedGets(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _two_attempts(paypal, c)
    real = routes_send.recover_dispute

    def racing(probe, store, dispute_id, db_path, **kw):
        late = SQLiteAttemptStore(db_path)                   # a third attempt shows up after the route's snapshot
        late.claim(DID, "late-arrival")
        late.write(DID, "late-arrival", {"state": "sent_unconfirmed", "request_id": "req-late", "ts": "2000-01-01T00:00:00-0600",
                                         "request_input": {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                                                           "evidence_info": {"tracking_info": [{"tracking_number": "L"}]}, "notes": "l"}]}})
        late.close()
        return real(probe, store, dispute_id, db_path, **kw)

    monkeypatch.setattr(routes_send, "recover_dispute", racing)
    paypal.script = ["fail", "ok"]
    dom = parse(O._check(c, pkg_id).text)
    assert dom.find("h1").text() == "Still not confirmed by PayPal"                  # THIS attempt's GET answered
    assert paypal.calls_log.count("recovery_get") == 2                              # exactly the snapshot's two attempts


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_f2_a_length_mismatch_means_could_not_check_not_a_guess(monkeypatch):
    paypal = O.ScriptedGets(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _two_attempts(paypal, c)

    def extra_get(probe, store, dispute_id, db_path, **kw):
        for _ in range(3):                                   # 3 GETs where the snapshot has 2 qualifying attempts
            probe.call("GET", "x", "recovery_get")
        return []

    monkeypatch.setattr(routes_send, "recover_dispute", extra_get)
    r = O._check(c, pkg_id)
    dom = parse(r.text)
    assert r.status_code == 502 and dom.find("h1").text() == "Could not check"
    assert state(dom, "paypal-unreachable") is not None and paypal.posts == 1


def _earlier_version_setup(c, with_request):
    """Version 1 of THIS package sent (registered in the audit) and left unconfirmed; the package is then edited."""
    pkg_id = O._approved_manual_package(c)
    assert I._send(c, pkg_id).status_code == 202
    if not with_request:
        # the earlier attempt has no stored request any more: no GET can be made for it
        raw = sqlite3.connect(config.db_path())
        raw.execute("UPDATE send_attempts SET request_input_json = NULL WHERE dispute_id = ?", (DID,))
        raw.commit()
        raw.close()
    claim_id = repo.list_claims(config.db_path(), pkg_id)[0]["id"]
    assert I._post(c, f"/disputes/{DID}/packages/{pkg_id}/claims/{claim_id}",
                   {"text": "Shipped via FedEx on 2026-09-11, tracking DEMO-FDX-000001."}).status_code == 303
    assert I._approve(c, pkg_id).status_code == 303
    return pkg_id


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_f3_the_earlier_version_message_says_paypal_has_not_shown_it_only_when_a_get_answered():
    paypal = I.PayPalDouble(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _earlier_version_setup(c, with_request=True)
    dom = parse(O._check(c, pkg_id).text)
    box = state(dom, "earlier-version-pending")
    assert box is not None and "has not shown it yet" in box.text() and "has not been confirmed yet" not in box.text()
    assert "recovery_get" in paypal.calls_log                                        # the GET really ran


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_f3_without_a_get_for_the_earlier_attempt_the_message_does_not_claim_paypal_looked():
    paypal = I.PayPalDouble(lose_response=True, hide_stored=True)
    c = I._client(paypal)
    pkg_id = _earlier_version_setup(c, with_request=False)
    gets_before = paypal.calls_log.count("recovery_get")
    dom = parse(O._check(c, pkg_id).text)
    box = state(dom, "earlier-version-pending")
    assert box is not None and "has not been confirmed yet" in box.text() and "has not shown it yet" not in box.text()
    assert paypal.calls_log.count("recovery_get") == gets_before                    # no GET could be made for it


# ==== the mcp approval state uses the same attachment list as approval =============================
def test_f4_the_mcp_approval_state_uses_the_same_attachment_list_as_approval_and_send():
    from copilot import mcp_server
    db = config.db_path()
    doc = repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha-label", "Tracking no.: DEMO-FDX-000001", True)
    repo.set_manual_fields(db, DID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
                                     "carrier": {"value": "FedEx", "confidence": "read"}})
    pkg = repo.insert_package(db, DID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(db, pkg, [{"evidence_index": 0, "claim_index": 0, "text": "Shipped via FedEx on 2026-09-11.",
                                  "source_ids": [], "basis": "seller_statement"}])
    repo.attach_document(db, pkg, doc)
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import pick_tracking
    tracking, carrier = pick_tracking(extracted_docs_for(db, DID))                   # exactly what approval/send would use
    req = build_request_input(repo.list_claims(db, pkg), tracking, carrier)
    repo.insert_approval(db, pkg, approval_sha256=approval_fingerprint_with_attachments(DID, "provide_evidence", req, ["sha-label"]),
                         approved_by="vendor")
    package = repo.get_package(db, pkg)
    assert mcp_server._approval_state(db, DID, package)[0] == "approved"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM documents WHERE id = ?", (doc,))                       # the attached document's row vanishes
    raw.commit()
    raw.close()
    assert repo.list_attachments(db, pkg) == [] and len(repo.list_attachment_sha256s(db, pkg)) == 1
    assert mcp_server._approval_state(db, DID, package)[0] == "approved_but_edited"   # as the page and the send say


# ==== a delivery claim citing a document that conflicts with paypal is flagged =====================
@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.local_spec
def test_f5_a_delivery_claim_citing_a_document_that_conflicts_with_paypal_is_flagged_at_generation(monkeypatch, tmp_path):
    """The conflict half of the generation-time flag (document 02 belongs to another order: tracking and postal code
    conflict with PayPal's own data)."""
    paypal = I.PayPalDouble()
    c = I._client(paypal)
    delivery_date=I._demo_delivery_date("02_wrong_order_tracking_and_postal_code.pdf")
    claim=f"Delivered on {delivery_date}."
    pkg_id = _generate_by_hand(c, monkeypatch, tmp_path, "02_wrong_order_tracking_and_postal_code.pdf",
                               [(claim, "seller_statement")], "Estafeta capture attached.",
                               tracking="DEMO-EST-999999", carrier="MEX_ESTAFETA")
    assert f'Unsupported or conflicting delivery assertion: "{claim}"' in _flags(pkg_id)
    assert I._approve(c, pkg_id, "Reviewed.").status_code == 400
    assert repo.get_latest_valid_approval(config.db_path(), pkg_id) is None and paypal.posts == 0


@pytest.mark.local_spec
def test_f5_create_package_survives_a_country_without_rules_and_still_flags_what_it_can(monkeypatch, tmp_path):
    def no_rules(*args, **kwargs):
        raise UnknownCountryError("No Seller Protection rule set for country 'ZZ'. Supported: MX, US.")

    monkeypatch.setattr(routes_package, "_checks_for", no_rules)
    paypal = I.PayPalDouble()
    c = I._client(paypal)
    pkg_id = _generate_by_hand(c, monkeypatch, tmp_path, "01_coherent_delivery.pdf",
                               [("Shipped via FedEx on 2026-09-11.", "seller_statement")], "FedEx proof attached.")
    assert repo.get_package(config.db_path(), pkg_id)["source"] == "ai"             # the package was still created
    assert not [f for f in _flags(pkg_id) if "delivery assertion" in f.lower()]
    # and a delivery claim with no source at all is still flagged without the checklist
    pkg2 = _generate_by_hand_again(c, monkeypatch, tmp_path)
    assert any("Unsupported or conflicting delivery assertion" in f for f in _flags(pkg2))


def _generate_by_hand_again(c, monkeypatch, tmp_path):
    I._install_model(monkeypatch, tmp_path, lambda docs: I._output(
        docs, [("Delivered and signed for by the buyer.", "seller_statement")], "FedEx proof attached."))
    return I._pkg_id(I._post(c, f"/disputes/{DID}/packages"))


# ==== the setup page depends on the empty environment not on the clients ===========================
@pytest.mark.parametrize("error_text", ["PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET missing in .env", "a completely different message"])
def test_f6_the_setup_page_depends_on_the_empty_environment_not_on_the_clients_error_text(monkeypatch, error_text):
    from copilot.web import routes_disputes

    class Raises:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(error_text)

    monkeypatch.setattr(routes_disputes, "SandboxClient", Raises)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "")
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    c = authenticated_client(I.create_app(), client=("127.0.0.1", 12345))
    r = c.get("/disputes")
    box = state(parse(r.text), "hosted-no-credentials" if config.hosted_demo() else "credentials-missing")
    assert r.status_code == 503 and box is not None and "banner-error" in box.attrs["class"]
    if config.hosted_demo():
        assert "come back later" in box.text() and ".env" not in box.text()
    else:
        assert "README" in box.text() and "Configure" in box.text() and "docs/" not in box.text()
        assert ".env.example" in box.text()
    assert "Traceback" not in r.text and error_text not in r.text


def test_f6_with_credentials_present_a_runtime_error_is_not_disguised_as_missing_credentials(monkeypatch):
    from copilot.web import routes_disputes

    class Raises:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET missing in .env")     # the old text, but...

    monkeypatch.setattr(routes_disputes, "SandboxClient", Raises)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "set-by-this-test")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "set-by-this-test")                          # ...both values ARE set
    c = authenticated_client(I.create_app(), client=("127.0.0.1", 12345), raise_server_exceptions=False)
    r = c.get("/disputes")
    assert r.status_code == 500 and state(parse(r.text), "credentials-missing") is None


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


@pytest.mark.usefixtures("legacy_approval_scenario")
@pytest.mark.hosted_spec
@pytest.mark.parametrize("document", ["04_injected_instruction.pdf", "02_wrong_order_tracking_and_postal_code.pdf"])
def test_hosted_generation_uses_manual_facts_and_still_blocks_untrusted_delivery(document, monkeypatch):
    calls = []
    monkeypatch.setattr(I.evidence_model.ai, "preflight", lambda: calls.append("preflight"))
    monkeypatch.setattr(I.evidence_model.ai, "run_with_docs", lambda *a, **kw: calls.append("model"))
    fake = I.PayPalDouble()
    c = I._client(fake)
    doc_id = I._upload(c, document)
    pkg_id = I._pkg_id(I._post(c, f"/disputes/{DID}/packages"))
    pkg = repo.get_package(config.db_path(), pkg_id)
    assert pkg["source"] == "human_edit" and auto_prep.HOSTED_AI_OFF_FLAG in pkg["review_flags"]
    assert repo.list_claims(config.db_path(), pkg_id) == [] and calls == []
    repo.insert_claims(config.db_path(), pkg_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "The package was delivered.", "source_ids": [f"doc_{doc_id}"], "basis": "seller_statement"}])
    refused = I._post(c, f"/disputes/{DID}/packages/{pkg_id}/approve", {"sufficiency_override": "Reviewed myself"})
    assert refused.status_code == 400 and repo.get_latest_valid_approval(config.db_path(), pkg_id) is None
    assert fake.posts == 0 and calls == []
