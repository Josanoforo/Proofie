"""Strict, case-free JSON exchange; free text and case identifiers are never exported."""
from __future__ import annotations

import json

from copilot import config
from copilot.features.trade_profiles.service import rules

VERSION = "proofie-seller-reminder-v1"
COUNTRIES = {"MX", "US", "unknown"}
AMOUNT_BANDS = {"unknown", "under_25", "25_to_100", "100_to_500", "over_500"}
TOPICS = {"delivery", "tracking", "packaging", "item_detail", "other"}
REMINDERS = {
    "check_delivery_window": "Check the recorded delivery window before deciding when to contact a buyer.",
    "save_tracking_receipt": "Keep a copy of the carrier receipt and tracking reference.",
    "photograph_package": "Photograph the packed parcel and its exterior label.",
    "record_item_details": "Record the item's distinguishing details before dispatch.",
    "keep_receipt": "Keep the sale and shipment receipts together.",
}
REQUIRED = {"format", "country", "amount_band", "trade_profile", "reported_topic", "preparation_reminder",
            "context_source"}


def _path():
    return config.data_dir() / "lessons_exchange" / "imported.json"


def seller_lesson_count() -> int:
    from copilot.features.sale_lesson import service as lessons
    return len(lessons.list_lessons())


def export_payload(*, country: str, amount_band: str, trade_profile: str,
                   reported_topic: str, preparation_reminder: str) -> dict:
    if not all(isinstance(value, str) for value in
               (country, amount_band, trade_profile, reported_topic, preparation_reminder)):
        raise ValueError("All reminder choices must be text values.")
    if country not in COUNTRIES or amount_band not in AMOUNT_BANDS:
        raise ValueError("Choose an allowed country and amount band.")
    if trade_profile not in rules() or reported_topic not in TOPICS or preparation_reminder not in REMINDERS:
        raise ValueError("Choose an allowed trade profile, topic, and preparation reminder.")
    return {"format": VERSION, "country": country, "amount_band": amount_band,
            "trade_profile": trade_profile, "reported_topic": reported_topic,
            "preparation_reminder": preparation_reminder,
            "context_source": "seller-selected; not an independently verified cause or policy requirement"}


def validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != REQUIRED:
        raise ValueError("This file is not a supported case-free seller reminder.")
    if payload.get("format") != VERSION:
        raise ValueError("This file uses an unsupported reminder format.")
    checked = export_payload(country=payload.get("country"), amount_band=payload.get("amount_band"),
        trade_profile=payload.get("trade_profile"), reported_topic=payload.get("reported_topic"),
        preparation_reminder=payload.get("preparation_reminder"))
    if payload.get("context_source") != checked["context_source"]:
        raise ValueError("The reminder provenance is invalid.")
    return checked


def imported_reminders() -> list[dict]:
    try:
        rows = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        try:
            result.append(validate_payload(row))
        except (TypeError, ValueError):
            continue
    return result


def import_payload(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 8192:
        raise ValueError("Choose a JSON file no larger than 8 KiB.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The reminder file must be valid UTF-8 JSON.") from exc
    checked = validate_payload(payload)
    rows = imported_reminders()
    if checked not in rows:
        if len(rows) >= 250:
            raise ValueError("The local reminder list is full.")
        rows.append(checked)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return checked
