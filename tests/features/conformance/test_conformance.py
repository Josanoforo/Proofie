"""Pinned contract semantics, real builders and explicit optional source tiers."""
import json
import os
from pathlib import Path

import pytest

from copilot.core.carriers import KNOWN_CODES
from copilot.features.conformance.carriers_map import ALLOWED_CODES, evidence_carrier
from copilot.features.conformance.extract import FIXTURES, clean, encoded, extract, read_pinned, subset
from copilot.features.conformance.pins import PINS
from copilot.features.conformance.schemas import Documents, issues, view
from copilot.features.conformance.suite import ROOT, mapped_domino_rows, request_rows, response_counts, run, sentence


@pytest.fixture(scope="module")
def docs():
    return Documents()


def test_wired_builders_validate_14_of_16_request_fixtures(docs):
    rows = request_rows(docs)
    assert len(rows) == 16
    assert sum(r["valid"] is True for r in rows) == 14, rows
    assert {r["name"] for r in rows if r["valid"] is False} == {"webhook-DEMO-ID-WITH-HYPHENS"}
    assert sum(r["valid"] is None for r in rows) == 1
    domino = [r for r in rows if r["name"] in ("evidence-DOMINO", "appeal-DOMINO")]
    assert len(domino) == 2 and all(r["valid"] is True and not r["issues"] for r in domino)


@pytest.mark.parametrize("code,name,expected", [("DOMINO", "Domino", ("OTHER", "Domino")), ("MEX_ESTAFETA", "Estafeta", ("MEX_ESTAFETA", None)), ("FEDEX", "FedEx", ("FEDEX", None)), ("OTHER", "DEMO Courier", ("OTHER", "DEMO Courier"))])
def test_evidence_carrier_mapping(code, name, expected):
    assert evidence_carrier(code, name) == expected


def test_exhaustive_catalogue_mapping_is_derived_from_pinned_enum():
    for code in KNOWN_CODES:
        mapped, name = evidence_carrier(code, code.title())
        assert mapped in ALLOWED_CODES
        assert (mapped == code and name is None) if code in ALLOWED_CODES else (mapped == "OTHER" and name == code.title())
    with pytest.raises(ValueError, match="display name"):
        evidence_carrier("DEMO_UNLISTED", None)


def test_both_domino_builders_validate_without_test_side_remapping(docs):
    rows = mapped_domino_rows(docs)
    assert len(rows) == 2 and all(not r["issues"] for r in rows)


def test_translations_only_readonly_writeonly_nullable():
    schema = {"type": "object", "properties": {"a": {"type": "string", "readOnly": True}, "b": {"type": "string", "writeOnly": True}, "c": {"type": "string", "nullable": True, "enum": ["DEMO"]}}, "required": ["a", "b", "c"], "additionalProperties": False}
    assert set(view(schema, "readOnly")["properties"]) == {"b", "c"}
    assert view(schema, "readOnly")["required"] == ["b", "c"]
    assert set(view(schema, "writeOnly")["properties"]) == {"a", "c"}
    assert view(schema, "writeOnly")["properties"]["c"]["enum"] == ["DEMO", None]
    assert view(schema, "readOnly")["additionalProperties"] is False
    assert "a" in schema["properties"]


def test_extractor_preserves_property_names_and_validation_values():
    schema = {"description": "documentation", "properties": {"description": {"type": "string", "description": "documentation"}}, "const": {"description": "required literal"}}
    assert clean(schema) == {"properties": {"description": {"type": "string"}}, "const": {"description": "required literal"}}


def test_placeholders_are_artefacts_but_real_bad_data_is_not():
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator({"type": "integer"})
    assert issues(validator, "[redacted]")[0]["artefact"]
    assert not issues(validator, "DEMO_BAD")[0]["artefact"]
    assert all("message" not in issue for issue in issues(validator, "DEMO_PRIVATE_BODY"))


def test_subset_provenance_is_present_for_every_document():
    for key, pin in PINS.items():
        doc = json.loads((FIXTURES / (key + ".json")).read_text(encoding="utf-8"))
        prov = doc["provenance"]
        assert prov["source_url"] == pin["url"] and prov["fetch_date"] == "2026-10-06"
        assert prov["document_version"] == pin["version"] and prov["full_document_sha256"] == pin["sha256"]
        assert set("#" + p for p in pin["roots"]) <= set(prov["json_pointers"])


def test_unread_fund_movement_tokens_report_contract_mismatch(docs, capsys):
    # Synthetic mismatch-shaped token, never attributed to an actual sandbox response.
    errors = issues(docs.validator("disputes", "res", "/components/schemas/fund_movement/properties/party"), "DEMO_UNDOCUMENTED_PARTY")
    assert errors and any(e["keyword"] == "enum" for e in errors)
    print("fund_movements token diagnostic: " + ("schema differs" if errors else "schema now accepts synthetic token"))


def test_empty_dispute_items_report_contract_mismatch(docs):
    errors = issues(docs.validator("disputes", "res", "/components/schemas/dispute_search"), {"items": [], "links": []})
    assert any(e["pointer"] == "/items" and e["keyword"] == "minItems" for e in errors)


def test_three_tracker_links_report_contract_mismatch(docs):
    tracker = {"transaction_id": "DEMO1234567890123", "status": "SHIPPED", "links": [{"href": "https://example.com/demo", "rel": "self", "method": "GET"}] * 3}
    errors = issues(docs.validator("tracking", "res", "/components/schemas/tracker"), tracker)
    assert any(e["pointer"] == "/links" and e["keyword"] == "maxItems" for e in errors)


def test_synthetic_response_tier_and_regenerated_sentence(docs, tmp_path):
    counts, responses = response_counts(docs)
    assert counts["checked"] == len(list((ROOT / "pilot/fixtures/synthetic").glob("*.json")))
    assert counts["checked"] == counts["valid"] + counts["artefact_only"] + counts["different"]
    result = run()
    output = Path(os.environ.get("PROOFIE_CONFORMANCE_OUTPUT_DIR", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    (output / "conformance-sentence.txt").write_text(result["sentence"] + "\n", encoding="utf-8")
    assert "14 of 16" in result["sentence"] and "synthetic response fixtures" in result["sentence"]
    assert "DOMINO fixtures conform through the wired" in result["sentence"]
    assert "latent carrier" not in result["sentence"] and "production wiring remains pending" not in result["sentence"]
    assert "142 recorded" not in result["sentence"]


def test_optional_full_tier_and_exact_subset_reproduction():
    directory = os.environ.get("PROOFIE_SPECS_DIR")
    if directory:
        output = extract(directory)
        result = run(directory)
    else:
        # Committed public subsets cover selected roots, not original full-file hashes.
        output = {key: encoded(subset(json.loads((FIXTURES / (key + ".json")).read_text(
            encoding="utf-8")), pin)) for key, pin in PINS.items()}
        result = run()
    assert all(data == (FIXTURES / (key + ".json")).read_bytes() for key, data in output.items())
    assert sum(r["valid"] is True for r in result["requests"]) == 14
    assert all(not r["issues"] for r in result["mapped"])


def test_pinned_hash_mismatch_is_a_failure_naming_file(tmp_path):
    (tmp_path / PINS["disputes"]["file"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch: live_customer-disputes_schema.json"):
        read_pinned(tmp_path)


def test_optional_recorded_response_tier_has_honest_scope(docs):
    directory = os.environ.get("PROOFIE_EVIDENCE_DIR")
    if directory:
        result = run(evidence_directory=directory)
        assert "recorded 2xx responses" in result["sentence"]
        assert result["response_counts"]["checked"] == len(result["responses"])
    else:
        directory = Path(__file__).parent / "fixtures" / "synthetic-responses"
        counts, responses = response_counts(docs, directory)
        assert counts == {"checked": 2, "valid": 1, "artefact_only": 0,
                          "different": 1, "non_2xx_or_unmatched": 1}
        assert len(responses) == 2
        assert responses[1]["issues"]
        scoped = sentence(request_rows(docs), counts, "committed synthetic 2xx response fixtures")
        assert "committed synthetic 2xx response fixtures" in scoped
        assert "recorded 2xx responses" not in scoped


def test_core_dispute_subset_matches_the_immutable_pinned_schema():
    import hashlib
    from copilot.core.carriers import DISPUTE_EVIDENCE_CODES, EVIDENCE_DISPLAY_NAMES
    raw = (FIXTURES / "disputes.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "614254e4df29f71cffb6f1acadd285be76d74758a3fd53428b6a131b142ba07d"
    assert PINS["disputes"]["version"] == "1.12"
    assert PINS["disputes"]["sha256"] == "fee5e04ab606f8961b95744e65c4189fc7c4613e79ba2f944d2deab79e834330"
    assert DISPUTE_EVIDENCE_CODES == KNOWN_CODES & ALLOWED_CODES
    assert set(EVIDENCE_DISPLAY_NAMES) == KNOWN_CODES - ALLOWED_CODES


@pytest.mark.parametrize("code", sorted(KNOWN_CODES))
def test_core_and_conformance_code_outcomes_agree(code):
    from copilot.core.carriers import evidence_carrier as core_evidence_carrier
    core_code, core_name = core_evidence_carrier(code, code)
    contract_code, contract_name = evidence_carrier(code, code)
    assert core_code == contract_code
    if code == "DOMINO":
        assert core_name == "Domino" and contract_name == "DOMINO"
    else:
        assert core_name == contract_name


@pytest.mark.parametrize("missing", ["auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time", "webhook_id", "webhook_event"])
def test_postman_verify_required_fields_remain_required_in_pinned_contract(docs, missing):
    # Webhooks Management /item/8 field names only; no example values copied.
    body = {"auth_algo": "SHA256withRSA", "cert_url": "https://localhost/cert",
            "transmission_id": "DEMO1234567890", "transmission_sig": "REVNTw==",
            "transmission_time": "2026-10-06T12:00:00Z", "webhook_id": "DEMO1234567890",
            "webhook_event": {"id": "DEMOevent1", "event_type": "CUSTOMER.DISPUTE.CREATED"}}
    validator = docs.validator("webhooks", "req", "/components/schemas/verify_webhook_signature")
    assert not issues(validator, body)
    del body[missing]
    errors = list(validator.iter_errors(body))
    assert any(e.validator == "required" and missing in e.validator_value for e in errors)


def test_postman_empty_appeal_example_does_not_satisfy_evidence_input(docs):
    # Disputes /item/3 {}, saved400/401 only: not a successful appeal contract.
    errors = issues(docs.evidence_validator(), {})
    assert any(e["keyword"] == "required" and e["pointer"] == "/" for e in errors)


def _rows(valid, invalid, no_schema):
    return [{"valid": True}] * valid + [{"valid": False}] * invalid + [{"valid": None}] * no_schema


_COUNTS = {"checked": 3, "valid": 2, "artefact_only": 0, "different": 1, "non_2xx_or_unmatched": 0}
_NO_SCHEMA = "has no JSON schema to check (send-message accepts a multipart document only)"
_MISMATCH = "the remaining request mismatch is a test-fixture webhook-id style"


def test_request_clause_states_all_three_groups_for_current_rows(docs):
    text = sentence(request_rows(docs), _COUNTS)
    assert ("14 of 16 request fixtures conform, 1 does not, and 1 " + _NO_SCHEMA + "; " + _MISMATCH + "; ") in text
    assert "1 do not" not in text


@pytest.mark.parametrize("valid,invalid,no_schema,clause", [
    (14, 1, 1, "14 of 16 request fixtures conform, 1 does not, and 1 " + _NO_SCHEMA),
    (12, 2, 3, "12 of 17 request fixtures conform, 2 do not, and 3 have no JSON schema to check (send-message accepts a multipart document only)"),
    (1, 1, 1, "1 of 3 request fixtures conform, 1 does not, and 1 " + _NO_SCHEMA),
    (15, 1, 0, "15 of 16 request fixtures conform and 1 does not"),
    (13, 3, 0, "13 of 16 request fixtures conform and 3 do not"),
])
def test_request_clause_number_agreement_and_omitted_groups(valid, invalid, no_schema, clause):
    text = sentence(_rows(valid, invalid, no_schema), _COUNTS)
    assert clause + "; " + _MISMATCH + "; " in text
    assert sum([valid, invalid, no_schema]) == int(clause.split(" of ")[1].split(" ")[0])


@pytest.mark.parametrize("valid,no_schema,clause", [
    (16, 0, "16 of 16 request fixtures conform"),
    (15, 1, "15 of 16 request fixtures conform and 1 " + _NO_SCHEMA),
    (14, 2, "14 of 16 request fixtures conform and 2 have no JSON schema to check (send-message accepts a multipart document only)"),
])
def test_no_mismatch_clause_when_nothing_is_invalid(valid, no_schema, clause):
    text = sentence(_rows(valid, 0, no_schema), _COUNTS)
    assert clause + "; 3 synthetic response fixtures were checked" in text
    assert "remaining request mismatch" not in text and "do not" not in text and "does not" not in text
