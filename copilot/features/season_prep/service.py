"""Local, date-bounded seasonal prep data. No carrier or provider requests."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from copilot.features.carrier_directory import service as carrier_service

_PATH = Path(__file__).with_name("seasons.json")


def _data() -> dict:
    data = json.loads(_PATH.read_text(encoding="utf-8"))
    if data.get("schema") != "proofie-season-prep/1" or not isinstance(data.get("countries"), dict):
        raise ValueError("Season preparation data is invalid")
    return data


def for_country(country: str, today: date | None = None, horizon_days: int = 90) -> dict:
    """Return active/upcoming windows without inferring carrier delays or PayPal dates."""
    data = _data()
    code = (country or "").strip().upper()
    today = today or date.today()
    if code not in ("MX", "US"):
        return {"country": code, "version": data["version"], "seasons": [], "carriers": [],
                "available": False}
    horizon = today + timedelta(days=max(0, min(horizon_days, 366)))
    seasons = []
    for item in data["countries"].get(code, []):
        start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        if end < today or start > horizon:
            continue
        seasons.append({**item, "state": "active" if start <= today <= end else "upcoming"})
    carriers = [item for item in carrier_service.carriers() if code in item.get("markets", [])]
    return {"country": code, "version": data["version"], "seasons": seasons,
            "carriers": carriers, "available": True}
