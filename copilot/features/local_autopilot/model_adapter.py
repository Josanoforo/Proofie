"""Optional adapter for the existing evidence model; default case rendering never calls it."""
import re


def existing_model(dispute, docs, names):
    """Explicit opt-in only. Reuses the project's existing GPU/preflight/model path."""
    from copilot.core.evidence_model import propose_package
    return propose_package(dispute, docs, names, label="case-attention")


def review_sources(dispute, docs, names, backend):
    """Project only cited source IDs into a fixed human-review suggestion, never model prose."""
    if not docs or not callable(backend):
        return None
    try:
        from pilot.step4_evidence_ai import build_schema, validate_schema
        result = backend(dispute, docs, names)
        if not isinstance(result, dict) or result.get("validation_ok") is not True:
            return None
        output = result.get("_output")
        if validate_schema(output, build_schema(["paypal_dispute", *docs])):
            return None
        claims = [claim for item in output["proposed_evidence"] for claim in item["claims"]]
        refs = sorted({source for claim in claims for source in claim["source_ids"]})
        if not refs or any(ref not in docs for ref in refs):
            return None
        if output["missing_evidence"] or output["conflicts"] or output["reviewer_note_draft"] or any(item["notes"] for item in output["proposed_evidence"]):
            return None
        if any(not claim["text"].strip() or not any(claim["text"].casefold() in docs[ref].casefold() for ref in claim["source_ids"]) for claim in claims):
            return None
        # The adapter never publishes model-authored facts, money, dates or status.
        # Reject instead of trying to sanitize invented numerical/state assertions.
        text = " ".join([output["reviewer_note_draft"], *output["missing_evidence"],
                         *(c["description"] for c in output["conflicts"]),
                         *(item["notes"] for item in output["proposed_evidence"]),
                         *(c["text"] for c in claims)])
        if re.search(r"\d|\b(?:paid|refunded|delivered|resolved|approved|closed|denied)\b", text, re.I):
            return None
        if any(item["tracking_info"] for item in output["proposed_evidence"]):
            return None
        return {"label": "Model suggestion: review these cited source documents; human review required.", "source_ids": refs}
    except Exception:
        return None
