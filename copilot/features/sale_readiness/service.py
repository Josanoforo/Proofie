import json
import os
import re
from datetime import date
from pathlib import Path

from copilot import config
from copilot.core.carriers import KNOWN_CODES, map_carrier
from copilot.web.security import validate_dispute_id
from copilot.features.fulfilment.tracker_request import validate_capture_id

REQUIRED_EVIDENCE = {"physical": {"order", "tracking", "delivery"}, "digital": {"purchase", "access"}, "service": {"agreement", "acceptance", "performance"}}
# USPS remains supported by this local checklist even though it is not in the
# shared Tracking catalogue used by fulfilment.
LEGACY_TRACKING_CARRIERS = {"USPS"}
_AMOUNT = re.compile(r"^(0|[1-9][0-9]{0,8})(?:[.,]([0-9]{1,2}))?$")


def parse_price_cents(value):
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value.strip()):
        raise ValueError("Enter a non-negative price with up to two decimal places.")
    match = _AMOUNT.fullmatch(value.strip())
    return int(match.group(1)) * 100 + int((match.group(2) or "").ljust(2, "0") or "0")
EXAMPLES = {
    "physical": {"kind": "physical", "transaction_ref": "SALE-1001", "buyer_ref": "BUYER-1", "description": "Handmade lamp", "variant": "Blue", "price_cents": 4200, "currency": "USD", "expected_date": "2026-10-25", "returns": "30 day returns", "contact": "support@example.invalid", "terms_version": "terms-v2", "acceptance_ref": "acceptance-771", "carrier": "USPS", "tracking_number": "SYNTHETIC-TRACK-1", "ship_to": {"name": "Synthetic Buyer", "street": "100 Example Street", "city": "Sample City", "region": "CA", "postal_code": "90000", "country": "US"}, "evidence": [{"id": "o1", "kind": "order", "value": "Order 1001", "source": "local order", "buyer_ref": "BUYER-1", "transaction_ref": "SALE-1001"}, {"id": "t1", "kind": "tracking", "value": "SYNTHETIC-TRACK-1", "source": "local record", "buyer_ref": "BUYER-1", "transaction_ref": "SALE-1001"}, {"id": "d1", "kind": "delivery", "value": "Delivered", "source": "recorded carrier event", "buyer_ref": "BUYER-1", "transaction_ref": "SALE-1001"}]},
    "digital": {"kind": "digital", "transaction_ref": "SALE-1002", "buyer_ref": "BUYER-2", "description": "Course access", "variant": "Annual", "price_cents": 9900, "currency": "MXN", "expected_date": "2026-10-30", "returns": "See terms-v4", "contact": "help@example.invalid", "terms_version": "terms-v4", "acceptance_ref": "accept-93", "evidence": [{"id": "p1", "kind": "purchase", "value": "Purchase recorded", "source": "local order", "buyer_ref": "BUYER-2", "transaction_ref": "SALE-1002"}, {"id": "a1", "kind": "access", "value": "Enabled", "source": "local access log", "buyer_ref": "BUYER-2", "transaction_ref": "SALE-1002"}]},
    "service": {"kind": "service", "transaction_ref": "SALE-1003", "buyer_ref": "BUYER-3", "description": "Consultation", "variant": "One hour", "price_cents": 15000, "currency": "MXN", "expected_date": "2026-10-29", "returns": "Reschedule once", "contact": "team@example.invalid", "terms_version": "terms-v1", "acceptance_ref": "", "evidence": [{"id": "ag1", "kind": "agreement", "value": "Scope agreed", "source": "service order", "buyer_ref": "BUYER-3", "transaction_ref": "SALE-1003"}]},
}


def _case_path(dispute_id, capture_id):
    if validate_dispute_id(dispute_id) or validate_capture_id(capture_id):
        raise ValueError("Invalid dispute or transaction reference.")
    root = config.data_dir() / "mejoras10" / "04" / "cases"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{dispute_id}--{capture_id}.json"
    if path.parent.resolve() != root.resolve() or path.is_symlink():
        raise ValueError("Sale terms path is outside the feature-owned case storage.")
    return path


def load_case_terms(dispute_id, capture_id):
    path = _case_path(dispute_id, capture_id)
    if not path.exists():
        return None
    if path.stat().st_size > 20000:
        raise ValueError("Saved sale terms exceed the 20 KB limit.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("dispute_id") != dispute_id or value.get("capture_id") != capture_id or not isinstance(value.get("terms"), dict):
        raise ValueError("Saved sale terms have an invalid schema or case reference.")
    return value


def save_case_terms(dispute_id, capture_id, terms):
    if not isinstance(terms, dict) or len(json.dumps(terms, ensure_ascii=False)) > 15000:
        raise ValueError("Sale terms must be a bounded JSON object under 15 KB.")
    allowed = {"kind", "buyer_ref", "description", "variant", "price_cents", "currency", "expected_date",
               "returns", "contact", "terms_version", "acceptance_ref", "carrier", "tracking_number", "ship_to",
               "evidence_kinds"}
    if set(terms) != allowed:
        raise ValueError("Sale terms contain missing or unsupported fields.")
    if terms.get("kind") not in REQUIRED_EVIDENCE or type(terms.get("price_cents")) is not int or terms["price_cents"] < 0 or not isinstance(terms.get("currency"), str) or not re.fullmatch(r"[A-Z]{3}", terms["currency"]):
        raise ValueError("Sale type, integer cents and uppercase currency are required.")
    string_fields = allowed - {"price_cents", "ship_to", "evidence_kinds"}
    if any(not isinstance(terms.get(key), str) or len(terms[key]) > 1000 for key in string_fields):
        raise ValueError("Sale term fields must be bounded text.")
    if any(not isinstance(terms.get("ship_to", {}).get(key), str) for key in ("name", "street", "city", "region", "postal_code", "country")):
        raise ValueError("Ship-to fields must be text.")
    if not isinstance(terms.get("evidence_kinds"), dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in terms["evidence_kinds"].items()):
        raise ValueError("Evidence classifications must map stored document IDs to type labels.")
    record = {"schema_version": 1, "dispute_id": dispute_id, "capture_id": capture_id, "terms": terms}
    path = _case_path(dispute_id, capture_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return record


def case_view(dispute_id, capture_id, transaction, documents, attachments, terms_record, existing_tracking=None):
    """Build a checklist from the real case rows/sale context plus explicitly labelled seller statements."""
    transaction = transaction if isinstance(transaction, dict) else {}
    documents = documents if isinstance(documents, list) else []
    attachments = attachments if isinstance(attachments, list) else []
    if not terms_record:
        return {"result": None, "terms": {}, "documents": documents, "attachments": attachments,
                "gaps": ["Sale type, buyer reference, terms and acceptance were not recorded in Proofie."],
                "source_label": "Current transaction read and Proofie document rows; seller sale terms not supplied."}
    terms = terms_record["terms"]
    kinds = terms.get("evidence_kinds") if isinstance(terms.get("evidence_kinds"), dict) else {}
    evidence = []
    for doc in documents:
        if not isinstance(doc, dict) or doc.get("excluded"):
            continue
        key = str(doc.get("id"))
        kind = kinds.get(key)
        if not kind:
            continue
        evidence.append({"id": f"doc_{key}", "kind": kind,
                         "value": f"Proofie stored document {key}: {doc.get('filename', 'filename unavailable')}",
                         "source": f"Seller classified Proofie document {key}; content not authenticated",
                         "buyer_ref": terms.get("buyer_ref", ""), "transaction_ref": capture_id})
    if isinstance(existing_tracking, dict):
        tracking_number = existing_tracking.get("tracking_number") or terms.get("tracking_number", "")
        carrier = existing_tracking.get("carrier") or terms.get("carrier", "")
    else:
        tracking_number, carrier = terms.get("tracking_number", ""), terms.get("carrier", "")
    local_conflicts = []
    local_source_error = False
    if config.feature_enabled("gap_resolution") and terms_record.get("dispute_id") == dispute_id and terms_record.get("capture_id") == capture_id:
        import sqlite3
        from copilot.db import repo
        try:
            manual = repo.get_manual_fields(config.db_path(), dispute_id)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            manual = {}
            local_source_error = True
        for name, previous in (("tracking_number", tracking_number), ("carrier", carrier)):
            row = manual.get(name)
            confirmed = row.get("value") if isinstance(row, dict) and row.get("confidence") == "read" else None
            if isinstance(confirmed, str) and confirmed:
                sources = (("shipment", existing_tracking.get(name) if isinstance(existing_tracking, dict) else None),
                           ("readiness", terms.get(name)))
                for source, other in sources:
                    if other and other != confirmed:
                        local_conflicts.append({"field": name, "seller_value": confirmed, "other_value": other,
                                                "source": source})
                if name == "tracking_number": tracking_number = confirmed
                else: carrier = confirmed
    address = transaction.get("shipping_address") if isinstance(transaction.get("shipping_address"), dict) else {}
    seller_address = terms.get("ship_to") if isinstance(terms.get("ship_to"), dict) else {}
    address_map = {"name": transaction.get("recipient_name") or seller_address.get("name", ""),
                   "street": address.get("line1") or seller_address.get("street", ""),
                   "city": address.get("city") or seller_address.get("city", ""),
                   "region": address.get("state") or seller_address.get("region", ""),
                   "postal_code": address.get("postal_code") or seller_address.get("postal_code", ""),
                   "country": address.get("country_code") or seller_address.get("country", "")}
    price, currency = terms.get("price_cents", 0), terms.get("currency", "")
    data = {**{k: terms.get(k, "") for k in ("kind", "buyer_ref", "description", "variant", "expected_date", "returns", "contact", "terms_version", "acceptance_ref")},
            "transaction_ref": capture_id, "price_cents": price, "currency": currency,
            "carrier": carrier, "tracking_number": tracking_number, "ship_to": address_map, "evidence": evidence}
    result = build(data)
    if local_conflicts:
        result["source_conflicts"] = local_conflicts
        result["source_warnings"] = ["Saved sources differ for " + item["field"].replace("_", " ") + "; the seller-entered value is selected." for item in local_conflicts]
    if local_source_error:
        result["gaps"].append("Saved case details could not be read; review before preparing tracking")
        result["tracking_payload"] = None
        result["status"] = "case details unavailable; review sources"
    attached_ids = {str(a.get("document_id")) for a in attachments if isinstance(a, dict)}
    document_rows = [{**doc, "seller_classified_kind": kinds.get(str(doc.get("id"))),
                      "attached_to_latest_package": str(doc.get("id")) in attached_ids}
                     for doc in documents if isinstance(doc, dict)]
    return {"result": result, "terms": terms, "documents": document_rows, "attachments": attachments,
            "gaps": result["gaps"], "source_label": "Provider transaction read + Proofie stored case documents + seller-supplied sale terms; not independently verified"}


def build(data):
    try:
        if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False)) > 25000:
            raise ValueError("Input must be an object under 25 KB.")
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Input"):
            raise
        raise ValueError("Input must contain JSON values.") from exc
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in REQUIRED_EVIDENCE:
        raise ValueError("Unknown sale type.")
    price, currency = data.get("price_cents"), data.get("currency")
    if type(price) is not int or price < 0 or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Price must be integer minor units and currency an uppercase three-letter code.")
    fields = ["transaction_ref", "buyer_ref", "description", "variant", "expected_date", "returns", "contact", "terms_version", "acceptance_ref"]
    if any(not isinstance(data.get(k, ""), str) for k in fields):
        raise ValueError("Sale terms and references must be text.")
    gaps = [k for k in fields if not data.get(k, "").strip()]
    try:
        date.fromisoformat(data.get("expected_date", ""))
    except (TypeError, ValueError):
        gaps.append("valid_expected_date")
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 100 or any(not isinstance(x, dict) for x in evidence):
        raise ValueError("Evidence must be a bounded list of objects.")
    ids = [x.get("id") for x in evidence]
    if any(not isinstance(x, str) or not x for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("Every evidence row needs a unique non-empty id.")
    linked, unlinked = [], []
    present = set()
    for item in evidence:
        if not all(isinstance(item.get(k, ""), str) for k in ("kind", "value", "source", "buyer_ref", "transaction_ref")):
            raise ValueError("Evidence kind, value, source and buyer/transaction references must be text.")
        if not item["value"] or not item["source"]:
            continue
        if item["buyer_ref"] != data.get("buyer_ref") or item["transaction_ref"] != data.get("transaction_ref"):
            unlinked.append(item["id"])
            continue
        linked.append(item)
        present.add(item["kind"])
    evidence_gaps = sorted(REQUIRED_EVIDENCE[kind] - present)
    post_shipment_gaps = [gap for gap in evidence_gaps if kind == "physical" and gap == "delivery"]
    tracking_payload, tracking_gaps = None, []
    if kind == "physical":
        carrier, tracking_number, address = data.get("carrier"), data.get("tracking_number"), data.get("ship_to")
        is_legacy_carrier = isinstance(carrier, str) and carrier.upper() in LEGACY_TRACKING_CARRIERS
        carrier_match = map_carrier(carrier) if isinstance(carrier, str) and carrier.strip() == carrier and not is_legacy_carrier else None
        tracking_carrier = carrier.upper() if is_legacy_carrier else (
            carrier_match.code if carrier_match and (carrier_match.code in KNOWN_CODES or carrier_match.code == "OTHER") else None)
        if tracking_carrier is None:
            tracking_gaps.append("supported Tracking carrier selection (MX catalog, UPS, USPS, FedEx, DHL, or named OTHER)")
        if not isinstance(tracking_number, str) or not tracking_number.strip() or len(tracking_number) > 100: tracking_gaps.append("tracking number")
        required_address = ("name", "street", "city", "region", "postal_code", "country")
        if not isinstance(address, dict) or any(not isinstance(address.get(k), str) or not address[k].strip() for k in required_address): tracking_gaps.append("complete ship-to address")
        else:
            if any(len(address[k]) > 200 for k in required_address) or any(any(ch in address[k] for ch in "\r\n") for k in required_address): tracking_gaps.append("safe address fields")
        if not tracking_gaps:
            tracking_payload = {"carrier": tracking_carrier,
                                "carrier_name_other": carrier_match.carrier_name_other if tracking_carrier == "OTHER" and carrier_match else None,
                                "tracking_number": tracking_number,
                                "ship_to": {k: address[k] for k in required_address},
                                "status": "prepared local Tracking payload only; no tracking POST",
                                "source_label": "Seller-supplied carrier and tracking number; local Tracking mapping only, not independently verified or provider-accepted",
                                "carrier_warning": carrier_match.note if carrier_match and carrier_match.risk else None,
                                "carrier_risk": bool(carrier_match and carrier_match.risk)}
    gaps.extend(f"evidence:{x}" for x in evidence_gaps)
    gaps.extend(f"evidence_unlinked:{x}" for x in unlinked)
    gaps.extend(f"tracking:{x}" for x in tracking_gaps)
    # Proof of delivery is a later-stage record. It remains visible as a gap,
    # but does not veto preparing the seller's local tracking payload.
    blocking_gaps = [gap for gap in gaps if gap != "evidence:delivery"]
    if blocking_gaps:
        tracking_payload = None
    status = "ready for human review" if not gaps else (
        "tracking preparation ready; proof of delivery is a later-stage record" if not blocking_gaps and post_shipment_gaps
        else "incomplete; resolve listed gaps")
    return {"journey": kind, "transaction_ref": data.get("transaction_ref"), "buyer_ref": data.get("buyer_ref"), "price_cents": price, "currency": currency, "terms": {k: data.get(k) for k in ("description", "variant", "expected_date", "returns", "contact", "terms_version", "acceptance_ref")}, "gaps": gaps, "post_shipment_gaps": post_shipment_gaps, "evidence": linked, "unlinked_evidence_ids": unlinked, "required_evidence_kinds": sorted(REQUIRED_EVIDENCE[kind]), "tracking_payload": tracking_payload, "status": status, "source_label": "User-supplied recorded data; not verified"}
