"""Read-only adversarial projection over existing claims and checklist readings.

No model, writes, approval snapshot changes, or reconstruction of historical source bytes.
"""
from __future__ import annotations

import hashlib
import re

from copilot.core.checklist_input import extracted_docs_for
from copilot.core.evidence_model import untrusted_document_ids
from copilot.core.raw_storage import read_raw
from copilot.db import repo
from copilot.features.claims.model import safe_text
from copilot.features.claims.service import FIELD_CHECK, compose
from copilot import config


def _document_state(doc, uploads_dir):
    if not doc:
        return "missing"
    if doc.get("excluded"):
        return "excluded"
    sha = doc.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return "original unavailable"
    try:
        data = read_raw(uploads_dir, sha)
    except OSError:
        data = None
    if data is None:
        return "original unavailable"
    if hashlib.sha256(data).hexdigest() != sha:
        return "original changed"
    if not doc.get("extraction_ok"):
        return "illegible"
    return "available"


def adversarial_review(db_path, dispute_id, package_id, checks, dispute):
    """Project literal comparisons and observed stored version differences for one case/package."""
    package = repo.get_package(db_path, package_id)
    if not package or package.get("dispute_id") != dispute_id:
        raise ValueError("Package does not belong to this dispute")
    composed = compose(db_path, dispute_id, package_id, checks, dispute)
    raw_claims = {row["id"]: row for row in repo.list_claims(db_path, package_id)}
    docs = {str(row["id"]): row for row in repo.list_documents(db_path, dispute_id)}
    readings = {str(row.doc_id): row for row in extracted_docs_for(db_path, dispute_id)}
    untrusted = {str(i) for i in untrusted_document_ids(checks, list(readings.values()))}
    checks_by_id = {check.id: check for check in checks}
    rows = []
    for claim in composed:
        stored = raw_claims[claim.claim_id]
        state_by_source = {}
        for source_id in stored.get("source_ids", []):
            if source_id.startswith("doc_"):
                key = source_id[4:]
                state = _document_state(docs.get(key), config.uploads_dir())
                if key in untrusted and state == "available": state = "untrusted"
                state_by_source[source_id] = state
            else:
                state_by_source[source_id] = "recorded case source" if source_id == "paypal_dispute" else "missing"
        support, opposing = [], []
        for source in claim.sources:
            if source.field in ("unknown", "extracted_text") or source.confidence != "read" or not source.value:
                continue
            if source.value.casefold() not in claim.text.casefold():
                continue
            source_state = state_by_source.get(source.artifact_id, "missing")
            if source_state != "available":
                continue
            check = checks_by_id.get(FIELD_CHECK.get(source.field, source.field))
            if not check or check.state != "conflict" or f"document:{source.artifact_id.removeprefix('doc_')}" not in check.sources:
                continue
            others = []
            for ref in check.sources:
                if not isinstance(ref, str) or not ref.startswith("document:"):
                    continue
                key = ref.removeprefix("document:")
                other_doc, reading = docs.get(key), readings.get(key)
                other_state = _document_state(other_doc, config.uploads_dir())
                if key in untrusted and other_state == "available": other_state = "untrusted"
                if not reading and other_state == "available": other_state = "missing reading"
                state_by_source.setdefault(f"doc_{key}", other_state)
                if other_state != "available":
                    continue
                field = reading.fields.get(source.field) or {}
                value = field.get("value")
                if field.get("confidence") != "read" or value is None or str(value).casefold() == source.value.casefold():
                    continue
                others.append({"id": f"doc_{key}", "name": safe_text(other_doc.get("filename", "Document")),
                               "field": source.field, "value": safe_text(value), "sha256": other_doc.get("sha256")})
            if others:
                own = docs.get(source.artifact_id.removeprefix("doc_")) or {}
                support.append({"id": source.artifact_id, "name": safe_text(own.get("filename", "Document")),
                                "field": source.field, "value": safe_text(source.value), "sha256": source.sha256})
                opposing.extend(others)
        invalid = [k for k, v in state_by_source.items() if v in {"missing", "excluded", "original unavailable", "original changed", "illegible", "untrusted", "missing reading"}]
        if invalid:
            assessment = "source needs attention"
        elif support and opposing:
            assessment = "conflicting source readings"
        else:
            assessment = "corroboration needed"
        rows.append({"key": claim.key, "claim": safe_text(claim.text), "basis": claim.claim_basis,
                     "assessment": assessment, "support": support, "opposing": opposing,
                     "sources": [{"id": safe_text(k), "state": v} for k, v in state_by_source.items()],
                     "verdict": claim.verdict})
    packages = repo.list_packages(db_path, dispute_id)
    prior = [p for p in packages if p["version"] < package["version"]]
    previous = max(prior, key=lambda p: (p["version"], p["id"])) if prior else None
    changes = []
    if previous:
        old = {(r["evidence_index"], r["claim_index"]): r for r in repo.list_claims(db_path, previous["id"])}
        new = {(r["evidence_index"], r["claim_index"]): r for r in raw_claims.values()}
        for slot in sorted(set(old) | set(new)):
            a, b = old.get(slot), new.get(slot)
            if a is None: kind = "added"
            elif b is None: kind = "removed"
            elif (a.get("text"), a.get("source_ids"), a.get("basis")) != (b.get("text"), b.get("source_ids"), b.get("basis")): kind = "changed"
            else: continue
            changes.append({"slot": slot, "kind": kind, "before": safe_text(a.get("text", "")) if a else None,
                            "after": safe_text(b.get("text", "")) if b else None,
                            "sources_changed": bool(a and b and a.get("source_ids") != b.get("source_ids"))})
    return {"package_id": package_id, "version": package["version"], "rows": rows, "changes": changes,
            "previous_package_id": previous["id"] if previous else None,
            "previous_version": previous["version"] if previous else None}
