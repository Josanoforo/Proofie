from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import base64
import hashlib
import json
import re
import secrets
import sqlite3
from pathlib import Path

from copilot import config
from copilot.db import repo
from copilot.web.security import validate_dispute_id

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
STATUSES = ("Preparing", "Shipped", "In transit", "Delivered")
TTL = timedelta(hours=72)
MAX_PHOTO_BYTES = 2 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer_page_tokens (
  token_sha256 TEXT PRIMARY KEY,
  dispute_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  buyer_action TEXT,
  view_json TEXT NOT NULL
)
"""


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Buyer link is invalid or expired.")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _db(db_path=None):
    return repo._connect(db_path or config.db_path())


def _ensure_table(conn) -> None:
    conn.execute(SCHEMA)


def create_link(dispute_id: str, view: dict, *, now: datetime | None = None, db_path=None) -> str:
    if validate_dispute_id(dispute_id):
        raise ValueError("Open a valid dispute before creating a buyer page.")
    if not isinstance(view, dict) or len(json.dumps(view, ensure_ascii=False)) > 3_000_000:
        raise ValueError("Buyer page content is invalid or too large.")
    allowed = {"description", "tracking_status", "expected_delivery", "locale", "photo_data_uri"}
    if set(view) - allowed:
        raise ValueError("Buyer page content contains unsupported fields.")
    if not isinstance(view.get("description"), str) or not 1 <= len(view["description"].strip()) <= 240:
        raise ValueError("Enter a short, non-sensitive description of the item.")
    if view.get("tracking_status") not in STATUSES:
        raise ValueError("Choose a recorded tracking status.")
    if view.get("locale") not in ("en", "es"):
        raise ValueError("Choose English or Spanish.")
    eta = view.get("expected_delivery", "")
    if eta:
        try:
            date.fromisoformat(eta)
        except (TypeError, ValueError) as exc:
            raise ValueError("Expected delivery must be a valid calendar date.") from exc
    photo = view.get("photo_data_uri")
    if photo is not None and (not isinstance(photo, str) or not re.fullmatch(
            r"data:image/(?:jpeg|png);base64,[A-Za-z0-9+/=]+", photo)):
        raise ValueError("Only an uploaded JPEG or PNG photo can be shared.")
    now = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    digest = _token_hash(token)
    conn = _db(db_path)
    try:
        _ensure_table(conn)
        conn.execute("INSERT INTO buyer_page_tokens VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                     (digest, dispute_id, _stamp(now), _stamp(now + TTL),
                      json.dumps({k: view.get(k) for k in allowed}, ensure_ascii=False, sort_keys=True)))
        conn.commit()
    finally:
        conn.close()
    return token


def read_link(token: str, *, now: datetime | None = None, db_path=None) -> dict:
    digest = _token_hash(token)
    now = now or datetime.now(timezone.utc)
    conn = _db(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute("SELECT expires_at, used_at, buyer_action, view_json FROM buyer_page_tokens WHERE token_sha256=?",
                           (digest,)).fetchone()
        if row is None:
            return {"state": "invalid"}
        expires = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        try:
            locale = json.loads(row[3]).get("locale", "en")
        except (TypeError, ValueError, AttributeError):
            locale = "en"
        if now >= expires:
            return {"state": "expired", "locale": locale}
        if row[1]:
            return {"state": "used", "action": row[2], "locale": locale}
        return {"state": "ready", "view": json.loads(row[3])}
    finally:
        conn.close()


def consume_link(token: str, action: str, *, now: datetime | None = None, db_path=None) -> dict:
    if action not in ("delivery_confirmed", "talk_requested"):
        raise ValueError("Choose a supported buyer response.")
    digest = _token_hash(token)
    now = now or datetime.now(timezone.utc)
    conn = _db(db_path)
    try:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT dispute_id, expires_at, used_at, view_json FROM buyer_page_tokens WHERE token_sha256=?",
                           (digest,)).fetchone()
        if row is None:
            conn.rollback()
            return {"state": "invalid"}
        dispute_id, expires_at, used_at, view_json = row
        try:
            locale = json.loads(view_json).get("locale", "en")
        except (TypeError, ValueError, AttributeError):
            locale = "en"
        if now >= datetime.fromisoformat(expires_at.replace("Z", "+00:00")):
            conn.rollback()
            return {"state": "expired", "locale": locale}
        if used_at:
            conn.rollback()
            return {"state": "used", "locale": locale}
        timestamp = _stamp(now)
        updated = conn.execute("UPDATE buyer_page_tokens SET used_at=?, buyer_action=? WHERE token_sha256=? AND used_at IS NULL",
                               (timestamp, action, digest)).rowcount
        if updated != 1:
            conn.rollback()
            return {"state": "used", "locale": locale}
        repo.log_action(db_path or config.db_path(), "system", "buyer_page_response", dispute_id,
                        {"buyer_action": action, "recorded_at": timestamp, "source": "one_use_buyer_link"}, conn=conn)
        conn.commit()
        return {"state": "recorded", "action": action, "recorded_at": timestamp, "locale": locale}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def buyer_events(db_path, dispute_id: str) -> list[dict]:
    if validate_dispute_id(dispute_id):
        raise ValueError("Open a valid dispute to read its buyer responses.")
    path = Path(db_path).absolute()
    if not path.is_file() or path.is_symlink():
        return []
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute("SELECT ts, detail FROM audit_log WHERE dispute_id=? AND action='buyer_page_response' ORDER BY id",
                            (dispute_id,)).fetchall()
        events = []
        for ts, raw in rows:
            try:
                detail = json.loads(raw)
            except (TypeError, ValueError):
                continue
            action = detail.get("buyer_action") if isinstance(detail, dict) else None
            if action in ("delivery_confirmed", "talk_requested"):
                events.append({"action": action, "recorded_at": ts})
        return events
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def image_data_uri(upload) -> str | None:
    if upload is None or not getattr(upload, "filename", ""):
        return None
    mime = getattr(upload, "content_type", "")
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError("Choose a JPEG or PNG packing photo.")
    content = upload.file.read(MAX_PHOTO_BYTES + 1)
    if len(content) > MAX_PHOTO_BYTES:
        raise ValueError("Packing photo must be 2 MB or smaller.")
    if not content:
        raise ValueError("Packing photo is empty.")
    valid_signature = (content.startswith(b"\xff\xd8\xff") if mime == "image/jpeg"
                       else content.startswith(b"\x89PNG\r\n\x1a\n"))
    if not valid_signature:
        raise ValueError("The uploaded file does not match its JPEG or PNG type.")
    return f"data:{mime};base64," + base64.b64encode(content).decode("ascii")
