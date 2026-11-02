from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from copilot.features.evidence_dossier import service


def _case(*item_types, channel="INTERNAL", evidence_item_type="SERVICE"):
    return {
        "dispute_channel": channel,
        "disputed_transactions": [{"items": [{"item_type": value} for value in item_types]}],
        "evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "item_type": evidence_item_type}],
    }


def _project(raw, tmp_path, reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED"):
    return service.assemble_case(raw, {"reason": reason}, [], [], [], {}, tmp_path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_case("SERVICE"), "service"),
        (_case("DIGITAL_DOWNLOAD"), "digital"),
        (_case("PRODUCT"), "unknown"),
        (_case("BOOKING"), "unknown"),
        (_case("SERVICE", "SERVICE"), "service"),
        (_case("SERVICE", "DIGITAL_DOWNLOAD"), "unknown"),
        (_case("PRODUCT", evidence_item_type="SERVICE"), "unknown"),  # requested evidence.item_type is not transaction item_type
        ({"disputed_transactions": []}, "unknown"),
        ({"disputed_transactions": [{"items": []}]}, "unknown"),
        ({"disputed_transactions": [{"items": [{"item_type": ""}]}]}, "unknown"),
        ({"disputed_transactions": [{"items": [{"item_type": "FUTURE_TYPE"}]}]}, "unknown"),
        ({"disputed_transactions": [{"items": [{"item_type": None}]}]}, "unknown"),
        ({"disputed_transactions": [{"items": [None]}]}, "unknown"),
        ({"disputed_transactions": [{"items": [{"item_type": "SERVICE"}]}, {"items": [{"item_type": "SERVICE"}]}]}, "service"),
        ({"disputed_transactions": [{"items": [{"item_type": "SERVICE"}]}, {"items": [{"item_type": "BOOKING"}]}]}, "unknown"),
        ({"disputed_transactions": [{"items": [{"item_type": "SERVICE"}]}, {}]}, "unknown"),
        ({"disputed_transactions": [None]}, "unknown"),
        ({"disputed_transactions": "malformed"}, "unknown"),
    ],
)
def test_case_kind_requires_complete_homogeneous_transaction_item_types(raw, expected, tmp_path):
    before = copy.deepcopy(raw)
    result = _project(raw, tmp_path)
    assert result["kind"] == expected
    assert result["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert raw == before
    if expected == "unknown":
        assert result["proof_matrix"]["items"] == []


def test_fixture_product_and_physical_channel_do_not_claim_physical(tmp_path):
    fixture_path = Path(__file__).resolve().parents[3] / "pilot" / "fixtures" / "synthetic" / \
        "dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))["body"]
    raw["dispute_channel"] = "physical"
    result = _project(raw, tmp_path)
    assert result["kind"] == "unknown"
    assert result["reason_label"] == service.REASONS["MERCHANDISE_OR_SERVICE_NOT_RECEIVED"]
    assert result["item_type_profile"]["registered_types"] == ["PRODUCT"]
    assert result["item_type_profile"]["unmapped_registered_types"] == ["PRODUCT"]
    assert "Recorded transaction item type: Product." in result["kind_explanation"]
    assert "The recorded Product type does not distinguish a physical product from a digital one" in result["kind_explanation"]
    assert "cannot select type-specific evidence suggestions" in result["kind_explanation"]
    assert result["proof_matrix"]["basis"].startswith("No type-specific suggestions selected")
    assert "not PayPal requirements" in result["proof_matrix"]["basis"]
    assert "Product" not in result["proof_matrix"]["basis"]
    assert "organizer-kind" not in result["kind_explanation"]


def test_valid_reason_missing_type_and_unrecognized_reason_are_separate(tmp_path):
    missing = _project({"disputed_transactions": [{"items": [{}]}]}, tmp_path)
    assert missing["reason_explanation"] == "Case reason: Item or service not received."
    assert "Some transaction items have no recorded type" in missing["kind_explanation"]
    assert "No type-specific suggestions selected" in missing["proof_matrix"]["basis"]
    assert "case reason is not recognized" not in missing["proof_matrix"]["basis"]

    unknown_reason = _project(_case("PRODUCT"), tmp_path, reason="UNRECOGNIZED_REASON")
    assert unknown_reason["reason"] is None
    assert unknown_reason["reason_explanation"] == "Case reason is not recognized in this record."
    assert "Recorded transaction item type: Product." in unknown_reason["kind_explanation"]
    assert "case reason is not recognized" in unknown_reason["proof_matrix"]["basis"]
    assert "Recorded transaction item type: Product." in unknown_reason["kind_explanation"]
    assert "No type-specific suggestions selected" not in unknown_reason["proof_matrix"]["basis"]


def test_mixed_and_booking_keep_registered_values_and_exact_causes(tmp_path):
    mixed = _project(_case("PRODUCT", "SERVICE"), tmp_path)
    assert mixed["kind"] == "unknown"
    assert mixed["item_type_profile"]["registered_types"] == ["PRODUCT", "SERVICE"]
    assert "mixed" in mixed["item_type_profile"]["causes"]
    assert "no_supported_mapping" in mixed["item_type_profile"]["causes"]
    assert "This case contains different transaction item types" in mixed["kind_explanation"]
    assert "Product, Service" in mixed["kind_explanation"]
    assert "does not distinguish a physical product from a digital one" in mixed["kind_explanation"]

    booking = _project(_case("BOOKING"), tmp_path)
    assert booking["item_type_profile"]["registered_types"] == ["BOOKING"]
    assert booking["item_type_profile"]["unmapped_registered_types"] == ["BOOKING"]
    assert "Recorded transaction item type: Booking." in booking["kind_explanation"]
    assert "no Booking-specific suggestions configured" in booking["kind_explanation"]

    service_case = _project(_case("SERVICE"), tmp_path)
    digital_case = _project(_case("DIGITAL_DOWNLOAD"), tmp_path)
    assert "Recorded transaction item type: Service." in service_case["kind_explanation"]
    assert "Recorded transaction item type: Digital download." in digital_case["kind_explanation"]
    assert "Type-specific suggestions use" not in service_case["kind_explanation"]
    assert service_case["proof_matrix"]["items"]
    assert all("not a PayPal requirement" in row["basis"] for row in service_case["proof_matrix"]["items"])


def test_unrecognized_and_malformed_item_type_causes_are_explicit(tmp_path):
    unrecognized = _project(_case("FUTURE_TYPE"), tmp_path)
    assert unrecognized["item_type_profile"]["unrecognized_values"] == ["FUTURE_TYPE"]
    assert "unrecognized" in unrecognized["item_type_profile"]["causes"]
    assert "unrecognized item type value: FUTURE_TYPE." in unrecognized["kind_explanation"]
    assert "No type-specific suggestions selected" in unrecognized["proof_matrix"]["basis"]

    malformed = _project(_case(42), tmp_path)
    assert malformed["item_type_profile"]["malformed_count"] == 1
    assert "malformed" in malformed["item_type_profile"]["causes"]
    assert "records cannot be read, so Proofie cannot determine their type" in malformed["kind_explanation"]
    assert "No type-specific suggestions selected" in malformed["proof_matrix"]["basis"]


def test_standalone_explicit_kind_contract_and_all_reason_guides_remain():
    raw = {"reason": "UNAUTHORISED", "kind": "physical"}
    report = service.assemble(raw)
    assert report["kind"] == "physical"
    assert report["kind_explanation"] == "Selected type for this dossier: physical."
    assert "type supplied for this dossier" in report["proof_matrix"]["basis"]
    assert "not PayPal requirements" in report["proof_matrix"]["basis"]
    missing_type = service.assemble({"reason": "UNAUTHORISED"})
    assert "No type was selected for this dossier" in missing_type["kind_explanation"]
    assert "No type-specific suggestions selected for this dossier" in missing_type["proof_matrix"]["basis"]
    assert len(service.PROOF_GUIDE) == 3
    assert all(set(kinds) == service.KINDS for kinds in service.PROOF_GUIDE.values())
