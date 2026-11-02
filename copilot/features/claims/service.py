"""Compose core claims with existing artifacts; no generation or remote writes."""
from __future__ import annotations

from copilot.core.checklist_input import extracted_docs_for
from copilot.core.evidence_model import untrusted_document_ids
from copilot.db import repo
from copilot.features.claims.model import Source, digest, is_private, validate

FIELD_CHECK = {"postal_code": "destination", "city": "destination", "state": "destination",
               "street": "full_address", "ship_date": "dates_coherent", "delivery_type": "branch_pickup"}


def document_names(db_path, dispute_id, claims, *, redact_private=False):
    """Display names only; raw source identifiers and approval snapshots stay intact."""
    documents = {str(d["id"]): d["filename"] for d in repo.list_documents(db_path, dispute_id)
                 if not d["excluded"]}
    ids = list(dict.fromkeys(s.artifact_id for c in claims for s in c.sources))
    result = {}
    for n, sid in enumerate(ids, 1):
        name = documents.get(sid.removeprefix("doc_"))
        result[sid] = (name if name and not (redact_private and is_private(name))
                       else f"Document {n} of {len(ids)}")
    return result


def compose(db_path, dispute_id: str, package_id: int, checks: list, dispute: dict) -> list:
    package = repo.get_package(db_path, package_id)
    if not package or package["dispute_id"] != dispute_id:
        raise ValueError("Package does not belong to this dispute")
    documents = {str(d["id"]): d for d in repo.list_documents(db_path, dispute_id) if not d["excluded"]}
    extracted = extracted_docs_for(db_path, dispute_id)
    readings = {d.doc_id: d for d in extracted}
    untrusted = untrusted_document_ids(checks, extracted)
    by_check = {c.id: c for c in checks}
    result = []
    for row in repo.list_claims(db_path, package_id):
        sources = []
        for sid in row["source_ids"]:
            key = sid.removeprefix("doc_") if sid.startswith("doc_") else sid
            reading = readings.get(key)
            artifact = documents.get(key)
            if reading:
                sha = artifact["sha256"] if artifact else digest(reading.fields)
                kind = "document" if artifact else "seller_statement"
                for name, field in reading.fields.items():
                    value = field.get("value")
                    # Retain explicitly suggested fields even when mismatched to show the actual value.
                    if value is not None and (str(value).casefold() in row["text"].casefold()
                                               or name == row.get("suggested_field")):
                        check = by_check.get(FIELD_CHECK.get(name, name))
                        sources.append(Source(sid, name, sha, str(value),
                                              "untrusted" if key in untrusted else field.get("confidence", "absent"),
                                              check.basis if check else "none",
                                              check.state if check else "present_unverifiable", kind))
                if not any(s.artifact_id == sid for s in sources):
                    text = reading.raw_text
                    sources.append(Source(sid, "extracted_text", sha, text,
                                          "untrusted" if key in untrusted else "read" if text else "illegible",
                                          kind=kind))
            elif sid == "paypal_dispute":
                # A selected normalized fact is an artifact of the read, never the raw PayPal body.
                facts = {k: dispute.get(k) for k in ("dispute_id", "amount_cents", "currency", "reason", "status")}
                for name, value in facts.items():
                    if value is not None and str(value).casefold() in row["text"].casefold():
                        sources.append(Source(sid, name, digest(facts), str(value), "read", kind="paypal_dispute"))
            if not any(s.artifact_id == sid for s in sources):
                sources.append(Source(sid, "unknown", None, None, "absent"))
        result.extend(validate(row["id"], row["text"], row["basis"], tuple(sources)))
    return result
