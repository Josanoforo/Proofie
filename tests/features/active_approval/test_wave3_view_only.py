import hashlib
import sqlite3

from copilot.db import repo
from copilot.features.active_approval import service
DID = "PP-R-DEMO-0000001"

def _choices(review):
    return {c.key: "seller_statement" if c.verdict == "unverifiable" else "confirmed" for c in review.claims}


def test_conflict_view_shows_both_document_values_without_changing_digest(prepared_hosted_access):
    web, fake, db, package, first_doc = prepared_hosted_access
    text = "Carrier: UPS\nTracking number: DEMO-UPS-000002\nStatus: Delivered"
    second_doc = repo.insert_document(db, DID, "synthetic-second.pdf", "application/pdf", len(text),
                                      hashlib.sha256(text.encode()).hexdigest(), text, True)
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 1,
                                    "text": "The tracking values are DEMO-FDX-001 and DEMO-UPS-000002.",
                                    "source_ids": [f"doc_{first_doc}", f"doc_{second_doc}"],
                                    "basis": "seller_statement"}])
    review = service.read(fake, db, DID, package)
    baseline_sha = review.review_sha256
    assert any(check.id == "tracking_number" and check.state == "conflict" for check in review.checks)
    expected_path = f"/disputes/{DID}/packages/{package}/active-approval"
    page = web.get(expected_path)
    assert page.url.path == expected_path
    assert page.status_code == 200
    assert 'class="claim-conflict-values"' in page.text
    assert "demo.pdf" in page.text and "synthetic-second.pdf" in page.text
    assert "DEMO-FDX-001" in page.text and "DEMO-UPS-000002" in page.text
    assert review.review_sha256 == baseline_sha
    assert all(method == "GET" for method, _, _ in fake.calls)


def test_cleared_approval_view_names_changed_statement_and_file_hash(prepared_hosted_access, monkeypatch):
    web, fake, db, package, doc_id = prepared_hosted_access
    original = service.read(fake, db, DID, package)
    service.approve(fake, db, DID, package, original.review_sha256, _choices(original), "", "")
    changed_text = "Tracking number: DEMO-FDX-001\nCarrier: FedEx\nStatus: Delivered"
    changed_sha = hashlib.sha256(changed_text.encode()).hexdigest()
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE documents SET extracted_text=?, sha256=? WHERE id=?",
                     (changed_text, changed_sha, doc_id))
        conn.commit()
    current = service.read(fake, db, DID, package)
    assert current.cleared and current.approval is None
    assert current.review_sha256 != original.review_sha256
    assert current.view_changes
    changed = current.view_changes[0]
    assert changed["statement"] == "Shipment is in transit."
    assert any(f["filename"] == "demo.pdf" and f["old_sha256"] != f["current_sha256"]
               for f in changed["files"])
    stale_digest = current.review_sha256
    monkeypatch.setattr(service, "read", lambda *args: current)
    expected_path = f"/disputes/{DID}/packages/{package}/active-approval"
    page = web.get(expected_path)
    assert page.url.path == expected_path
    assert page.status_code == 200
    assert "What changed since you approved" in page.text
    assert "Shipment is in transit." in page.text and "demo.pdf" in page.text
    assert "stored hash differs" in page.text
    assert current.review_sha256 == stale_digest
    assert repo.get_latest_valid_approval(db, package) is None
    assert all(method == "GET" for method, _, _ in fake.calls)
