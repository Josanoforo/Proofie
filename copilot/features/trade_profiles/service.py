"""Small, code-owned preparation rules selected by the seller."""
from __future__ import annotations

import json

from copilot import config


def rules() -> dict:
    from pathlib import Path
    return json.loads(Path(__file__).with_name("profiles.json").read_text(encoding="utf-8"))


def _path():
    return config.data_dir() / "trade_profiles" / "selection.json"


def current_profile() -> dict | None:
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    key = saved.get("profile") if isinstance(saved, dict) else None
    profiles = rules()
    return {"key": key, **profiles[key]} if key in profiles else None


def select_profile(key: str) -> dict:
    profiles = rules()
    if key not in profiles:
        raise ValueError("Choose one of the listed seller profiles.")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"profile": key}, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return {"key": key, **profiles[key]}
