"""Synthetic values: provenance cannot be inferred from a fluent sentence."""
import pytest

from copilot import config
from copilot.core.checklist import Check
from copilot.db import repo
from copilot.features.claims.model import Source, digest, safe_text, validate
from copilot.features.claims.service import compose


def source(**kw):
    values = dict(artifact_id="doc_1", field="tracking_number", sha256=digest("DEMO artifact"),
                  value="DEMO-TRACK-001", confidence="read", check_basis="published_policy", check_state="verified")
    return Source(**(values | kw))


@pytest.mark.parametrize("basis,expected", [("verified", "verified"), ("seller_statement", "seller_statement"),
                                           ("buyer_statement", "unverifiable"), ("model_interpretation", "unverifiable")])
def test_stored_basis_is_not_check_basis(basis, expected):
    claim = validate(1, "DEMO-TRACK-001", basis, (source(),))[0]
    assert claim.claim_basis == basis and claim.verdict == expected
    assert claim.bases == ["published_policy"]


@pytest.mark.parametrize("changes,expected", [({"check_state": "conflict"}, "conflict"),
    ({"confidence": "illegible"}, "unverifiable"), ({"confidence": "untrusted"}, "unverifiable"),
    ({"sha256": None}, "unverifiable"), ({"check_state": "missing"}, "unverifiable"),
    ({"check_state": "present_unverifiable"}, "unverifiable"),
    ({"kind": "seller_statement"}, "unverifiable"), ({"value": "DEMO-OTHER"}, "unverifiable")])
def test_source_states_are_conservative(changes, expected):
    assert validate(1, "DEMO-TRACK-001", "verified", (source(**changes),))[0].verdict == expected


def test_composite_prose_is_not_verified_by_a_literal_date_or_id():
    units = validate(1, "DEMO-TRACK-001. Delivered without proof.", "verified", (source(),))
    assert len(units) == 2 and units[1].verdict == "unverifiable"
    assert units[0].text == "DEMO-TRACK-001."


def test_unknown_source_and_empty_claim():
    assert validate(1, "Synthetic statement", "verified", ())[0].verdict == "unverifiable"
    assert validate(1, "", "seller_statement", ()) == []
    with pytest.raises(ValueError):
        source(sha256="invented")
    with pytest.raises(ValueError):
        validate(True, "text", "verified", ())


def test_artifact_hash_field_exact_value_and_exclusion_are_composed():
    db = config.db_path()
    did = "PP-R-DEMO-0000001"
    text = "Tracking number: DEMO-TRACK-001\nCarrier: FedEx"
    doc = repo.insert_document(db, did, "demo.txt", "application/pdf", len(text), digest(text), text, True)
    package = repo.insert_package(db, did, "human_edit", True, [])
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "DEMO-TRACK-001",
                                    "source_ids": [f"doc_{doc}"], "basis": "verified"}])
    check = Check("tracking_number", "Tracking", "verified", "published_policy", "synthetic rule",
                  [f"document:{doc}"], "none", False, "Synthetic match")
    claim = compose(db, did, package, [check], {})[0]
    assert claim.verdict == "verified"
    assert claim.sources[0].sha256 == digest(text)
    assert claim.sources[0].field == "tracking_number" and claim.sources[0].value == "DEMO-TRACK-001"
    repo.set_document_excluded(db, doc, True)
    assert compose(db, did, package, [check], {})[0].verdict == "unverifiable"
    with pytest.raises(ValueError):
        compose(db, "PP-R-DEMO-0000002", package, [], {})


def test_private_text_is_not_rendered():
    assert "@" not in safe_text("buyer@example.com")
    assert "Bearer" not in safe_text("Bearer DEMO credential")
    assert "PayPal-" not in safe_text("PayPal-Request-Id: DEMO")
