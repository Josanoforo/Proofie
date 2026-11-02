"""Offline tests for copilot/core/send.py (approval_fingerprint extended to cover
attachments)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.checklist import ExtractedDoc
from copilot.core.send import (EVIDENCE_FILE_FIELD, NOT_ASKING_MESSAGE, approval_fingerprint_with_attachments,
                               build_multipart, build_request_input, pick_tracking, validate_send_link)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilot"))
from step5_send_evidence import approval_fingerprint as pilot_approval_fingerprint

DID = "PP-R-TEST-0001"
ACTION = "provide_evidence"
REQ_INPUT = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                                "tracking_number": "DEMO8800123456"}]},
                           "notes": "demo"}]}


def test_with_no_attachments_matches_pilots_original_fingerprint_exactly():
    """The attachment extension must be backward-compatible: a package with zero attachments (everything the pilot
    ever sent) produces the SAME fingerprint pilot's own approval_fingerprint would, so nothing about
    existing (non-attachment) sends changes."""
    extended = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, attachment_sha256s=[])
    original = pilot_approval_fingerprint(DID, ACTION, REQ_INPUT)
    assert extended == original


def test_adding_an_attachment_changes_the_fingerprint():
    base = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, [])
    with_one = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa"])
    assert base != with_one


def test_different_attachment_content_changes_the_fingerprint():
    a = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa"])
    b = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-bbb"])
    assert a != b


def test_removing_an_attachment_changes_the_fingerprint():
    with_two = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa", "sha-bbb"])
    with_one = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa"])
    assert with_two != with_one


def test_attachment_order_never_matters():
    a = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa", "sha-bbb", "sha-ccc"])
    b = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-ccc", "sha-aaa", "sha-bbb"])
    assert a == b


def test_dispute_id_action_and_content_still_matter_exactly_as_before(tmp_path):
    base = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa"])
    assert base != approval_fingerprint_with_attachments("PP-R-OTHER", ACTION, REQ_INPUT, ["sha-aaa"])
    assert base != approval_fingerprint_with_attachments(DID, "accept_claim", REQ_INPUT, ["sha-aaa"])
    tampered = {**REQ_INPUT, "evidences": [{**REQ_INPUT["evidences"][0], "notes": "tampered"}]}
    assert base != approval_fingerprint_with_attachments(DID, ACTION, tampered, ["sha-aaa"])


def test_duplicate_attachment_sha_is_stable_not_an_error():
    # two different documents that happen to have identical content (same sha256) is a legitimate case
    # (e.g. the vendor uploaded the same file twice); the fingerprint must still be well-defined.
    a = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa", "sha-aaa"])
    b = approval_fingerprint_with_attachments(DID, ACTION, REQ_INPUT, ["sha-aaa", "sha-aaa"])
    assert a == b


# ---- pick_tracking: a document's reading wins over a manual-typed one -----------------------
def _doc(doc_id, tracking_number=None, carrier=None):
    fields = {}
    if tracking_number:
        fields["tracking_number"] = {"value": tracking_number, "confidence": "read"}
    if carrier:
        fields["carrier"] = {"value": carrier, "confidence": "read"}
    return ExtractedDoc(doc_id, fields, True, "x")


def test_pick_tracking_prefers_a_document_reading_over_manual():
    docs = [ExtractedDoc("manual", {"tracking_number": {"value": "MANUAL1", "confidence": "read"}}, False),
           _doc("3", "DOC1", "Estafeta")]
    assert pick_tracking(docs) == ("DOC1", "Estafeta")


def test_pick_tracking_falls_back_to_manual_when_no_document_has_one():
    docs = [ExtractedDoc("manual", {"tracking_number": {"value": "MANUAL1", "confidence": "read"},
                                    "carrier": {"value": "FedEx", "confidence": "read"}}, False)]
    assert pick_tracking(docs) == ("MANUAL1", "FedEx")


def test_pick_tracking_returns_none_none_when_nothing_available():
    assert pick_tracking([]) == (None, None)
    assert pick_tracking([_doc("3")]) == (None, None)


# ---- build_request_input ------------------------------------------------------------------------------
def test_build_request_input_refuses_with_no_tracking_number():
    with pytest.raises(ValueError):
        build_request_input([], None, None)


def test_build_request_input_maps_a_known_carrier_and_joins_claim_texts():
    claims = [{"text": "Shipped via Estafeta."}, {"text": "Tracking DEMO1."}]
    req = build_request_input(claims, "DEMO1", "Estafeta")
    ev = req["evidences"][0]
    assert ev["evidence_type"] == "PROOF_OF_FULFILLMENT"
    assert ev["evidence_info"]["tracking_info"][0] == {"carrier_name": "MEX_ESTAFETA", "tracking_number": "DEMO1"}
    assert ev["notes"].startswith("DEMO DATA")
    assert "Shipped via Estafeta." in ev["notes"] and "Tracking DEMO1." in ev["notes"]


def test_build_request_input_sets_carrier_name_other_for_an_unknown_carrier():
    req = build_request_input([], "DEMO1", "Totally Unknown Carrier Co")
    tracking = req["evidences"][0]["evidence_info"]["tracking_info"][0]
    assert tracking["carrier_name"] == "OTHER"
    assert tracking["carrier_name_other"] == "Totally Unknown Carrier Co"


def test_build_request_input_truncates_notes_at_2000_chars():
    claims = [{"text": "x" * 3000}]
    req = build_request_input(claims, "DEMO1", "Estafeta")
    assert len(req["evidences"][0]["notes"]) == 2000


# Recorded prechange multipart bodies; only Domino's dispute carrier changes.
@pytest.mark.parametrize("carrier,expected", [('MEX_ESTAFETA',
  {'evidences': [{'evidence_type': 'PROOF_OF_FULFILLMENT',
                  'evidence_info': {'tracking_info': [{'carrier_name': 'MEX_ESTAFETA',
                                                       'tracking_number': 'TN1'}]},
                  'notes': 'DEMO DATA (sandbox test with synthetic documents). Synthetic '
                           'fulfilment note.'}]}),
 ('FEDEX',
  {'evidences': [{'evidence_type': 'PROOF_OF_FULFILLMENT',
                  'evidence_info': {'tracking_info': [{'carrier_name': 'FEDEX',
                                                       'tracking_number': 'TN1'}]},
                  'notes': 'DEMO DATA (sandbox test with synthetic documents). Synthetic '
                           'fulfilment note.'}]}),
 ('Custom Carrier',
  {'evidences': [{'evidence_type': 'PROOF_OF_FULFILLMENT',
                  'evidence_info': {'tracking_info': [{'carrier_name': 'OTHER',
                                                       'tracking_number': 'TN1',
                                                       'carrier_name_other': 'Custom Carrier'}]},
                  'notes': 'DEMO DATA (sandbox test with synthetic documents). Synthetic '
                           'fulfilment note.'}]}),
 (None,
  {'evidences': [{'evidence_type': 'PROOF_OF_FULFILLMENT',
                  'evidence_info': {'tracking_info': [{'carrier_name': 'OTHER',
                                                       'tracking_number': 'TN1'}]},
                  'notes': 'DEMO DATA (sandbox test with synthetic documents). Synthetic '
                           'fulfilment note.'}]}),
 ('DOMINO',
  {'evidences': [{'evidence_type': 'PROOF_OF_FULFILLMENT',
                  'evidence_info': {'tracking_info': [{'carrier_name': 'OTHER',
                                                       'tracking_number': 'TN1',
                                                       'carrier_name_other': 'Domino'}]},
                  'notes': 'DEMO DATA (sandbox test with synthetic documents). Synthetic '
                           'fulfilment note.'}]})])
def test_dispute_carrier_body_matches_recorded_golden(carrier, expected):
    import json
    actual = build_request_input([{"text": " Synthetic fulfilment note. "}], "TN1", carrier)
    assert json.dumps(actual, ensure_ascii=False).encode("utf-8") == json.dumps(expected, ensure_ascii=False).encode("utf-8")
    assert actual["evidences"][0]["notes"] == expected["evidences"][0]["notes"]
    assert actual["evidences"][0]["evidence_type"] == expected["evidences"][0]["evidence_type"]


@pytest.mark.parametrize("carrier", ["DOMINO", "Domino", "domino"])
def test_domino_spelling_uses_the_normalized_dispute_display_name(carrier):
    assert build_request_input([{"text": "DEMO shipped"}], "TN1", carrier)["evidences"][0]["evidence_info"]["tracking_info"][0] == {
        "carrier_name": "OTHER", "tracking_number": "TN1", "carrier_name_other": "Domino",
    }


@pytest.mark.parametrize("code", [
    "CORREOS_DE_MEXICO", "DHL", "FEDEX", "GRUPO", "HOUNDEXPRESS",
    "MEX_ESTAFETA", "MEX_REDPACK", "MEX_SENDA", "PAQUETEXPRESS", "UPS",
])
def test_dispute_enum_catalogue_codes_pass_through(code):
    from copilot.core.carriers import evidence_carrier
    assert evidence_carrier(code, "ignored") == (code, None)


@pytest.mark.parametrize("code,name,expected", [
    ("OTHER", "x", ("OTHER", "x")),
    ("OTHER", "", ("OTHER", None)),
    ("OTHER", "  x  ", ("OTHER", "  x  ")),
    ("OTHER", "   ", ("OTHER", None)),
    (None, None, ("OTHER", None)),
    ("", "x", ("OTHER", None)),
    ("DOMINO", None, ("OTHER", "Domino")),
    ("DOMINO", "ignored", ("OTHER", "Domino")),
    ("DEMO_UNLISTED", "  Demo Courier  ", ("OTHER", "Demo Courier")),
])
def test_dispute_carrier_names_and_absent_codes_keep_their_contract(code, name, expected):
    from copilot.core.carriers import evidence_carrier
    assert evidence_carrier(code, name) == expected


def test_a_direct_unlisted_code_without_a_name_is_refused():
    from copilot.core.carriers import evidence_carrier
    with pytest.raises(ValueError, match="display name"):
        evidence_carrier("DEMO_UNLISTED", None)


def test_every_catalogue_code_has_an_explicit_dispute_status():
    from copilot.core.carriers import DISPUTE_EVIDENCE_CODES, EVIDENCE_DISPLAY_NAMES, KNOWN_CODES
    assert DISPUTE_EVIDENCE_CODES | set(EVIDENCE_DISPLAY_NAMES) == KNOWN_CODES
    assert not DISPUTE_EVIDENCE_CODES & set(EVIDENCE_DISPLAY_NAMES)


# ---- validate_send_link (gated on a FRESH GET's real link, nothing else) ----------------------
def test_validate_send_link_blocks_with_no_link_at_all():
    assert validate_send_link({"action_links": {}}) == NOT_ASKING_MESSAGE
    assert validate_send_link({}) == NOT_ASKING_MESSAGE


def test_validate_send_link_accepts_a_real_sandbox_link():
    norm = {"action_links": {"provide-evidence": {
        "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/provide-evidence",
        "method": "POST"}}}
    assert validate_send_link(norm) is None


def test_validate_send_link_rejects_a_non_sandbox_host():
    norm = {"action_links": {"provide-evidence": {
        "href": "https://api-m.paypal.com/v1/customer/disputes/PP-D-1/provide-evidence", "method": "POST"}}}
    assert validate_send_link(norm) is not None


def test_validate_send_link_rejects_an_unexpected_path():
    norm = {"action_links": {"provide-evidence": {
        "href": "https://api-m.sandbox.paypal.com/v1/payments/payouts", "method": "POST"}}}
    assert validate_send_link(norm) is not None


def test_validate_send_link_rejects_a_non_post_method():
    norm = {"action_links": {"provide-evidence": {
        "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/provide-evidence",
        "method": "GET"}}}
    assert validate_send_link(norm) is not None


# ---- build_multipart (attachments) -----------------------------------------------------------
import httpx  # noqa: E402 - only used by these tests, to prove the wire format against the real library


def test_build_multipart_with_no_attachments_is_just_the_input_part():
    parts = build_multipart({"evidences": []}, [])
    assert parts == [("input", (None, '{"evidences": []}', "application/json"))]


def test_build_multipart_repeats_the_evidence_file_field_once_per_attachment():
    parts = build_multipart({"evidences": []}, [
        {"filename": "a.pdf", "content_type": "application/pdf", "data": b"AAA"},
        {"filename": "b.pdf", "content_type": "application/pdf", "data": b"BBB"}])
    file_parts = [p for p in parts if p[0] == EVIDENCE_FILE_FIELD]
    assert len(file_parts) == 2
    assert file_parts[0][1] == ("a.pdf", b"AAA", "application/pdf")
    assert file_parts[1][1] == ("b.pdf", b"BBB", "application/pdf")


def test_build_multipart_produces_a_real_multipart_body_httpx_can_encode():
    """Proves the list-of-tuples shape actually round-trips through httpx's own request builder (a dict
    could not represent two parts under the same field name at all)."""
    parts = build_multipart({"evidences": []}, [
        {"filename": "a.pdf", "content_type": "application/pdf", "data": b"AAA"},
        {"filename": "b.pdf", "content_type": "application/pdf", "data": b"BBB"}])
    req = httpx.Request("POST", "https://example.com/x", files=parts)
    body = req.read().decode("latin-1")
    assert body.count(f'name="{EVIDENCE_FILE_FIELD}"') == 2
    assert 'filename="a.pdf"' in body and 'filename="b.pdf"' in body
    assert "AAA" in body and "BBB" in body
    assert 'name="input"' in body


# ---- verify_after_with_attachments: a dropped/misnamed file must not look like a full success -------------
from copilot.core.send import PARTIAL_MESSAGE, verify_after_with_attachments  # noqa: E402

ATT_REQ_INPUT = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                               "evidence_info": {"tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                                    "tracking_number": "DEMO1"}]},
                               "notes": "demo"}]}


def _evidence_body(tracking="DEMO1", documents=None):
    e = {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
        "evidence_info": {"tracking_info": [{"tracking_number": tracking}]}, "notes": "demo",
        "date": "2026-10-05"}
    if documents is not None:
        e["documents"] = documents
    return {"evidences": [e]}


def test_no_attachments_means_files_ok_is_vacuously_true():
    result = verify_after_with_attachments(_evidence_body(), ATT_REQ_INPUT, [])
    assert result["content_ok"] is True and result["files_ok"] is True and result["missing_files"] == []


def test_content_not_confirmed_means_all_attachments_count_as_missing():
    result = verify_after_with_attachments({"evidences": []}, ATT_REQ_INPUT, ["a.pdf"])
    assert result["content_ok"] is False and result["files_ok"] is False
    assert result["missing_files"] == ["a.pdf"]


def test_content_confirmed_and_the_file_is_listed_by_name_is_fully_ok():
    body = _evidence_body(documents=[{"name": "a.pdf", "url": "https://example.com/x"}])
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, ["a.pdf"])
    assert result["content_ok"] is True and result["files_ok"] is True
    assert result["missing_files"] == []


def test_content_confirmed_but_the_file_is_not_listed_is_the_partial_case():
    """The exact scenario a wrong multipart field name would produce: PayPal processed the JSON (tracking
    landed) but the file never arrived as a document on that evidence entry."""
    body = _evidence_body(documents=[])   # no documents at all on the matching evidence entry
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, ["a.pdf"])
    assert result["content_ok"] is True and result["files_ok"] is False
    assert result["missing_files"] == ["a.pdf"]


def test_a_differently_named_document_does_not_count_as_a_match():
    body = _evidence_body(documents=[{"name": "wrong_name.pdf", "url": "https://example.com/x"}])
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, ["a.pdf"])
    assert result["files_ok"] is False and result["missing_files"] == ["a.pdf"]


def test_multiple_attachments_reports_only_the_ones_actually_missing():
    body = _evidence_body(documents=[{"name": "a.pdf"}])
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, ["a.pdf", "b.pdf"])
    assert result["files_ok"] is False and result["missing_files"] == ["b.pdf"]


def test_a_document_on_a_different_evidence_entry_does_not_count():
    """Documents attached to some OTHER evidence entry (e.g. the buyer's own CREATE evidence) must not be
    mistaken for this seller submission's attachment - only the matching (tracking-number) entry counts."""
    body = {"evidences": [
        {"source": "SUBMITTED_BY_BUYER", "evidence_type": "CREATE", "documents": [{"name": "a.pdf"}]},
        {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
         "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "demo"}]}
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, ["a.pdf"])
    assert result["content_ok"] is True and result["files_ok"] is False


# ---- pilot's verify_after stops at the FIRST matching evidence_type/tracking entry
# regardless of notes - a copilot-side wrapper must scan every candidate, not just the first -------------
def test_same_tracking_but_different_notes_on_the_only_entry_stays_pending():
    """The original negative case: a single entry matches evidence_type/tracking but not notes - never
    confirmed for OUR content."""
    body = _evidence_body()
    body["evidences"][0]["notes"] = "a completely different note"
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, [])
    assert result["content_ok"] is False


def test_an_earlier_entry_with_different_notes_does_not_shadow_a_later_matching_one():
    """TWO seller-submitted PROOF_OF_FULFILLMENT entries share the same evidence_type and tracking
    number (an earlier attempt, then ours) - the earlier one's notes do not match, but a LATER entry's do.
    pilot's own verify_after would return on the first (wrong) match and never even look at the second;
    this must still report content_ok=True from the one that actually matches."""
    body = {"evidences": [
        {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
         "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "an earlier attempt",
         "date": "2026-10-04"},
        {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
         "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "demo",
         "date": "2026-10-05", "documents": [{"name": "a.pdf"}]}]}
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, ["a.pdf"])
    assert result["content_ok"] is True and result["files_ok"] is True
    assert result["details"]["date"] == "2026-10-05"   # confirms it matched the SECOND entry, not the first


def test_files_ok_is_scoped_to_the_matched_entry_not_a_union_across_candidates():
    """'de forma coherente con files_ok': the earlier (non-matching-notes) entry happens to list a file
    with the SAME name our own attachment uses - that must never count. Only the entry that actually
    matched notes/tracking is checked for files_ok."""
    body = {"evidences": [
        {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
         "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "an earlier attempt",
         "date": "2026-10-04", "documents": [{"name": "a.pdf"}]},   # same filename, WRONG attempt
        {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
         "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]}, "notes": "demo",
         "date": "2026-10-05", "documents": []}]}   # the entry that actually matches has no documents at all
    result = verify_after_with_attachments(body, ATT_REQ_INPUT, ["a.pdf"])
    assert result["content_ok"] is True
    assert result["files_ok"] is False and result["missing_files"] == ["a.pdf"]
