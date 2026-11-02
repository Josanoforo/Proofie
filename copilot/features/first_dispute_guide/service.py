from datetime import datetime, timezone


def _date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_guide(country: str, norm: dict, *, outcome_code: str | None = None) -> dict:
    country = country.upper() if isinstance(country, str) else ""
    policy_label = {"MX": "Mexico", "US": "United States"}.get(country, "not identified")
    stage = norm.get("stage")
    stage_words = {"INQUIRY": "inquiry", "CHARGEBACK": "claim", "PRE_ARBITRATION": "pre-arbitration"}
    after_adverse = None
    if norm.get("status") == "RESOLVED" and outcome_code == "RESOLVED_BUYER_FAVOUR":
        if policy_label == "Mexico":
            paths = {"authority_name": "PROFECO: how to submit a consumer complaint",
                     "authority_url": "https://www.gob.mx/profeco/documentos/tuviste-algun-problema-de-consumo",
                     "authority_scope": "Concilianet/Conciliaexprés applies only to participating providers and eligible consumer relationships; otherwise check the appropriate in-person office/process.",
                     "court_name": "", "court_url": ""}
        elif policy_label == "United States":
            paths = {"authority_name": "USAGov: state consumer-protection offices",
                     "authority_url": "https://www.usa.gov/state-consumer",
                     "authority_scope": "The state office determines its scope and complaint process.",
                     "court_name": "USAGov: federal, state, territory, county and municipal courts",
                     "court_url": "https://www.usa.gov/courts"}
        else:
            paths = None
        if paths:
            after_adverse = {
                "checked_on": "2026-10-25",
                "note": "PayPal's current case read states that it was resolved in the buyer's favor. Proofie stops here; it does not appeal, file a carrier/insurance claim, start a court case or contact an authority.",
                "carrier_note": "For a carrier claim or purchased insurance, confirm the shipment's service, contract, carrier, insurer, eligibility and deadlines directly; this guide cannot determine them.",
                "authority_note": "Local consumer-protection and court procedures have their own eligibility and jurisdiction rules. These official links are starting points, not legal advice or a determination that a claim is available.",
                "paths": paths,
            }
    return {"policy_country": policy_label, "stage": stage_words.get(stage, "current stage not available"),
            "status": norm.get("status") or "not available", "created_at": _date(norm.get("create_time")),
            "updated_at": _date(norm.get("update_time")),
            "deadline": _date(norm.get("seller_response_due_date")),
            "country_note": f"The {policy_label} policy selection is the local seller-account setting. The case stage and any response deadline below come from this PayPal case read.",
            "seller_role": "Review the current PayPal prompt and the case record. Proofie does not submit a response from this guide.",
            "paypal_role": "PayPal decides the case and controls its next stage. A date is shown only when PayPal supplied a readable date.",
            "after_adverse_decision": after_adverse}
