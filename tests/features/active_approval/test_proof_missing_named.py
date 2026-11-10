"""v1.1.2 P route 2: required proof that is Missing is named at the head of the approval page and beside the approve control.
Display only: approval is allowed exactly as before, with no new input."""
import sqlite3

from copilot.core.checklist import Check
from copilot.core.context import TransactionContext
from copilot.features.active_approval import routes, service

DID = "PP-R-DEMO-0000001"


def _c(cid, state, basis="published_policy"):
    return Check(cid, "Label", state, basis, "Synthetic policy", [], "add_document", False, "Synthetic.")


def test_words():
    assert routes.proof_missing_words([_c("delivery_status", "missing"), _c("delivery_date", "missing")]) == ["delivered status with a date"]
    assert routes.proof_missing_words([_c("delivery_date", "missing"), _c("signature", "missing")]) == [
        "delivery date", "signed receipt (the total is above the limit)"]
    assert routes.proof_missing_words([_c("delivery_status", "verified"), _c("signature", "not_applicable"),
                                       _c("receiver_name", "missing", "app_quality_rule")]) == []


def test_named_at_the_head_and_at_the_control_and_approval_still_works_with_no_new_input(prepared_hosted_access, monkeypatch):
    web, fake, db, package, _ = prepared_hosted_access
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE claims SET source_ids_json='[]' WHERE package_id=?", (package,))
    checks = [_c("delivery_status", "missing"), _c("delivery_date", "missing")]
    monkeypatch.setattr(service, "_checks_for", lambda *a: (checks, TransactionContext()))
    text = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    assert "PayPal can reject this answer" in text and "no delivered status with a date." in text
    control = text[text.index('data-state="proof-missing-approve"'):]
    assert "You are approving sending without: delivered status with a date." in control and "Approve this package" in control
    review = service.read(fake, db, DID, package)
    choices = {c.key: "seller_statement" if c.verdict == "unverifiable" else "confirmed" for c in review.claims}
    assert service.approve(fake, db, DID, package, review.review_sha256, choices, "", "")       # exactly today's call


def test_nothing_is_shown_when_no_required_proof_is_missing(prepared_hosted_access, monkeypatch):
    web, fake, db, package, _ = prepared_hosted_access
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([_c("delivery_status", "verified")], TransactionContext()))
    text = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    assert "PayPal can reject this answer" not in text and "proof-missing-approve" not in text
