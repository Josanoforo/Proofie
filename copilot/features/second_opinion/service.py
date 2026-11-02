from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from copilot import config

TTL = timedelta(hours=24)
DISPUTE_ID = re.compile(r"^PP-[A-Z0-9-]{1,40}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,60}$")


def _root():
    return config.data_dir() / "second_opinion"


def _token_path(token: str):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError("This review link is invalid.")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return _root() / "tokens" / (digest + ".json")


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("This review link is invalid or has expired.") from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("This review link is invalid.")
    return value


def create_link(dispute_id: str, package_id: int, snapshot: dict, *, now: datetime | None = None) -> tuple[str, dict]:
    if not DISPUTE_ID.fullmatch(dispute_id or "") or type(package_id) is not int or package_id < 1:
        raise ValueError("Package reference is invalid.")
    if not isinstance(snapshot, dict) or snapshot.get("dispute_id") != dispute_id or snapshot.get("package_id") != package_id:
        raise ValueError("Package snapshot does not match this case.")
    token = secrets.token_urlsafe(32)
    start = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expiry = start + TTL
    row = {"schema": 1, "dispute_id": dispute_id, "package_id": package_id, "created_at": start.isoformat(timespec="seconds"),
           "expires_at": expiry.isoformat(timespec="seconds"), "viewed_at": None, "note_saved": False,
           "snapshot": snapshot}
    path = _token_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return token, row


def open_view(token: str, *, now: datetime | None = None) -> dict:
    path = _token_path(token)
    row = _read(path)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current >= datetime.fromisoformat(row["expires_at"]):
        raise ValueError("This review link has expired.")
    if row["viewed_at"] is not None:
        raise ValueError("This one-time review link has already been opened.")
    row["viewed_at"] = current.isoformat(timespec="seconds")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return row


def save_note(token: str, note: str, *, now: datetime | None = None) -> dict:
    path = _token_path(token)
    row = _read(path)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current >= datetime.fromisoformat(row["expires_at"]):
        raise ValueError("This review link has expired.")
    if row["viewed_at"] is None:
        raise ValueError("Open the package review before leaving a note.")
    if row["note_saved"]:
        raise ValueError("A note has already been left for this review.")
    note = str(note or "")
    if "\n" in note or "\r" in note:
        raise ValueError("Leave one line of up to 180 characters.")
    note = " ".join(note.split())
    if not note or len(note) > 180 or not note.isprintable():
        raise ValueError("Leave one line of up to 180 characters.")
    record = {"dispute_id": row["dispute_id"], "package_id": row["package_id"], "note": note,
              "created_at": current.isoformat(timespec="seconds"), "source": "second reviewer; local note only"}
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "notes.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
    row["note_saved"] = True
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return record
