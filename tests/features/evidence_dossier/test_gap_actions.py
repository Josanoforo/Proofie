import hashlib
import pytest
from copilot.features.evidence_dossier import service

@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("original", ["valid", "missing", "mismatch"])
def test_original_state_matches_attachment_and_bytes(tmp_path, attached, original):
    raw = b"synthetic original"
    digest = hashlib.sha256(raw).hexdigest()
    if original != "missing":
        (tmp_path / digest).write_bytes(raw if original == "valid" else b"altered")
    doc = {"id": 1, "filename": "proof.pdf", "sha256": digest, "extraction_ok": True, "extracted_text": "record"}
    attachments = [{"document_id": 1, "sha256": digest}] if attached else []
    result = service.assemble_case({"disputed_transactions": [{"items": [{"item_type": "SERVICE"}]}]},
        {"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}, [], [doc], attachments, {}, tmp_path)
    annex = result["annexes"][0]
    assert annex["attached"] is attached
    assert all(row["status"].startswith("association not determined") for row in result["proof_matrix"]["items"])
    if original == "valid":
        assert annex["sha256"] == digest
        assert annex["status"] == ("attached; original bytes and package digest match" if attached else "source document not attached to this package")
    else:
        assert annex["sha256"] is None
        assert annex["status"].startswith("attached;" if attached else "not attached;")
        assert ("unavailable" if original == "missing" else "does not match") in annex["status"]


def test_duplicate_claim_text_retains_own_basis(tmp_path):
    claims = [{"text": "Same statement", "source_ids": [], "basis": basis} for basis in ("seller_statement", "buyer_statement")]
    result = service.assemble_case({}, {}, claims, [], [], {}, tmp_path)
    assert [row["basis"] for row in result["claims"]] == ["seller_statement", "buyer_statement"]
