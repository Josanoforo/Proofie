import json
import hashlib
import html
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from jinja2 import ChoiceLoader, DictLoader

from copilot import config
from copilot.core.checklist import Check
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.context import TransactionContext
from copilot.core.send import approval_fingerprint_with_attachments, build_request_input, pick_tracking
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import rules, service, store
from copilot.web import routes_package
from _hosted import authenticated_client, set_test_csrf

DID = "PP-R-DEMO-0000001"


def test_ui_copy_b8_server_count_does_not_replace_approval_gate(prepared_web):
    web, fake, db, package, _ = prepared_web
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 1,
                                    "text": "A synthetic statement.", "source_ids": [],
                                    "basis": "seller_statement"}])
    url = f"/disputes/{DID}/packages/{package}/active-approval"
    page = web.get(url)
    review = service.read(fake, db, DID, package)
    total = len(review.claims)
    assert f"0 of {total} statements checked." in page.text
    assert f"{total} statements to confirm" in page.text
    first = next(iter(choices(review).items()))
    response = web.post(url, data={"csrf_token": web.cookies["csrf_token"],
                                   "review_sha256": review.review_sha256,
                                   "claim-" + first[0]: first[1]})
    assert response.status_code == 400
    assert f"1 of {total} statements checked." in response.text
    assert repo.get_latest_valid_approval(db, package) is None
    assert all(method == "GET" for method, _, _ in fake.calls)


def test_ui_copy_b4_filename_is_resolved_and_private_filename_blocks(prepared_web):
    web, fake, db, package, doc = prepared_web
    url = f"/disputes/{DID}/packages/{package}/active-approval"
    assert 'data-source-id="doc_' + str(doc) + '">demo.pdf' in web.get(url).text
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE documents SET filename=? WHERE id=?", ("synthetic.pdf@seller.example.com", doc))
    review = service.read(fake, db, DID, package)
    assert any("private contact" in message for message in review.blocked)
    with pytest.raises(service.ReviewError):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    page = web.get(url)
    assert "synthetic.pdf@seller.example.com" not in page.text
    assert repo.get_latest_valid_approval(db, package) is None


def test_ui_copy_b4_fallback_keeps_raw_id_without_changing_snapshot(prepared):
    from copilot.features.claims.service import document_names
    _, fake, db, package, _ = prepared
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 1,
                                    "text": "A synthetic statement.", "source_ids": ["missing-id"],
                                    "basis": "seller_statement"}])
    review = service.read(fake, db, DID, package)
    names = document_names(db, DID, review.claims)
    assert names["missing-id"] == "Document 2 of 2"
    assert any(s.artifact_id == "missing-id" for c in review.claims for s in c.sources)


@pytest.fixture
def prepared_web(web_mode, prepared):
    """Keep the original synthetic records; opt in to named authenticated web contracts."""
    web, fake, db, package, doc = prepared
    return authenticated_client(web.app), fake, db, package, doc


def choices(review):
    return {c.key: "seller_statement" if c.verdict == "unverifiable" else "confirmed" for c in review.claims}


@pytest.mark.parametrize("check_id", ["delivery_date", "dates_coherent"])
def test_dr1b_date_difference_requires_note_in_receipt_and_print(prepared, monkeypatch, check_id):
    _, fake, db, package, _ = prepared
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE claims SET source_ids_json='[]' WHERE package_id=?", (package,))
    check = Check(check_id, "Date difference", "conflict", "app_quality_rule", "Synthetic policy",
                  [], "review_difference", False, "Synthetic dates differ.")
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([check], TransactionContext()))
    review = service.read(fake, db, DID, package)
    assert not review.blocked
    with pytest.raises(service.ReviewError, match="Open notices"):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    note = "I checked the dates against the synthetic shipping record."
    approval_id = service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", note)
    receipt = json.loads(store.receipt(db, approval_id)["confirmations_json"])
    assert receipt[0]["acknowledged_differences"] == [{"check_id": check_id, "note": note}]
    from copilot.features.package_print import service as printable
    from copilot.web.routes_disputes import templates
    data = printable.read(db, __import__('copilot').config.uploads_dir(), DID, package)
    rendered = templates.env.get_template("features/package_print/print.html").render(package=data)
    assert note in rendered and "Review the difference" in rendered
    assert all(method == "GET" for method, _, _ in fake.calls)


@pytest.mark.parametrize("check_id", ["tracking_number", "destination", "delivery_status"])
def test_dr1b_other_conflicts_still_cannot_be_acknowledged(prepared, monkeypatch, check_id):
    _, fake, db, package, _ = prepared
    check = Check(check_id, "Hard difference", "conflict", "published_policy", "Synthetic policy",
                  [], "review_difference", False, "Hard source difference.")
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([check], TransactionContext()))
    review = service.read(fake, db, DID, package)
    with pytest.raises(service.ReviewError, match="conflicts"):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "I reviewed it.")


def test_one_standard_row_fingerprint_receipt_audit_and_duplicate(prepared):
    _web, fake, db, package, _doc = prepared
    review = service.read(fake, db, DID, package)
    aid = service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    assert service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "") == aid
    req = build_request_input(repo.list_claims(db, package), *pick_tracking(extracted_docs_for(db, DID)))
    assert repo.get_latest_valid_approval(db, package)["approval_sha256"] == approval_fingerprint_with_attachments(
        DID, "provide_evidence", req, [])
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    confirmation = json.loads(store.receipt(db, aid)["confirmations_json"])[0]
    assert confirmation["decision"] == "confirmed" and len(confirmation["sentence_sha256"]) == 64
    assert confirmation["sources"][0]["sha256"] is not None
    assert any(a["action"] == "approve_package" and a["detail"]["via"] == "active_approval"
               for a in repo.list_audit(db, DID))


def test_individual_decisions_required_and_unverifiable_must_be_owned(prepared):
    _, fake, db, package, _ = prepared
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 1, "text": "A synthetic unsupported fact.",
                                    "source_ids": [], "basis": "model_interpretation"}])
    review = service.read(fake, db, DID, package)
    with pytest.raises(service.ReviewError, match="every statement"):
        service.approve(fake, db, DID, package, review.review_sha256, {}, "", "")
    wrong = choices(review)
    wrong[review.claims[-1].key] = "confirmed"
    with pytest.raises(service.ReviewError, match="unverifiable"):
        service.approve(fake, db, DID, package, review.review_sha256, wrong, "", "")
    assert service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")


@pytest.mark.parametrize("currency,amount,required", [("MXN", 999999, False), ("MXN", 1000000, True),
    ("MXN", 1000001, True), ("USD", 74999, False), ("USD", 75000, True), ("USD", 75001, True),
    ("EUR", 1, True), (None, 1, True), ("USD", None, True), ("USD", True, True)])
def test_threshold_integer_boundaries(currency, amount, required):
    assert bool(rules.typed_reasons(amount, currency, [])) is required


@pytest.mark.parametrize("word", rules.FRAUD_WORDS)
def test_bilingual_caution_words(word):
    assert rules.typed_reasons(1, "USD", ["Synthetic " + word.upper()])
    assert not rules.typed_reasons(1, "USD", ["prefix" + word + "suffix"])


def test_wrong_phrase_refused_and_reason_constants_rendered(prepared_web):
    web, fake, db, package, _ = prepared_web
    fake.raw["dispute_amount"] = {"currency_code": "USD", "value": "750.00"}
    review = service.read(fake, db, DID, package)
    with pytest.raises(service.ReviewError, match="exactly"):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "yes", "")
    page = web.get(f"/disputes/{DID}/packages/{package}/active-approval")
    assert page.status_code == 200
    assert "typed-required" in page.text and "750.00 USD" in page.text and "10000.00 MXN" in page.text
    assert rules.PHRASE in page.text and rules.SOURCE in html.unescape(page.text)
    service.approve(fake, db, DID, package, review.review_sha256, choices(review), rules.PHRASE, "")


def test_four_core_refusals(prepared, monkeypatch):
    _, fake, db, package, _ = prepared
    from copilot.core.checklist import UnknownCountryError
    monkeypatch.setattr(service, "_checks_for", lambda *a: (_ for _ in ()).throw(UnknownCountryError()))
    with pytest.raises(service.ReviewError, match="no Seller Protection"):
        service.read(fake, db, DID, package)
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([], TransactionContext()))
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 1, "text": "Delivered yesterday.",
                                    "source_ids": [], "basis": "seller_statement"}])
    review = service.read(fake, db, DID, package)
    with pytest.raises(service.ReviewError, match="trusted matching source"):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    row = repo.list_claims(db, package)[-1]
    repo.mark_claim_edited(db, row["id"], "A synthetic seller statement.")
    check = Check("tracking_number", "Tracking", "conflict", "published_policy", "synthetic", [], "review_difference", False, "Mismatch")
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([check], TransactionContext()))
    review = service.read(fake, db, DID, package)
    with pytest.raises(service.ReviewError, match="conflicts"):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([], TransactionContext()))
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE packages SET review_flags_json=? WHERE id=?", (json.dumps(["Synthetic notice"]), package))
    review = service.read(fake, db, DID, package)
    with pytest.raises(service.ReviewError, match="Open notices"):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    assert service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "I reviewed the notice.")


def test_stale_text_attachment_source_and_core_edit(prepared_web):
    web, fake, db, package, doc = prepared_web
    old = service.read(fake, db, DID, package)
    repo.attach_document(db, package, doc)
    with pytest.raises(service.ReviewError, match="changed"):
        service.approve(fake, db, DID, package, old.review_sha256, choices(old), "", "")
    current = service.read(fake, db, DID, package)
    aid = service.approve(fake, db, DID, package, current.review_sha256, choices(current), "", "")
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?", ("\nSynthetic extra field", doc))
    assert service.read(fake, db, DID, package).cleared
    assert repo.get_latest_valid_approval(db, package) is None
    current = service.read(fake, db, DID, package)
    service.approve(fake, db, DID, package, current.review_sha256, choices(current), "", "")
    token = "DEMO-CSRF"
    set_test_csrf(web, token)
    claim = repo.list_claims(db, package)[0]
    response = web.post(f"/disputes/{DID}/packages/{package}/claims/{claim['id']}",
                        data={"csrf_token": token, "text": "Edited by the seller."}, follow_redirects=False)
    assert response.status_code == 303 and repo.get_latest_valid_approval(db, package) is None
    assert store.receipt(db, aid) is not None  # historical metadata retained


def test_route_csrf_dom_passive_guard_and_ownership(prepared_web):
    web, fake, db, package, _ = prepared_web
    url = f"/disputes/{DID}/packages/{package}/active-approval"
    page = web.get(url)
    assert page.status_code == 200
    for name in ("approval", "approval-header", "send-summary", "approval-counters", "counter-confirmed",
                 "counter-unverifiable", "counter-conflicts", "claim-list", "claim-card", "claim-sentence",
                 "claim-basis", "claim-source-doc", "claim-source-field", "claim-source-value", "claim-source-hash",
                 "claim-verdict", "claim-confirm", "typed-confirm", "typed-confirm-reason", "typed-confirm-phrase", "approval-control"):
        assert (name not in page.text) if name.startswith("typed-confirm") else (name in page.text)
    assert "approve all" not in page.text.casefold() and "style=" not in page.text
    review_sha = re.search(r'name="review_sha256" value="([a-f0-9]+)"', page.text)[1]
    review = service.read(fake, db, DID, package)
    data = {"csrf_token": web.cookies["csrf_token"], "review_sha256": review_sha,
            **{"claim-" + key: value for key, value in choices(review).items()}}
    assert web.post(url, data=data | {"csrf_token": "bad"}).status_code == 403
    assert web.post(url, data=data, follow_redirects=False).status_code == 303
    assert "approval-receipt" in web.get(url).text
    assert web.post(f"/disputes/{DID}/packages/{package}/approve", data=data).status_code == 409
    assert web.get(url.replace(DID, "PP-R-DEMO-0000002")).status_code == 400


def test_receipt_failure_invalidates_standard_row(prepared, monkeypatch):
    _, fake, db, package, _ = prepared
    review = service.read(fake, db, DID, package)
    monkeypatch.setattr(store, "save", lambda *a, **kw: (_ for _ in ()).throw(OSError("Synthetic failure")))
    with pytest.raises(OSError):
        service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    assert repo.get_latest_valid_approval(db, package) is None


def test_threaded_duplicate_gets_same_approval(prepared):
    _, fake, db, package, _ = prepared
    review = service.read(fake, db, DID, package)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", ""), range(2)))
    assert ids[0] == ids[1]


def test_fake_send_accepts_active_row_like_core_row(prepared):
    _, fake, db, package, _ = prepared
    review = service.read(fake, db, DID, package)
    service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    # No provide-evidence link: both approvals reach the same core precondition refusal before any POST.
    fake.raw["links"] = []
    active_result = send_package(fake, db, DID, package)
    repo.insert_approval(db, package, review.fingerprint, "vendor")
    core_result = send_package(fake, db, DID, package)
    assert (active_result.http_status, active_result.title, active_result.sent) == (
        core_result.http_status, core_result.title, core_result.sent)
    assert active_result.title != "Approval out of date" and not active_result.sent


def test_successful_fake_send_is_identical_for_active_and_core_approval(prepared, tmp_path, monkeypatch):
    import copy
    from copilot.core import send_recovery
    _, fake, db, package, _ = prepared
    fake.raw["evidences"] = [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}]
    review = service.read(fake, db, DID, package)
    service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    core_db = tmp_path / "core.db"
    with sqlite3.connect(str(db)) as original, sqlite3.connect(str(core_db)) as duplicate:
        original.backup(duplicate)
    repo.insert_approval(core_db, package, review.fingerprint, "vendor")
    # The legacy core-only approval remains usable only with active approval disabled.
    monkeypatch.setattr(send_recovery.time, "sleep", lambda _: None)

    class FakeSend:
        def __init__(self):
            self.calls = []
            self.body = copy.deepcopy(fake.raw)

        def call(self, method, path, op, **kw):
            self.calls.append((method, op))
            if method == "POST":
                assert op == "provide_evidence"
                self.body["evidences"] = [{**review.request_input["evidences"][0], "source": "SUBMITTED_BY_SELLER"}]
                return {}
            assert method == "GET"
            return copy.deepcopy(self.body)

    first, second = FakeSend(), FakeSend()
    # Resolve transaction/tracking context identically to the original approval review.
    first_call = first.call
    def active_call(method, path, op, **kw):
        if "transactions" in path or "trackers" in path:
            return fake.call(method, path, op, **kw)
        return first_call(method, path, op, **kw)
    first.call = active_call
    active_result = send_package(first, db, DID, package)
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "false")
    core_result = send_package(second, core_db, DID, package)
    assert active_result.sent and core_result.sent
    assert active_result == core_result
    # v1.1.2 O: the active review reads the sale itself (capture) when the report has no row; a read-only extra, like show_dispute
    assert [c for c in first.calls if c[1] not in ("show_dispute", "context_capture", "context_order")] == second.calls
    assert len([m for m, _ in first.calls if m == "POST"]) == 1


@pytest.mark.local_spec
def test_flag_off_core_template_bytes_identical_and_on_only_active_entry(prepared_web, monkeypatch):
    from pathlib import Path
    web, _, _, package, _ = prepared_web
    monkeypatch.delenv("COPILOT_FEATURE_ACTIVE_APPROVAL")
    current = web.get(f"/disputes/{DID}/packages/{package}")
    historic=(Path(__file__).parent/"fixtures/package_review-baseline-0.html").read_text(encoding="utf-8")
    assert hashlib.sha256(historic.encode()).hexdigest()=="09614b908184f1ad09310ff2cce18e0baca45bce7c41df735bd9fd43cad8695b"
    original=(Path(__file__).parent/"fixtures/package_review-historic-1.html").read_text(encoding="utf-8")
    assert hashlib.sha256(original.encode()).hexdigest()=="d4b2570088ab73c193b8eb90bd6e7afaa0b0385672b5e184e5f9c9ccb156f991"
    package_flow=(Path(__file__).parent/"fixtures/package_review-historic-2.html").read_text(encoding="utf-8")
    assert hashlib.sha256(package_flow.encode()).hexdigest()=="b11cf2c942c2bc6ff80d2e33baff55a7703f6d4e9ed19f393beeee9df420ae40"
    # Re-baseline, frozen from the merged template (new fixture and hash);
    # the historic fixtures and their hashes stay asserted above.
    frozen = (Path(__file__).parent/"fixtures/package_review-frozen-3.html").read_text(encoding="utf-8")
    assert hashlib.sha256(frozen.encode()).hexdigest()=="83e85d5c4c0ad8f2a7b208884b08315cc4144d01e075edcb52c796562d9ba620"
    # The template differs from that frozen fixture by an explicit, counted list of approved edits
    # (tests/local/_template_delta.py); applying exactly those edits must give the template in the tree, byte for byte.
    import _template_delta as delta
    assert len(delta.edits()) == 108 and delta.apply(frozen) == delta.current_template()
    assert any("The assistant's answer could not be checked" in e["new"] for e in delta.edits())
    env = routes_package.templates.env.overlay(loader=ChoiceLoader([
        DictLoader({"package_review.html": delta.apply(frozen)}), routes_package.templates.env.loader]))
    assert env.get_template("package_review.html").render(current.context) == current.text
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    rendered = web.get(f"/disputes/{DID}/packages/{package}").text
    # O4 relabelled the package page's step "Open approval review" (the approval page keeps its own title)
    assert 'Open approval review' in rendered and f'href="/disputes/{DID}/packages/{package}/active-approval"' in rendered
    assert f'action="/disputes/{DID}/packages/{package}/approve"' not in rendered


# rc4.3: the three include hooks (edit-pk1..3) other lanes plug into. While their flags are off and no read-back exists,
# the page is byte-identical to the 53-edit template that was in the tree before them.
@pytest.mark.local_spec
def test_rc43_hooks_off_page_identical_to_the_46_edit_template(prepared_web, monkeypatch):
    from pathlib import Path
    import _template_delta as delta
    web, _, _, package, _ = prepared_web
    for name in ("CLAUSES", "BILINGUAL"):
        monkeypatch.delenv(f"COPILOT_FEATURE_{name}", raising=False)
    current = web.get(f"/disputes/{DID}/packages/{package}")
    assert current.status_code == 200 and current.context["bilingual"] is None
    hooks = {e["id"].split("@")[0]: e["new"] for e in delta.edits() if e["id"].startswith("edit-pk")}
    assert sorted(hooks) == ["edit-pk1", "edit-pk2", "edit-pk3"]
    assert '{% include "_paypal_record.html" ignore missing %}' in hooks["edit-pk1"]
    assert '{% include "features/bilingual/_statements.html" ignore missing %}' in hooks["edit-pk2"]
    assert '{% include "features/clauses/_clause.html" ignore missing %}' in hooks["edit-pk3"]
    lines = (Path(__file__).parent/"fixtures/package_review-frozen-3.html").read_text(encoding="utf-8").split("\n")
    earlier = [e for e in delta.edits() if not e["id"].startswith("edit-pk")]
    assert len(earlier) == 105     # the v1.1.2 verdict line plus 79 earlier edits plus the 19 NIVEL2-FIX entries of the O4 B composition plus the 2 seller-shortcuts proposals plus the LINK-FIX F-04 anchors plus the 3 HERO send-panel entries
    for edit in reversed(earlier):
        old = edit["old"].split("\n") if edit["old"] != "" else []
        lines[edit["at"]:edit["at"] + len(old)] = edit["new"].split("\n") if edit["new"] != "" else []
    env = routes_package.templates.env.overlay(loader=ChoiceLoader([
        DictLoader({"package_review.html": "\n".join(lines)}), routes_package.templates.env.loader]))
    assert env.get_template("package_review.html").render(current.context) == current.text


# rc4.3 W1-1a, package half: what PayPal's record listed when the send was confirmed (lane RX's SendRecord.readback),
# shown under "What PayPal confirmed" through hook edit-pk1; absent for a record without a read-back.
_READBACK = {"evidence_type": "PROOF_OF_FULFILLMENT", "documents_present": ["label.pdf", "receipt.pdf"],
             "paypal_date": "2026-10-05T10:00:00Z", "read_at": "2026-10-05T10:01:00-06:00"}


def test_rc43_paypal_record_partial_lists_what_paypal_showed():
    from types import SimpleNamespace
    env = routes_package.templates.env
    text = html.unescape(env.get_template("_paypal_record.html").render(
        record=SimpleNamespace(readback=_READBACK), readback_label="Proof of fulfillment"))
    assert 'data-state="paypal-record"' in text and "What PayPal's record lists" in text
    assert "Evidence type: Proof of fulfillment." in text and "PayPal dates it" in text
    assert "<li>label.pdf</li><li>receipt.pdf</li>" in text and "Proofie read this from PayPal at" in text
    empty = html.unescape(env.get_template("_paypal_record.html").render(
        record=SimpleNamespace(readback={**_READBACK, "documents_present": [], "paypal_date": None}),
        readback_label="Proof of fulfillment"))
    assert "PayPal lists no document with it." in empty and "PayPal dates it" not in empty


@pytest.mark.local_spec
def test_rc43_package_page_shows_paypal_record_only_when_the_send_record_has_a_readback(prepared_web, monkeypatch):
    import dataclasses
    from copilot.core.disputes import humanize_evidence_type
    web, _, _, package, _ = prepared_web
    before = web.get(f"/disputes/{DID}/packages/{package}")
    assert 'data-state="paypal-record"' not in before.text and before.context["readback_label"] is None
    real = routes_package.status.send_record
    monkeypatch.setattr(routes_package.status, "send_record",
                        lambda *a, **k: dataclasses.replace(real(*a, **k), readback=_READBACK))
    page = web.get(f"/disputes/{DID}/packages/{package}")
    assert page.status_code == 200
    assert page.context["readback_label"] == humanize_evidence_type("PROOF_OF_FULFILLMENT")
    section = page.text[page.text.index("<h3>What PayPal confirmed</h3>"):]
    section = section[:section.index("</section>")]
    assert section.count('data-state="paypal-record"') == 1 and "<li>receipt.pdf</li>" in section


@pytest.mark.hosted_spec
def test_hosted_flag_off_keeps_shared_warning_and_on_only_active_entry(prepared_web, monkeypatch):
    web, _, _, package, _ = prepared_web
    assert config.hosted_demo()
    monkeypatch.delenv("COPILOT_FEATURE_ACTIVE_APPROVAL")
    current = web.get(f"/disputes/{DID}/packages/{package}")
    assert current.status_code == 200
    assert 'data-state="hosted-shared-send"' in current.text
    assert 'Review each statement and approve' not in current.text
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    response = web.get(f"/disputes/{DID}/packages/{package}")
    assert response.status_code == 200
    assert 'data-state="hosted-shared-send"' in response.text
    assert 'Open approval review' in response.text and f'href="/disputes/{DID}/packages/{package}/active-approval"' in response.text
    assert f'action="/disputes/{DID}/packages/{package}/approve"' not in response.text


@pytest.mark.parametrize("required",[False,True])
def test_dr1b_partial_save_respects_carrier_feature_flag(web_mode,monkeypatch,required):
    monkeypatch.setenv("COPILOT_FEATURE_CARRIER_REQUIRED","true" if required else "false")
    import test_routes_package as package_tests
    c,db,token=package_tests._client(monkeypatch=monkeypatch)
    dispute_id=package_tests.DID
    pid=repo.insert_package(db,dispute_id,"human_edit",False,[])
    response=c.post(f"/disputes/{dispute_id}/manual-fields",data={"csrf_token":token,"package_id":pid,
        "tracking_number":"DEMO-PARTIAL-1"},follow_redirects=False)
    assert response.status_code == 303
    assert repo.get_manual_fields(db,dispute_id) == {"tracking_number":{"value":"DEMO-PARTIAL-1","confidence":"read"}}
    # Mandatory-to-send is not mandatory-to-save. Approval still enforces the original flag.
    from copilot.features.carrier_required.service import MESSAGE
    review=service.read(package_tests.FakeClient(),db,dispute_id,pid)
    assert (MESSAGE in review.blocked) is required
    assert repo.get_latest_valid_approval(db,pid) is None


# ---- the approval counters count what is true
from _dom import parse  # noqa: E402


def _counters(page):
    dom = parse(page)
    return {name: dom.find("span", id=name).text() for name in ("counter-confirmed", "counter-unverifiable", "counter-conflicts")}


def test_statements_without_a_document_are_counted_as_having_no_document(prepared_web):
    web, fake, db, package, _doc = prepared_web
    repo.insert_claims(db, package, [
        {"evidence_index": 0, "claim_index": 10, "text": "We packed it carefully.", "source_ids": [], "basis": "seller_statement"},
        {"evidence_index": 0, "claim_index": 11, "text": "We shipped it on time.", "source_ids": [], "basis": "seller_statement"}])
    review = service.read(fake, db, DID, package)
    own = sum(c.verdict == "seller_statement" for c in review.claims)
    assert own >= 2
    page = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    counters = _counters(page)
    assert f"{own} have no document, so Proofie cannot verify them." in counters["counter-unverifiable"]
    assert "0 cannot be verified" not in page and "0 do not match" not in page
    assert counters["counter-conflicts"] == ""                       # no conflicts, so nothing about conflicts is said
    assert re.fullmatch(r"\d+ of \d+ statements checked\.", counters["counter-confirmed"])


def test_rc43_golden_review_and_prompt_hash_w3_off(prepared):
    """Keep the frozen rc4.3 review and serialized prompt stable with only its recorded flag."""
    import hashlib
    import json
    from copilot.core.checklist_input import docs_for_dispute
    from step4_evidence_ai import build_messages

    _, fake, db, package, _ = prepared
    review = service.read(fake, db, DID, package)
    docs, names = docs_for_dispute(db, DID)
    messages = build_messages(fake.raw, docs, names)
    prompt_sha = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
    assert review.review_sha256 == "4c22504ba4c6568ab423016f4b44bfd0a20ed9a6797c889e6d5a4e7572234f88"
    assert prompt_sha == "975bea58fadf42940d8a973e95f495b5c5653077c45c4abee31d281a4ab1acf1"

    assert all(method == "GET" for method, _, _ in fake.calls)

