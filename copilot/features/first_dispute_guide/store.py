"""Separate local display preference; this is not the case ledger or audit history."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone

from copilot import config


def _path():
    return Path(config.db_path()).with_name("first_dispute_guide_seen.json")


def seen() -> bool:
    path = _path()
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and isinstance(value.get("seen_at"), str) and bool(value["seen_at"])


def mark_seen() -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    payload = {"seen_at": datetime.now(timezone.utc).isoformat(), "scope": "single local Proofie account display preference"}
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)
    except FileExistsError:
        try:
            temp.unlink()
        except OSError:
            pass
