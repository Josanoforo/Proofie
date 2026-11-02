from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

REASONS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Item or service not received",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Item or service not as described",
    "UNAUTHORISED": "Unauthorized transaction",
}
KINDS = {"physical", "digital", "service"}
# Exact item_type values that have a directly supported organizer-kind equivalent.
# PRODUCT does not establish that an item is physical; BOOKING is not assumed to be a service.
ITEM_TYPE_TO_KIND = {"SERVICE": "service", "DIGITAL_DOWNLOAD": "digital"}
PAYPAL_ITEM_TYPES = {"PRODUCT", "SERVICE", "BOOKING", "DIGITAL_DOWNLOAD"}
ITEM_TYPE_LABELS = {
    "PRODUCT": "Product",
    "SERVICE": "Service",
    "BOOKING": "Booking",
    "DIGITAL_DOWNLOAD": "Digital download",
}
PROOF_GUIDE = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": {"physical": ["shipment", "tracking", "delivery record"], "digital": ["access or delivery event"], "service": ["appointment or performance record"]},
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": {"physical": ["versioned listing", "variant and condition record"], "digital": ["versioned listing", "access or content version"], "service": ["scope agreed", "service record"]},
    "UNAUTHORISED": {"physical": ["transaction-linked order record"], "digital": ["account/access record linked to transaction"], "service": ["transaction and customer-contact record"]},
}
EXAMPLES = {
    "physical": {"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "kind": "physical", "facts": [{"id": "order", "at": "2026-10-02T09:00:00-06:00", "text": "Order 1842 placed", "source": "order_record", "proof_type": "shipment", "content": "order:1842"}, {"id": "delivery", "at": "2026-10-08T16:00:00-06:00", "text": "Carrier marked delivery", "source": "tracking_record", "proof_type": "tracking", "content": "tracking:delivered"}], "claims": [{"text": "Order 1842 was delivered on 2026-10-08.", "source_ids": ["delivery"], "annex_ids": ["tracking"]}], "annexes": [{"id": "tracking", "name": "tracking.txt", "content": "Carrier delivery record for order 1842"}]},
    "digital": {"reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "kind": "digital", "facts": [{"id": "listing", "at": "2026-10-03T09:00:00-06:00", "text": "Listing describes one year access", "source": "versioned_listing", "proof_type": "versioned listing", "content": "sku:membership-v3"}, {"id": "access", "at": "2026-10-04T10:00:00-06:00", "text": "Account access enabled", "source": "access_log", "proof_type": "access or content version", "content": "event:access-enabled"}], "claims": [{"text": "Access was enabled on 2026-10-04.", "source_ids": ["access"]}], "annexes": [{"id": "access-log", "name": "access.txt", "content": "Synthetic account access event"}]},
    "incomplete": {"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "kind": "service", "facts": [{"id": "appointment", "at": "2026-10-09T11:00:00-06:00", "text": "Appointment requested", "source": "booking_record", "proof_type": "appointment or performance record", "content": "booking:42"}], "claims": [{"text": "The service was completed.", "source_ids": ["missing-completion"]}], "annexes": [{"id": "completion", "name": "completion.pdf", "content": ""}]},
}


def _rows(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = raw.get(key, [])
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, dict) for x in value):
        raise ValueError(f"{key} must be a list of at most 100 objects.")
    return value


def _case_item_profile(dispute_raw: dict[str, Any]) -> dict[str, Any]:
    """Preserve observed PayPal item_type values and explain why a single organizer kind may be absent."""
    transactions = dispute_raw.get("disputed_transactions")
    observed: list[str] = []
    missing = 0
    malformed = 0
    unrecognized: list[str] = []
    if transactions is None or transactions == []:
        missing += 1
    elif not isinstance(transactions, list) or len(transactions) > 100:
        malformed += 1
    else:
        for transaction in transactions:
            if not isinstance(transaction, dict):
                malformed += 1
                continue
            items = transaction.get("items")
            if items is None or items == []:
                missing += 1
                continue
            if not isinstance(items, list) or len(items) > 100:
                malformed += 1
                continue
            for item in items:
                if not isinstance(item, dict):
                    malformed += 1
                    continue
                value = item.get("item_type")
                if value is None or value == "":
                    missing += 1
                elif isinstance(value, str) and value in PAYPAL_ITEM_TYPES:
                    if value not in observed:
                        observed.append(value)
                elif isinstance(value, str):
                    safe_value = value[:120]
                    if safe_value not in unrecognized:
                        unrecognized.append(safe_value)
                else:
                    malformed += 1

    unmapped = [value for value in observed if value not in ITEM_TYPE_TO_KIND]
    mixed = len(observed) + len(unrecognized) > 1
    mapped = [ITEM_TYPE_TO_KIND[value] for value in observed if value in ITEM_TYPE_TO_KIND]
    causes = []
    if mixed:
        causes.append("mixed")
    if missing:
        causes.append("missing")
    if unrecognized:
        causes.append("unrecognized")
    if malformed:
        causes.append("malformed")
    if unmapped:
        causes.append("no_supported_mapping")
    kind = mapped[0] if not causes and len(mapped) == 1 else "unknown"
    return {
        "kind": kind,
        "registered_types": observed,
        "unmapped_registered_types": unmapped,
        "unrecognized_values": unrecognized,
        "missing_count": missing,
        "malformed_count": malformed,
        "mixed": mixed,
        "causes": causes,
    }


def _profile_explanation(profile: dict[str, Any], *, standalone_kind: bool = False) -> str:
    kind = profile.get("kind", "unknown")
    registered = profile.get("registered_types", [])
    unrecognized = profile.get("unrecognized_values", [])
    details = []
    if registered:
        labels = [ITEM_TYPE_LABELS.get(value, value) for value in registered]
        details.append("Recorded transaction item type" + ("s" if len(labels) != 1 else "") + ": " + ", ".join(labels) + ".")
    if unrecognized:
        details.append("The transaction record contains an unrecognized item type" + (" value" if len(unrecognized) == 1 else " values") + ": " + ", ".join(unrecognized) + ". Proofie cannot select type-specific suggestions for those items.")
    if profile.get("mixed"):
        details.append("This case contains different transaction item types, so Proofie cannot select one set of type-specific suggestions.")
    if profile.get("missing_count"):
        details.append("No type was selected for this dossier, so Proofie cannot select type-specific suggestions." if standalone_kind else
                       "Some transaction items have no recorded type, so Proofie cannot select type-specific suggestions for the whole case.")
    if profile.get("malformed_count"):
        details.append("Some transaction item records cannot be read, so Proofie cannot determine their type.")
    unmapped = profile.get("unmapped_registered_types", [])
    if "PRODUCT" in unmapped:
        details.append("The recorded Product type does not distinguish a physical product from a digital one, so Proofie cannot select type-specific evidence suggestions.")
    if "BOOKING" in unmapped:
        details.append("The transaction record lists Booking. Proofie has no Booking-specific suggestions configured.")
    if kind != "unknown":
        if standalone_kind:
            return f"Selected type for this dossier: {kind}."
        return " ".join(details)
    if not details:
        details.append("No type was selected for this dossier, so Proofie cannot select type-specific suggestions." if standalone_kind else
                       "No transaction item types are recorded for this case, so Proofie cannot select type-specific suggestions.")
    return " ".join(details)


def _item_type_cause(profile: dict[str, Any]) -> str:
    causes = []
    if profile.get("mixed"):
        registered = ", ".join(profile.get("registered_types", []) + profile.get("unrecognized_values", []))
        causes.append(f"transaction item types are mixed ({registered})")
    if profile.get("missing_count"):
        causes.append("item_type is missing for one or more transaction items")
    if profile.get("unrecognized_values"):
        causes.append("item_type contains unrecognized value(s): " + ", ".join(profile["unrecognized_values"]))
    if profile.get("malformed_count"):
        causes.append("one or more transaction item records are malformed")
    unmapped = profile.get("unmapped_registered_types", [])
    if unmapped:
        causes.append("registered type(s) without a supported organizer-kind mapping: " + ", ".join(unmapped))
    return "; ".join(causes) or "no transaction item_type is available"


def _case_matrix_basis(reason: str | None, profile: dict[str, Any], *, standalone: bool = False) -> str:
    if reason not in REASONS:
        source = "this dossier" if standalone else "this record"
        consequence = f"No suggestions selected because the case reason is not recognized in {source}."
    elif profile.get("kind") == "unknown":
        consequence = "No type-specific suggestions selected for this dossier." if standalone else "No type-specific suggestions selected for this case."
    else:
        consequence = "Suggestions shown use the case reason and type supplied for this dossier." if standalone else \
            "Suggestions shown are based on the recorded case reason and transaction item type."
    return consequence + " Organizer suggestions are internal guidance, not PayPal requirements."


def assemble(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Input must be a JSON object containing JSON values.") from exc
    if not isinstance(raw, dict) or len(encoded) > 50000:
        raise ValueError("Input must be a JSON object under 50 KB.")
    facts, claims, annexes = (_rows(raw, key) for key in ("facts", "claims", "annexes"))
    reason_raw, kind_raw = raw.get("reason"), raw.get("kind")
    reason = reason_raw if isinstance(reason_raw, str) and reason_raw in REASONS else None
    kind = kind_raw if isinstance(kind_raw, str) and kind_raw in KINDS else "unknown"
    for row_type, rows in (("fact", facts), ("claim", claims), ("annex", annexes)):
        ids = [r.get("id") for r in rows if "id" in r]
        if any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError(f"Every {row_type} id must be a non-empty string.")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate {row_type} ids are not allowed.")
    for fact in facts:
        if not all(isinstance(fact.get(k, ""), str) for k in ("text", "source", "proof_type", "content", "at", "status")):
            raise ValueError("Fact text, source, proof_type, content, at and status must be strings.")
        if any(len(fact.get(k, "")) > limit for k, limit in (("text", 2000), ("content", 20000), ("source", 200), ("proof_type", 200))):
            raise ValueError("Fact field exceeds its length limit.")
    sources = {x["id"]: x for x in facts if "id" in x}
    annex_by = {x["id"]: x for x in annexes if "id" in x}
    claim_rows = []
    for claim in claims:
        text, refs, annex_refs = claim.get("text", ""), claim.get("source_ids", []), claim.get("annex_ids", [])
        if not isinstance(text, str) or len(text) > 2000 or not isinstance(refs, list) or not isinstance(annex_refs, list):
            raise ValueError("Claim text must be a string (max 2000 chars); source_ids and annex_ids must be lists.")
        if any(not isinstance(ref, str) for ref in refs + annex_refs):
            raise ValueError("Claim references must be strings.")
        resolved = [sources[r] for r in refs if r in sources]
        missing = [r for r in refs if r not in sources]
        annex_rows_for_claim = [annex_by[r] for r in annex_refs if r in annex_by]
        missing_annexes = [r for r in annex_refs if r not in annex_by]
        absent_content = [x["id"] for x in annex_rows_for_claim if not isinstance(x.get("content"), str) or not x.get("content")]
        if any("conflict" in str(x.get("status", "")).casefold() for x in resolved):
            state = "conflict"
        elif not resolved or missing or missing_annexes or absent_content:
            state = "unsupported or incomplete"
        else:
            state = "source-linked; human review required"
        claim_rows.append({"text": text, "source_ids": refs, "resolved": resolved, "missing": missing, "annex_ids": annex_refs, "missing_annexes": missing_annexes, "annexes_without_content": absent_content, "status": state})
    annex_output = []
    for annex in annexes:
        content = annex.get("content")
        if "content" in annex and not isinstance(content, str):
            raise ValueError("Annex content must be text when supplied.")
        if not isinstance(annex.get("name", ""), str) or len(annex.get("name", "")) > 200:
            raise ValueError("Annex name must be a string under 200 characters.")
        has_bytes = isinstance(content, str) and bool(content)
        annex_output.append({"id": annex.get("id"), "name": annex.get("name", ""), "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest() if has_bytes else None, "included": has_bytes, "status": "content available for preview" if has_bytes else "content missing; metadata only", "synthetic": bool(annex.get("synthetic", False)), "content_preview": content[:500] if has_bytes else None})
    suggestions = PROOF_GUIDE.get(reason, {}).get(kind, []) if reason and kind != "unknown" else []
    matrix_rows = []
    for suggestion in suggestions:
        matching = [f for f in facts if f.get("proof_type") == suggestion]
        matrix_rows.append({"proof_type": suggestion, "basis": "Internal organizer suggestion; not a PayPal requirement. Confirm against the case request and cited policy.", "source_ids": [f.get("id") for f in matching], "status": "source supplied; authenticity unverified" if matching else "missing"})

    def chronology_key(f: dict[str, Any]) -> tuple[int, Any]:
        value = f.get("at", "")
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
            if dt is None or dt.tzinfo is None:
                return (1, datetime.max.replace(tzinfo=timezone.utc))
            return (0, dt.astimezone(timezone.utc))
        except ValueError:
            return (1, datetime.max.replace(tzinfo=timezone.utc))

    chronology = sorted(facts, key=chronology_key)
    standalone_profile = {"kind": kind, "registered_types": [], "unmapped_registered_types": [],
                          "unrecognized_values": [], "missing_count": int(kind == "unknown"),
                          "malformed_count": 0, "mixed": False, "causes": []}
    standalone_reason = ("Case reason: " + REASONS[reason] + ".") if reason else "Case reason is not recognized in this dossier."
    matrix_basis = _case_matrix_basis(reason, standalone_profile, standalone=True)
    return {"reason": reason, "reason_label": REASONS.get(reason, "Unknown reason"), "reason_input": reason_raw, "reason_explanation": standalone_reason, "kind": kind,
            "kind_explanation": _profile_explanation(standalone_profile, standalone_kind=True),
            "item_type_profile": standalone_profile,
            "proof_matrix": {"basis": matrix_basis, "items": matrix_rows}, "facts": facts, "chronology": chronology, "claims": claim_rows, "annexes": annex_output, "missing_sources": [m for row in claim_rows for m in row["missing"]], "preview": {"claim_text": [r["text"] for r in claim_rows], "annexes": [{k: a[k] for k in ("id", "name", "sha256", "included")} for a in annex_output]}, "source_label": "User-supplied recorded data; authenticity is not verified"}


# Exact correspondences only: seller classification is not proof of authenticity.
READINESS_TO_PROOF = {
    ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "physical", "tracking"): "tracking",
    ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "physical", "delivery"): "delivery record",
    ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "digital", "access"): "access or delivery event",
    ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "service", "performance"): "appointment or performance record",
    ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "digital", "access"): "access or content version",
    ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "service", "agreement"): "scope agreed",
    ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "service", "performance"): "service record",
}


def _readiness_associations(norm: dict) -> tuple[dict, str | None]:
    """Read existing seller classifications only for one unambiguous linked transaction."""
    from copilot import config
    if not config.feature_enabled("sale_readiness"):
        return {}, None
    from copilot.features.fulfilment.tracker_request import validate_capture_id
    from copilot.features.sale_readiness.service import load_case_terms
    ids = norm.get("seller_transaction_ids") or []
    if not isinstance(ids, list):
        return {}, None
    ids = sorted({x for x in ids if isinstance(x, str) and not validate_capture_id(x)})
    dispute_id = norm.get("dispute_id")
    if len(ids) != 1 or not isinstance(dispute_id, str):
        return {}, None
    try:
        record = load_case_terms(dispute_id, ids[0])
    except (OSError, TypeError, ValueError):
        return {}, None
    return (record.get("terms", {}) if record else {}), ids[0]


def paypal_tracker_record(norm: dict) -> dict | None:
    """PayPal's own tracker record for the one sale of this case, when the seller registered the tracking through Proofie
    and the stored read-back verifies (its hashes match and PayPal listed the tracker). Local read only. None otherwise,
    and then the dossier is unchanged. It records what PayPal lists, not that the parcel arrived."""
    from copilot import config
    from copilot.features.fulfilment.tracker_request import validate_capture_id
    ids = norm.get("seller_transaction_ids") if isinstance(norm, dict) else None
    ids = sorted({x for x in ids if isinstance(x, str) and not validate_capture_id(x)}) if isinstance(ids, list) else []
    if len(ids) != 1:
        return None
    try:
        from copilot.features.delivery_facts import service as facts
        record = facts.read(config.db_path(), ids[0])
    except (ImportError, OSError, ValueError):
        return None
    seen = record.get("readback")
    if record.get("record_status") != "registered" or not seen:
        return None
    return {"capture_id": ids[0], "tracking_number": seen["tracking"], "carrier": seen["carrier"], "carrier_name": seen["carrier_name"],
            "status": seen["status"], "read_back_at": record["saved_at"], "sha256": record["sha256"],
            "source": "PayPal's tracker listing for this sale, read after the seller registered the tracking",
            "note": "This is what PayPal lists for the registered tracking. It does not show that the parcel was shipped or delivered."}


def assemble_case(dispute_raw: dict, norm: dict, claims: list[dict], documents: list[dict],
                  attachments: list[dict], manual_fields: dict, uploads_dir) -> dict[str, Any]:
    """Project the package's existing case records into the dossier without a second case store.

    File hashes are calculated only from original stored bytes that match the upload's recorded digest.
    Extracted text is display/search context and never substituted for a binary file's bytes.
    """
    if not isinstance(dispute_raw, dict) or not isinstance(norm, dict):
        raise ValueError("The current case read from PayPal is unavailable.")
    from copilot.core.raw_storage import read_raw
    from copilot.core.content_type import ALLOWED_CONTENT_TYPES, MAX_FILE_BYTES

    facts: list[dict[str, Any]] = []
    facts_by_id: dict[str, dict] = {}

    def add_fact(row: dict) -> None:
        facts.append(row)
        facts_by_id[row["id"]] = row

    reason = norm.get("reason")
    reason_status = "recorded in the current case read" if reason in REASONS else "unknown in the current case read"
    add_fact({"id": "paypal_dispute", "at": norm.get("update_time") or "", "text": reason or "Reason not provided",
              "source": "Current PayPal case read", "proof_type": "case request", "content": "",
              "status": reason_status})
    source_doc_by_id: dict[int, dict] = {}
    for doc in documents:
        doc_id = doc.get("id")
        if type(doc_id) is not int:
            continue
        source_doc_by_id[doc_id] = doc
        extracted = doc.get("extracted_text") if doc.get("extraction_ok") else ""
        content = extracted if isinstance(extracted, str) else ""
        add_fact({"id": f"doc_{doc_id}", "at": doc.get("uploaded_at") or "", "text": content or
                  f"Stored file: {doc.get('filename', 'unnamed document')}",
                  "source": f"Proofie stored document {doc.get('filename', 'unnamed document')}",
                  "proof_type": "seller supplied record", "content": content,
                  "status": "excluded from current case inputs" if doc.get("excluded") else
                            "unreadable; source content unavailable" if not doc.get("extraction_ok") else
                            "recorded text; authenticity unverified"})
    if manual_fields:
        details = [f"{name}: {row.get('value', '')}" for name, row in sorted(manual_fields.items())
                   if isinstance(name, str) and isinstance(row, dict) and isinstance(row.get("value"), str)]
        add_fact({"id": "doc_manual", "at": "", "text": "Seller-entered package details",
                  "source": "Seller-entered Proofie fields", "proof_type": "seller statement",
                  "content": "\n".join(details), "status": "seller statement; authenticity unverified"})

    attachment_by_id = {a.get("document_id"): a for a in attachments if type(a.get("document_id")) is int}
    annexes: list[dict] = []
    actual: dict[str, dict] = {}
    for doc_id, doc in source_doc_by_id.items():
        annex_id = f"doc_{doc_id}"
        recorded_sha = doc.get("sha256")
        data = read_raw(uploads_dir, recorded_sha) if isinstance(recorded_sha, str) and \
            re.fullmatch(r"[0-9a-f]{64}", recorded_sha) else None
        digest = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None
        valid_bytes = bool(data is not None and digest == doc.get("sha256"))
        extracted = doc.get("extracted_text") if doc.get("extraction_ok") else ""
        annexes.append({"id": annex_id, "name": doc.get("filename", "unnamed document"),
                        "content": extracted if isinstance(extracted, str) else ""})
        attachment = attachment_by_id.get(doc_id)
        attachment_matches = bool(attachment and attachment.get("sha256") == digest and valid_bytes)
        attachment_label = "attached" if attachment is not None else "not attached"
        if not valid_bytes:
            byte_status = f"{attachment_label}; original bytes unavailable" if data is None else \
                f"{attachment_label}; original byte hash does not match the stored digest"
        elif attachment is not None and not attachment_matches:
            byte_status = "attached; package attachment digest does not match original bytes"
        elif attachment_matches:
            byte_status = "attached; original bytes and package digest match"
        else:
            byte_status = "source document not attached to this package"
        sendable_type = doc.get("content_type") in ALLOWED_CONTENT_TYPES
        within_limit = type(doc.get("size_bytes")) is int and 0 <= doc["size_bytes"] < MAX_FILE_BYTES
        actual[annex_id] = {"sendable_type": sendable_type, "within_limit": within_limit, "sha256": digest if valid_bytes else None, "bytes_available": valid_bytes,
                            "attached": attachment is not None, "attachment_matches": attachment_matches,
                            "status": byte_status,
                            "content_preview": (extracted[:500] if valid_bytes and isinstance(extracted, str) and extracted else None)}

    dossier_claims = []
    for claim in claims:
        refs = claim.get("source_ids") if isinstance(claim.get("source_ids"), list) else []
        dossier_claims.append({"text": claim.get("text", ""), "source_ids": refs,
                               "annex_ids": [ref for ref in refs if ref in actual],
                               "basis": claim.get("basis", "unknown")})
    item_type_profile = _case_item_profile(dispute_raw)
    kind = item_type_profile["kind"]
    result = assemble({"reason": reason, "kind": kind, "facts": facts,
                       "claims": dossier_claims, "annexes": annexes})
    terms, capture_id = _readiness_associations(norm)
    seller_kind = terms.get("kind")
    # Do not overwrite a conflicting recorded item type or guess from a filename.
    if kind == "unknown" and seller_kind in KINDS and not item_type_profile.get("mixed"):
        kind = seller_kind
        result["proof_matrix"]["items"] = [{"proof_type": suggestion, "source_ids": [],
            "basis": "Internal organizer suggestion; not a PayPal requirement."}
            for suggestion in PROOF_GUIDE.get(reason, {}).get(kind, [])]
        result["kind_explanation"] = f"Seller selected {kind} in the sale checklist; transaction type remains {item_type_profile['kind']}."
    classifications = terms.get("evidence_kinds") if isinstance(terms.get("evidence_kinds"), dict) else {}
    compatible = seller_kind == kind and kind in KINDS
    for row in result["proof_matrix"]["items"]:
        linked = []
        for doc_id, doc in source_doc_by_id.items():
            category = classifications.get(str(doc_id))
            if not compatible or READINESS_TO_PROOF.get((reason, kind, category)) != row["proof_type"]:
                continue
            item = actual[f"doc_{doc_id}"]
            if doc.get("excluded") or not item["bytes_available"]:
                continue
            linked.append({"id": doc_id, "name": doc.get("filename", "unnamed document"),
                           "attached": item["attachment_matches"], "status": item["status"]})
        row["sources"] = linked
        row["source_ids"] = [f"doc_{source['id']}" for source in linked]
        row["status"] = "seller-associated original; authenticity unverified" if linked else "association not determined; review case documents"
    result["readiness_capture"] = capture_id
    for annex in result["annexes"]:
        case_item = actual[annex["id"]]
        annex.update(case_item)
        annex["included"] = case_item["attached"]
    for row, claim in zip(result["claims"], dossier_claims):
        row["basis"] = claim["basis"]
        row["source_links"] = [{"id": ref, "label": facts_by_id[ref]["source"], "status": facts_by_id[ref]["status"]}
                               for ref in row["source_ids"] if ref in facts_by_id]
        source_problem = any(link["status"].startswith(("unreadable", "excluded")) for link in row["source_links"])
        annex_problem = any(aid not in actual or not actual[aid]["bytes_available"] or
                            (actual[aid]["attached"] and not actual[aid]["attachment_matches"])
                            for aid in row["annex_ids"])
        if row["missing"] or row["missing_annexes"] or source_problem or annex_problem:
            row["status"] = "unsupported or incomplete"
        elif any(link["status"].startswith("seller statement") for link in row["source_links"]):
            row["status"] = "seller statement; human review required"
        else:
            row["status"] = "source-linked; human review required"
    result["preview"]["annexes"] = [{k: a[k] for k in ("id", "name", "sha256", "included", "status")}
                                     for a in result["annexes"]]
    result["kind"] = kind
    result["item_type_profile"] = item_type_profile
    result["reason_explanation"] = ("Case reason: " + REASONS[reason] + ".") if reason in REASONS else "Case reason is not recognized in this record."
    result["kind_explanation"] = result.get("kind_explanation") if seller_kind in KINDS and item_type_profile["kind"] == "unknown" and kind == seller_kind else _profile_explanation(item_type_profile)
    result["proof_matrix"]["basis"] = ("Suggestions use the seller-selected sale type; they are internal guidance, not PayPal requirements."
        if item_type_profile["kind"] == "unknown" and seller_kind == kind and kind in KINDS else
        _case_matrix_basis(reason if reason in REASONS else None, item_type_profile))
    result["paypal_tracker_record"] = paypal_tracker_record(norm)
    if result["paypal_tracker_record"]:
        for row in result["proof_matrix"]["items"]:
            if row["proof_type"] == "tracking":
                row["status"] += "; PayPal's own tracker record is on file (see below)"
    result["source_label"] = "The current case read from PayPal plus Proofie-stored records; source authenticity is not independently verified"
    return result


def pretty(raw: str) -> str:
    return json.dumps(assemble(json.loads(raw)), ensure_ascii=False, indent=2)
