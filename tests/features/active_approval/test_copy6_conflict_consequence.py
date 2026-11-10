"""v1.1.2 copy 6: a blocking conflict says its consequence first."""
import sqlite3

from copilot.core.checklist import Check
from copilot.core.context import TransactionContext
from copilot.features.active_approval import service

DID = "PP-R-DEMO-0000001"


def test_a_blocking_conflict_states_the_consequence_first(prepared_hosted_access, monkeypatch):
    web, fake, db, package, _ = prepared_hosted_access
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE claims SET source_ids_json='[]' WHERE package_id=?", (package,))
    check = Check("destination", "Delivery place", "conflict", "published_policy", "Synthetic policy", [], "review_difference",
                  False, "Your receipt says 03100. PayPal says 06600.")
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([check], TransactionContext()))
    text = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    head = text.index("Cannot approve yet")
    line = text.index("This answer cannot be sent while the receipt does not match PayPal")
    assert head < line < text.index("A check conflicts with the source data")


def test_another_blocking_conflict_gets_the_general_line_not_the_receipt_one(prepared_hosted_access, monkeypatch):
    web, fake, db, package, _ = prepared_hosted_access
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE claims SET source_ids_json='[]' WHERE package_id=?", (package,))
    checks = [Check("destination", "Delivery place", "verified", "published_policy", "Synthetic policy", [], "none", False, "Matches."),
              Check("delivery_status", "Delivered", "conflict", "published_policy", "Synthetic policy", [], "review_difference", False,
                    "One source says delivered and another says it is not.")]
    monkeypatch.setattr(service, "_checks_for", lambda *a: (checks, TransactionContext()))
    text = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    assert "This answer cannot be sent while a check does not match PayPal" in text
    assert "while the receipt does not match" not in text


def test_the_conflict_block_shows_with_no_statement_in_the_package(prepared_hosted_access, monkeypatch):
    """Found by table 2: the model's draft left no statement and the approval page showed no block for the address conflict."""
    web, fake, db, package, _ = prepared_hosted_access
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM claims WHERE package_id=?", (package,))
    check = Check("destination", "Delivery place", "conflict", "published_policy", "Synthetic policy", [], "review_difference",
                  False, "Your receipt says 03100. PayPal says 06600.")
    monkeypatch.setattr(service, "_checks_for", lambda *a: ([check], TransactionContext()))
    text = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    assert "Cannot approve yet" in text
    assert "This answer cannot be sent while the receipt does not match PayPal" in text
    assert "A check conflicts with the source data" in text
