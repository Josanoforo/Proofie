"""Read-only lookup from a bundled JSON directory; never contacts carriers."""
from __future__ import annotations

import json
import re
from pathlib import Path

_PATH = Path(__file__).with_name("directory.json")


def carriers() -> list[dict]:
    data = json.loads(_PATH.read_text(encoding="utf-8"))
    if data.get("schema") != "proofie-carrier-directory/1" or not isinstance(data.get("carriers"), list):
        raise ValueError("Carrier directory data is invalid")
    return data["carriers"]


def find(query: str) -> list[dict]:
    key = re.sub(r"[^a-z0-9]", "", (query or "").casefold())
    if not key:
        return carriers()
    return [item for item in carriers() if key in re.sub(r"[^a-z0-9]", "", item["name"].casefold())
            or any(key == re.sub(r"[^a-z0-9]", "", market.casefold()) for market in item["markets"])]
