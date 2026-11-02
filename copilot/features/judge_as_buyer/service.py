"""Offline guide and exact seed-capture matching; this module never contacts a provider."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

from copilot import config

CAPTURE_ID = re.compile(r"[A-Za-z0-9]{17}\Z")
GUIDE = (
    {"id": 1, "title": "Open the sandbox buyer account", "text": "Use a separate PayPal Sandbox Personal account in a browser you control. Proofie never signs in for you.", "url": "https://www.sandbox.paypal.com/"},
    {"id": 2, "title": "Open the Resolution Center", "text": "Go to Resolution Center, then choose Report a Problem.", "url": "https://www.sandbox.paypal.com/disputes/"},
    {"id": 3, "title": "Choose the seeded purchase", "text": "Select the purchase created by your sandbox seed run. The seed output identifies the sale and its capture ID."},
    {"id": 4, "title": "Choose the reason", "text": "Select that you did not receive the item. Type: I have not received the item."},
    {"id": 5, "title": "Submit the buyer report yourself", "text": "Review the sandbox form and confirm it yourself. Proofie does not open disputes or submit anything for the buyer."},
    {"id": 6, "title": "Return to the seller's local Proofie", "text": "Refresh All disputes (the capture ID is read from the setup script's state file, so there is nothing to paste). A new sandbox dispute can take a few minutes to appear."},
    {"id": 7, "title": "Review as seller", "text": "Open the matching case, review its evidence package, and use the existing human approval and send flow. Proofie reports a send only after reading PayPal's response."},
)


def capture_ids_path() -> Path:
    root = config.data_dir() / "nivel2" / "judge_as_buyer"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "seed-captures.json"
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("Seed capture record is not stored in the feature data folder.")
    return path


SEED_STATE_ENV = "COPILOT_SEED_STATE"


def seed_state_path() -> Path:
    """The setup script's state file: COPILOT_SEED_STATE when set, otherwise data/sandbox-seed/state.json in this checkout."""
    given = os.environ.get(SEED_STATE_ENV, "").strip()
    return Path(given) if given else Path(__file__).resolve().parents[3] / "data" / "sandbox-seed" / "state.json"


def load_seed_state_ids() -> list[str]:
    """The sale capture ids the setup script wrote to its state file (keys capture, capture2); [] when there is none or it is unreadable.
    A read of a local file only: nothing is pasted, nothing reaches PayPal."""
    try:
        path = seed_state_path()
        if not path.is_file() or path.stat().st_size > 65536:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    ids = [data.get(key) for key in ("capture", "capture2")]
    return list(dict.fromkeys(i for i in ids if isinstance(i, str) and CAPTURE_ID.fullmatch(i)))


def load_capture_ids() -> list[str]:
    """Ids saved by hand win; with none saved, the ids in the setup script's state file are used (no paste needed)."""
    saved = load_saved_capture_ids()
    return saved if saved else load_seed_state_ids()


def load_saved_capture_ids() -> list[str]:
    path = capture_ids_path()
    if not path.exists():
        return []
    if path.stat().st_size > 4096:
        raise ValueError("Seed capture record exceeds the size limit.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "proofie-judge-seed-captures/1":
        raise ValueError("Seed capture record has an unsupported format.")
    ids = data.get("capture_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 2 or any(not isinstance(i, str) or not CAPTURE_ID.fullmatch(i) for i in ids):
        raise ValueError("Seed capture record is invalid.")
    return ids


def save_capture_ids(raw: str) -> list[str]:
    ids = list(dict.fromkeys((raw or "").replace(",", "\n").split()))
    if not 1 <= len(ids) <= 2 or any(not CAPTURE_ID.fullmatch(i) for i in ids):
        raise ValueError("Enter one or two 17-character capture IDs copied from the seed output.")
    path = capture_ids_path()
    temporary = path.with_suffix(".tmp")
    if temporary.is_symlink():
        raise ValueError("Temporary seed record is linked; no data was changed.")
    payload = {"schema": "proofie-judge-seed-captures/1", "capture_ids": ids}
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return ids


def elapsed_seconds(opened_at: str, observed_at: str) -> int | None:
    try:
        start = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            return None
        seconds = int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds())
        return seconds if seconds >= 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def format_elapsed(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _readback_at(db_path: str | Path, dispute_id: str) -> str | None:
    path = Path(db_path).absolute()
    if not path.is_file() or path.is_symlink():
        return None
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("PRAGMA query_only=ON")
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='send_attempts'").fetchone()
        if not present:
            return None
        rows = conn.execute("SELECT readback_json FROM send_attempts WHERE dispute_id=? AND state='confirmed'",
                            (dispute_id,)).fetchall()
        readbacks = []
        for (raw,) in rows:
            try:
                value = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                continue
            stamp = value.get("read_at") if isinstance(value, dict) else None
            if isinstance(stamp, str):
                readbacks.append(stamp)
        return min(readbacks) if readbacks else None
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()


def marker_for_norm(norm: dict, *, db_path: str | Path | None = None, now: datetime | None = None) -> dict | None:
    if not isinstance(norm, dict):
        return None
    try:
        seeded = set(load_capture_ids())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    captures = norm.get("seller_transaction_ids")
    if not isinstance(captures, list) or not seeded.intersection(i for i in captures if isinstance(i, str)):
        return None
    opened = norm.get("create_time")
    if not isinstance(opened, str):
        return {"elapsed": None, "readback": False, "label": "Opened by you as buyer — seeded sandbox sale"}
    now = now or datetime.now(timezone.utc)
    readback_at = _readback_at(db_path or config.db_path(), str(norm.get("dispute_id", "")))
    observed = readback_at or now.isoformat(timespec="seconds")
    seconds = elapsed_seconds(opened, observed)
    if readback_at and seconds is not None:
        label = f"Elapsed from dispute opening to PayPal read-back: {format_elapsed(seconds)}"
    elif seconds is not None:
        label = f"Elapsed since PayPal opened this case: {format_elapsed(seconds)}; waiting for send read-back"
    else:
        label = "Elapsed time unavailable; dispute opening time could not be parsed"
    return {"elapsed": seconds, "readback": bool(readback_at), "label": label,
            "opened_by_buyer": True, "readback_at": readback_at}
