from copilot.features.carrier_directory.service import carriers


def selected_physical_lessons(available: list[dict], selected_ids: list[str] | None) -> list[dict]:
    """Return only seller-selected, current physical reminders, retaining their attribution and source IDs."""
    selected_ids = selected_ids or []
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("Choose each seller lesson once.")
    by_id = {}
    for row in available:
        if row.get("sale_type") != "physical":
            continue
        case_id = row.get("source_dispute_id")
        capture_id = row.get("source_capture_id")
        if not isinstance(case_id, str) or not case_id or not isinstance(capture_id, str) or not capture_id:
            continue
        by_id[f"{case_id}:{capture_id}"] = row
    if any(value not in by_id for value in selected_ids):
        raise ValueError("A selected seller lesson is unavailable. Review the current suggestions.")
    return [{"text": by_id[value]["text"], "source_dispute_id": by_id[value]["source_dispute_id"],
             "source_capture_id": by_id[value]["source_capture_id"],
             "recorded_outcome": by_id[value]["recorded_outcome"],
             "created_at": by_id[value]["created_at"],
             "attribution": by_id[value]["attribution"]} for value in selected_ids]


def build_insert(*, tracking_number: str, carrier: str, item_description: str, seller_contact: str,
                 selected_lessons: list[dict] | None = None) -> dict:
    if not isinstance(tracking_number, str) or not 1 <= len(tracking_number.strip()) <= 64:
        raise ValueError("A saved tracking number is required.")
    if not isinstance(item_description, str) or not 1 <= len(item_description.strip()) <= 120:
        raise ValueError("Enter a short item description.")
    if not isinstance(seller_contact, str) or not 1 <= len(seller_contact.strip()) <= 100:
        raise ValueError("Enter a seller contact method for the package insert.")
    carrier_row = next((row for row in carriers() if carrier in (row.get("id"), row.get("name"))), None)
    tracking_url = carrier_row.get("tracking_url") if carrier_row else None
    carrier_name = carrier_row.get("name") if carrier_row else carrier
    return {"tracking_number": tracking_number.strip(), "carrier": carrier_name,
            "tracking_url": tracking_url, "item_description": " ".join(item_description.split()),
            "seller_contact": " ".join(seller_contact.split()),
            "seller_lessons": selected_lessons or [],
            "en": "If anything is wrong with your order, please contact me before opening a dispute. ",
            "es": "Si hay algún problema con tu pedido, por favor contáctame antes de abrir una disputa. "}
