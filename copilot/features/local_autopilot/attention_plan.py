"""Pure case attention plan, recalculated from current recorded inputs."""
import hashlib
import json


def build(norm, actions, documents, preparation, recommendation, model_hint=None):
    manifest = [{"id": f"doc_{d['id']}", "sha256": d.get("sha256"), "text_sha256": hashlib.sha256(d["extracted_text"].encode("utf-8")).hexdigest(), "source": d.get("filename", "document")}
                for d in documents if not d.get("excluded") and d.get("extraction_ok") and isinstance(d.get("extracted_text"), str) and d["extracted_text"].strip()]
    manifest.sort(key=lambda item: item["id"])
    snapshot = {"case_id": norm.get("dispute_id"), "state": norm.get("status"), "updated": norm.get("update_time"),
                "amount_cents": norm.get("amount_cents"), "currency": norm.get("currency"),
                "deadline": norm.get("seller_response_due_date"), "deadline_valid": norm.get("deadline_parse_ok"),
                "actions": sorted(actions), "sources": manifest, "preparation": preparation, "recommendation": recommendation}
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    tasks = []
    if not manifest:
        tasks.append({"id": "sources", "text": "Add a readable case document and review its origin.", "href": "documents"})
    elif "changed" in preparation or "not started" in preparation or "failed" in preparation or "unavailable" in preparation:
        tasks.append({"id": "preparation", "text": "Check preparation in the inbox; review any resulting draft.", "href": "inbox"})
    else:
        tasks.append({"id": "sources", "text": "Review the recorded documents and their sources.", "href": "documents"})
    if recommendation:
        tasks.append({"id": "practice", "text": "Check the suggested action against your limits before drafting.", "href": "practice"})
    else:
        tasks.append({"id": "review", "text": "Review the case and available actions before choosing a response.", "href": "documents"})
    return {"fingerprint": fingerprint, "tasks": tasks, "sources": manifest, "model_hint": model_hint,
            "invalidators": "Case state, recorded source data, amount, currency, deadline, available actions or recorded version change this plan."}
