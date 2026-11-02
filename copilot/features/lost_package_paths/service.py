"""Static, source-dated paths for a missing shipment; never contacts carriers."""
from __future__ import annotations

import json
import re
from pathlib import Path

from copilot.features.carrier_directory import service as carrier_service

_PATH = Path(__file__).with_name("paths.json")
_REFERENCE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_UNKNOWN = "No separate deadline or requirement list was verified in the cited source. Confirm the purchased service contract."


def carrier_rows() -> list[dict]:
    carriers = carrier_service.carriers()
    data = json.loads(_PATH.read_text(encoding="utf-8"))
    if data.get("schema") != "proofie-lost-package-paths/1" or not isinstance(data.get("carriers"), dict):
        raise ValueError("Missing-package path data is invalid")
    rows = []
    for carrier in carriers:
        profile = data["carriers"].get(carrier["id"])
        if not isinstance(profile, dict):
            raise ValueError(f"Missing path source for {carrier['id']}")
        rows.append({**carrier, **profile})
    return rows


def paths_for(carrier_id: str, dispute_id: str) -> dict:
    carrier = next((row for row in carrier_rows() if row["id"] == carrier_id), None)
    if carrier is None:
        raise ValueError("Choose a carrier from the local directory.")
    source = {"name": carrier["source_name"], "url": carrier["source_url"],
              "checked_on": "2026-10-25"}
    paths = [
        {"key": "tracking", "label": "1. Check tracking", "who": "Seller or recipient checks the public tracking page.",
         "deadline": "No separate deadline is established by this link; check the shipment service.",
         "documents": ["Tracking/guide number"], "url": carrier["tracking_url"], "source": source},
        {"key": "search", "label": "2. Ask the carrier to search/trace", "who": carrier["search_actor"],
         "deadline": carrier["search_window"], "documents": carrier["search_documents"],
         "url": carrier["search_url"], "source": source},
        {"key": "claim", "label": "3. File a carrier claim", "who": carrier["claim_actor"],
         "deadline": carrier["claim_window"], "documents": carrier["claim_documents"],
         "url": carrier["claim_url"], "source": source},
        {"key": "insurance", "label": "4. Check purchased insurance/value coverage", "who": "The policyholder/contracting sender, if a policy or eligible service coverage was purchased.",
         "deadline": carrier["claim_window"], "documents": [carrier["insurance_note"], "Keep the policy/waybill and proof of value; the insurer may require more."],
         "url": carrier["insurance_url"], "source": source},
        {"key": "paypal", "label": "5. Continue the existing PayPal case", "who": "The seller reviews and responds in the current case; Proofie does not send from this guide.",
         "deadline": "Use the Respond by date already shown on the dispute page; only PayPal supplies that date.",
         "documents": ["Review the existing case package and source records before any separate human-approved send."],
         "url": f"/disputes/{dispute_id}", "source": {"name": "Current Proofie case page", "url": f"/disputes/{dispute_id}", "checked_on": "Current case view"}},
    ]
    return {"carrier": carrier, "paths": paths}


def validate_reference(value: str) -> str:
    value = " ".join(str(value or "").split())
    if not value or len(value) > 120 or not _REFERENCE.fullmatch(value):
        raise ValueError("Enter the carrier case/reference number, up to 120 characters, without control characters.")
    return value
