"""S03d (RECORD walks 12-15): a carrier-free document that carries the same tracking number as the shipment form must not
wipe the form's carrier. pick_tracking fills a missing carrier only from a source with the SAME tracking number; with
different numbers there is no mixed pair and the carrier check blocks. The approval step restates the send body."""
import sqlite3

import pytest
from carrier_required.fixture_gate import DID, MESSAGE, FakeClient, choices, digest, prepared, web, rows  # noqa: F401

from copilot.core.checklist_input import extracted_docs_for
from copilot.core.send import build_request_input, pick_tracking
from copilot.core.send_service import content_sha
from copilot.db import repo
from copilot.features.active_approval import service

NUMBER = "DEMO-RECORD-0001"
FORM = {"tracking_number": {"value": NUMBER, "confidence": "read"}, "carrier": {"value": "FedEx", "confidence": "read"}}


def _carrier_free_document(db, text_number):
    text = f"DEMO RECEIPT\nPostal code: 06600\nTracking number: {text_number}\nStatus: En tránsito"
    return repo.insert_document(db, DID, "receipt.txt", "text/plain", len(text), digest(text), text, True)


def _only_that_document(db, doc_text_number):
    """The fixture's own document is replaced by the carrier-free one (the walk had a single uploaded receipt)."""
    with sqlite3.connect(db) as c:
        c.execute("UPDATE documents SET excluded=1 WHERE dispute_id=?", (DID,))
    return _carrier_free_document(db, doc_text_number)


def test_walk15_order_form_with_carrier_then_carrier_free_document_same_number(prepared, monkeypatch):
    db, pid, _doc, fake = prepared
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    repo.set_manual_fields(db, DID, FORM)
    _only_that_document(db, NUMBER)
    docs = extracted_docs_for(db, DID)
    assert pick_tracking(docs) == (NUMBER, "FedEx")                       # the document's number, the form's carrier
    review = service.read(fake, db, DID, pid)
    assert MESSAGE not in review.blocked                                    # "Pick a carrier" no longer blocks
    body = build_request_input(repo.list_claims(db, pid), *pick_tracking(docs))
    assert body["evidences"][0]["evidence_info"]["tracking_info"][0] == {"carrier_name": "FEDEX", "tracking_number": NUMBER}
    assert review.request_input == body                                     # A4-5: what the approval restates is the send body
    assert content_sha(db, DID, pid) == service.approval_fingerprint_with_attachments.__globals__["sha256_json"](body)


def test_different_tracking_numbers_never_make_a_mixed_pair(prepared, monkeypatch):
    db, pid, _doc, fake = prepared
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    repo.set_manual_fields(db, DID, FORM)
    _only_that_document(db, "DEMO-OTHER-0002")
    assert pick_tracking(extracted_docs_for(db, DID)) == ("DEMO-OTHER-0002", None)      # the form's carrier belongs to another number
    review = service.read(fake, db, DID, pid)
    assert MESSAGE in review.blocked                                         # blocked in the open, not silently paired
    body = review.request_input
    assert body is None or "FEDEX" not in repr(body)


def test_the_form_number_alone_keeps_its_own_carrier(prepared, monkeypatch):
    db, pid, _doc, fake = prepared
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    repo.set_manual_fields(db, DID, FORM)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE documents SET excluded=1 WHERE dispute_id=?", (DID,))
    assert pick_tracking(extracted_docs_for(db, DID)) == (NUMBER, "FedEx")
    assert MESSAGE not in service.read(fake, db, DID, pid).blocked
