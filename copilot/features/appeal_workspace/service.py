import hashlib
import json
import re


def assemble_case(dispute_id, view, db_path, uploads_dir):
    """Project only the outcome GET and the same case's existing sent package/document records."""
    from copilot.core.raw_storage import read_raw
    from copilot.db import repo
    if not isinstance(dispute_id, str) or not dispute_id or not isinstance(db_path, (str, bytes)) and not hasattr(db_path, "__fspath__"):
        raise ValueError("A valid case and configured Proofie database are required.")
    read = view.read
    confirmed = {row.get("package_sha256") for row in view.file.send_attempts
                 if isinstance(row, dict) and row.get("state") == "confirmed"}
    original_packages = [p for p in view.file.packages if p.get("approval_sha256") in confirmed]
    original_package = max(original_packages, key=lambda p: (p.get("version") or 0, p.get("id") or 0)) if original_packages else None
    original, new, content_hashes, source_rows = [], [], {}, []
    source_catalog, unavailable = [], []
    original_version = None
    if original_package:
        original_version = f"sent-package-v{original_package.get('version')}-id{original_package.get('id')}"
        for index, claim in enumerate(original_package.get("claims") or []):
            row_id = f"sent-claim:{original_package.get('id')}:{index}"
            text = claim.get("text") if isinstance(claim.get("text"), str) else ""
            if not text:
                continue
            source_id = f"package:{original_package.get('id')}:claim:{index}"
            original.append({"id": row_id, "name": f"Package statement {index + 1}", "content": text,
                             "source_id": source_id, "category": "claim"})
            source_catalog.append({"source_id": source_id, "label": f"Sent package v{original_package.get('version')} statement {index + 1}",
                                   "status": "stored package statement; not independently authenticated"})
        for index, attachment in enumerate(original_package.get("attachments") or []):
            digest = attachment.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                unavailable.append({"name": attachment.get("filename", "attachment"), "status": "no valid package attachment hash"})
                continue
            row_id = f"sent-annex:{original_package.get('id')}:{index}"
            source_id = f"package:{original_package.get('id')}:attachment:{index}"
            original.append({"id": row_id, "name": attachment.get("filename", "attachment"),
                             "content": "package attachment hash record", "source_id": source_id, "category": "annex"})
            content_hashes[row_id] = digest
            source_catalog.append({"source_id": source_id, "label": f"Sent package v{original_package.get('version')} attachment hash",
                                   "status": "hash recorded on package attachment; original byte availability is separate"})
    else:
        unavailable.append({"name": "Original sent dossier", "status": "no confirmed send attempt matches a package; original evidence remains unknown"})

    for document in repo.list_documents(db_path, dispute_id):
        if document.get("excluded"):
            continue
        row_id = f"document:{document['id']}"
        source_id = f"Proofie-document:{document['id']}"
        digest = document.get("sha256")
        data = read_raw(uploads_dir, digest) if isinstance(digest, str) else None
        if data is None:
            unavailable.append({"name": document.get("filename", row_id), "status": "original bytes unavailable; excluded from content comparison"})
            source_catalog.append({"source_id": source_id, "label": document.get("filename", row_id), "status": "Proofie row exists; original bytes unavailable"})
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            unavailable.append({"name": document.get("filename", row_id), "status": "stored bytes do not match the document row hash; excluded from comparison"})
            source_catalog.append({"source_id": source_id, "label": document.get("filename", row_id), "status": "hash conflict; not compared"})
            continue
        new.append({"id": row_id, "name": document.get("filename", row_id), "content": "content-addressed original bytes", 
                    "source_id": source_id, "category": "annex"})
        content_hashes[row_id] = actual
        source_catalog.append({"source_id": source_id, "label": document.get("filename", row_id),
                               "status": "actual bytes read and SHA-256 matched to the Proofie document row; authenticity not verified"})

    appeal_available = bool("appeal" in (read.links or ()) and read.appeal_link_present)
    action_status = "available" if appeal_available else "not available"
    action_projection = {"source": "current outcome GET projection", "appeal_link_present": appeal_available,
                         "status": read.status, "outcome_code": read.outcome_code}
    source_rows.append({"id": "current-outcome-appeal-link", "action": "APPEAL", "status": action_status,
                        "name": "Current PayPal outcome GET action-link projection",
                        "content": json.dumps(action_projection, sort_keys=True, ensure_ascii=False)})
    data = {"outcome": {"status": read.status or "unknown", "code": read.outcome_code or "unknown",
                         "source": "current PayPal outcome GET projection"},
            "recorded_actions": [{"action": "APPEAL", "source_id": "current-outcome-appeal-link", "status": action_status}],
            "action_sources": source_rows, "original_version": original_version,
            "original": original, "new": new,
            "interpretation": "No seller interpretation is stored in this comparison."}
    result = build(data, content_hashes=content_hashes)
    result["source_label"] = "Current PayPal outcome GET projection and existing Proofie package/document records; authenticity not verified"
    result["original_package"] = {"id": original_package.get("id"), "version": original_package.get("version")} if original_package else None
    result["sources"] = source_catalog
    result["unavailable_sources"] = unavailable
    result["recovery"] = recovery_context(view, result)
    packages = view.file.packages
    attached_ids = {p["id"]: {a["document_id"] for a in repo.list_attachments(db_path, p["id"])} for p in packages}
    for row in result["proposed_evidence"]:
        document_id = int(row["id"].split(":", 1)[1])
        row["packages"] = [{"id": p.get("id"), "version": p.get("version")}
                           for p in packages if document_id in attached_ids[p["id"]]]
    result["recovery"]["packages"] = [{"id": p.get("id"), "version": p.get("version")} for p in packages]
    from copilot import config
    if config.feature_enabled("sale_lesson"):
        from copilot.features.sale_lesson.service import list_lessons
        result["recovery"]["lesson"] = next((r for r in list_lessons() if r.get("dispute_id") == dispute_id), None)
        result["recovery"]["captures"] = [x for x in ((read.norm or {}).get("seller_transaction_ids") or [])
                                          if isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9]{17}", x)]
        result["recovery"]["lesson_enabled"] = True
    return result


def recovery_context(view, comparison):
    """Read-only handoff for the outcome page; outcome remains the eligibility authority."""
    read = view.read
    if read.norm is None:
        eligibility = "unknown"
        eligibility_reason = "The current outcome could not be read."
    elif read.decided and read.appeal_link_present:
        eligibility = "eligible"
        eligibility_reason = "The current outcome is decided and its read includes an appeal link."
    else:
        eligibility = "not eligible"
        eligibility_reason = "The current read does not show both a decision and an appeal link."
    evidence = comparison.get("proposed_evidence", [])
    if not comparison.get("original_package") or comparison.get("unavailable_sources"):
        novelty = "unknown"
    elif any(row.get("delta") == "new content" for row in evidence):
        novelty = "new"
    elif evidence:
        novelty = "duplicate"
    else:
        novelty = "none"
    attempts = getattr(view, "appeals", ()) or ()
    latest = attempts[-1].get("status") if attempts else None
    if latest == "sent_unconfirmed":
        next_action = "Refresh the recorded outcome and review the uncertain attempt before any further action; do not resend automatically."
    elif latest == "sent_confirmed":
        next_action = "Review the recorded readback and outcome before considering another action."
    elif latest == "failed":
        next_action = "Review the rejection and correct the cause before considering another attempt."
    elif eligibility == "unknown":
        next_action = "Read the outcome again before deciding whether an appeal is available."
    elif eligibility == "not eligible":
        next_action = "Review the outcome; no appeal action is currently shown."
    elif novelty != "new":
        next_action = "Find and compare new evidence before preparing an appeal."
    elif not view.appeal_allowed:
        next_action = "Appeal preparation is switched off; review the outcome and available options."
    else:
        next_action = "Add the new evidence to the case package, then use the existing appeal preview and review."
    return {"eligibility": eligibility, "eligibility_reason": eligibility_reason, "evidence_novelty": novelty, "latest_attempt": latest,
            "next_action": next_action, "source": "current outcome read and same-case recorded evidence",
            "provider_confirmation": "Only a recorded provider readback can confirm an attempt."}

EXAMPLE = {"outcome": {"status": "RESOLVED", "code": "RESOLVED_BUYER_FAVOUR", "source": "recorded official snapshot", "snapshot_id": "outcome-v4"}, "recorded_actions": [{"action": "APPEAL", "source_id": "action-log-9", "status": "available in recorded snapshot"}], "action_sources": [{"id": "action-log-9", "action": "APPEAL", "status": "available", "name": "Recorded dispute action snapshot", "content": "synthetic action availability record"}], "original_version": "dossier-v4", "original": [{"id": "proof", "name": "delivery.txt", "content": "original bytes", "source_id": "annex-1"}], "new": [{"id": "proof", "name": "delivery.txt", "content": "corrected delivery evidence", "source_id": "annex-2"}], "interpretation": "A corrected source may be reviewed."}


def _content_hash(row):
    content = row.get("content")
    return hashlib.sha256(content.encode("utf-8")).hexdigest() if isinstance(content, str) and content else None


def build(data, *, content_hashes=None):
    try:
        if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False)) > 30000:
            raise ValueError("Input must be an object under 30 KB.")
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Input"):
            raise
        raise ValueError("Input must contain JSON values.") from exc
    outcome = data.get("outcome")
    if outcome is not None and not isinstance(outcome, dict):
        raise ValueError("Outcome must be an object or omitted.")
    if outcome is None:
        outcome = {"status": "unknown", "source": "missing"}
    for name in ("original", "new", "recorded_actions", "action_sources"):
        if not isinstance(data.get(name, []), list) or len(data.get(name, [])) > 100 or any(not isinstance(x, dict) for x in data.get(name, [])):
            raise ValueError(f"{name} must be a bounded list of objects.")
    original, new = data.get("original", []), data.get("new", [])
    content_hashes = content_hashes if isinstance(content_hashes, dict) else {}
    for rows in (original, new):
        ids = [x.get("id") for x in rows]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("Evidence IDs must be unique non-empty text.")
        for item in rows:
            if not all(isinstance(item.get(k, ""), str) for k in ("name", "content", "source_id")):
                raise ValueError("Evidence name, content and source_id must be text.")
            if not item.get("content") or not item.get("source_id"):
                raise ValueError("Evidence needs content bytes and source provenance before comparison.")
    actions = data.get("recorded_actions", [])
    source_rows = data.get("action_sources", [])
    source_ids = [x.get("id") for x in source_rows]
    if any(not isinstance(i, str) or not i for i in source_ids) or len(source_ids) != len(set(source_ids)):
        raise ValueError("Action source records require unique IDs.")
    for source in source_rows:
        if not all(isinstance(source.get(k, ""), str) for k in ("action", "status", "name", "content")) or not source.get("content"):
            raise ValueError("Action source records need action, status, name and content bytes.")
    source_map = {x["id"]: x for x in source_rows}
    matching_actions = [a for a in actions if a.get("action") == "APPEAL" and isinstance(a.get("source_id"), str) and a.get("source_id") in source_map and source_map[a["source_id"]].get("action") == "APPEAL" and isinstance(a.get("status"), str) and a.get("status").casefold() in {"available", "available in recorded snapshot", "permitted"} and source_map[a["source_id"]].get("status", "").casefold() in {"available", "permitted"}]
    status = outcome.get("status")
    applicable = isinstance(status, str) and status.upper() in {"RESOLVED", "DENIED", "CLOSED"}
    def digest(item):
        value = content_hashes.get(item.get("id"))
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
            raise ValueError("A source-backed content hash must be a lowercase SHA-256 value.")
        return value or _content_hash(item)
    original_hashed = [{**x, "sha256": digest(x)} for x in original]
    original_by_hash = {(x.get("category", "evidence"), x["sha256"]): x for x in original_hashed}
    delta = []
    for item in new:
        digest_value = digest(item)
        match = original_by_hash.get((item.get("category", "evidence"), digest_value))
        delta.append({**item, "sha256": digest_value, "delta": "unchanged content" if match else "new content", "same_content_as": match["id"] if match else None})
    original_version = data.get("original_version")
    if original_version is not None and (not isinstance(original_version, str) or not original_version or len(original_version) > 100):
        raise ValueError("Original dossier version must be bounded non-empty text.")
    snapshot = hashlib.sha256(json.dumps({"version": original_version, "evidence": original_hashed}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    allowed = bool(matching_actions and applicable and original_version)
    action_sources = [{"id": source_id, "sha256": hashlib.sha256(source_map[source_id]["content"].encode()).hexdigest()} for source_id in (a["source_id"] for a in matching_actions)]
    return {"recorded_outcome": outcome, "interpretation": data.get("interpretation", "") if isinstance(data.get("interpretation", ""), str) else "", "original_version": original_version, "original_snapshot_sha256": snapshot, "original_frozen": original_hashed, "proposed_evidence": delta, "recorded_appeal_action_sources": action_sources, "appeal_preview_allowed": allowed, "eligibility_asserted": False, "reason": "Local comparison preview only; this tool does not determine appeal eligibility" if allowed else "Recorded sourced APPEAL action, applicable recorded status and original dossier version required", "source_label": "User-supplied snapshots; authenticity not verified"}
