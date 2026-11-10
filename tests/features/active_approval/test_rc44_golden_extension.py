import hashlib
import json
import pytest
from copilot.features.active_approval import service
from copilot.db import repo
DID = "PP-R-DEMO-0000001"

def test_rc43_wider_fixture_review_and_prompt_pin_w3_off(prepared, monkeypatch):
    """Pin the existing review and serialized prompt over the versioned fixture corpus."""
    import hashlib
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from copilot.core.document_ingest import extract_text
    from copilot.core.field_extraction import extract_fields
    from copilot.core.checklist import evaluate
    from copilot.core.checklist_input import cached_rules, docs_for_dispute
    from copilot.features.claims.model import digest
    from copilot.fixtures.m1 import loader
    from step4_evidence_ai import build_messages

    _, fake, db, package, _ = prepared
    root = Path(__file__).resolve().parents[3]
    fixed_paths = (
        "copilot/fixtures/m1/doc_aggregator_receipt.txt",
        "copilot/fixtures/m1/doc_estafeta_capture.txt",
        "copilot/fixtures/m1/doc_fedex_letter.txt",
        "pilot/fixtures/buyer_chat.txt",
        "pilot/fixtures/order_confirmation.txt",
        "pilot/fixtures/shipping_tracking.txt",
        "pilot/fixtures/variants/contradictory/shipping_tracking.txt",
        "pilot/fixtures/variants/no_tracking/buyer_chat.txt",
        "demo/documents/01_coherent_delivery.pdf",
        "demo/documents/02_wrong_order_tracking_and_postal_code.pdf",
        "demo/documents/03_in_transit.pdf",
        "demo/documents/04_injected_instruction.pdf",
        "demo/documents/05_illegible_scan.png",
    )
    fixed_now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)
    monkeypatch.setattr(service, "datetime", FixedDateTime)
    # evaluate() reads "today" from the clock when no date is passed (service.read passes none); freeze that clock too,
    # otherwise the pinned review hash changes with the calendar day
    import copilot.core.checklist as checklist_module
    monkeypatch.setattr(checklist_module, "datetime", FixedDateTime)

    evaluated = []
    for name in loader.case_names():
        case = loader.load_case(name)
        checks = evaluate(loader.build_context(case), loader.build_docs(case), cached_rules(), loader.today_of(case))
        evaluated.append({"case": name, "today": case["today"],
                          "checks": [[c.id, c.state, c.basis, c.basis_ref, c.seller_message, c.action] for c in checks]})

    for relative in fixed_paths:
        path = root / relative
        payload = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix == ".txt":
            # The pin below was recorded over a Windows checkout (CRLF text fixtures; the blobs are LF). Normalise to that
            # form so a Linux checkout or an extracted kit ZIP gives the same hash (PDF and PNG are -text and untouched).
            payload = payload.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            text = payload.decode("utf-8")
            content_type = "text/plain"
        elif suffix == ".pdf":
            text, ok = extract_text("application/pdf", payload)
            assert ok, relative
            content_type = "application/pdf"
        else:
            text, ok = extract_text("image/png", payload)
            assert not ok, relative
            text = None
            content_type = "image/png"
        document_id = repo.insert_document(db, DID, Path(relative).name, content_type,
            len(payload), hashlib.sha256(payload).hexdigest(), text, text is not None)
        repo.attach_document(db, package, document_id)

    review = service.read(fake, db, DID, package)
    docs, names = docs_for_dispute(db, DID)
    prompt = build_messages(fake.raw, docs, names)
    result = {"review_sha256": review.review_sha256,
              "prompt_sha256": hashlib.sha256(json.dumps(prompt, sort_keys=True).encode("utf-8")).hexdigest(),
              "m1_cases": evaluated,
              "documents": [[name, hashlib.sha256(text.encode("utf-8")).hexdigest()]
                            for name, text in sorted(docs.items())]}
    actual = hashlib.sha256(json.dumps(result, ensure_ascii=True, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    assert actual == "efbd1f8712f1f6fe020d0f8dbdbf10aca7bdb9b68b00bbd3fc01149252ec0784"
    assert all(method == "GET" for method, _, _ in fake.calls)
