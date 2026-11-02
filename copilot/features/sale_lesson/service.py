"""Append-only seller lesson notes; PayPal outcome is recorded separately from seller interpretation."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from copilot import config

DISPUTE_ID = re.compile(r"^PP-[A-Z0-9-]{1,40}$")
CAPTURE_ID = re.compile(r"^[A-Za-z0-9]{17}$")
KINDS = {"physical", "digital", "service"}
OUTCOMES = {"RESOLVED_BUYER_FAVOUR", "RESOLVED_SELLER_FAVOUR", "RESOLVED_WITH_PAYOUT",
            "CANCELED_BY_BUYER", "ACCEPTED", "DENIED", "OTHER"}


def _path():
    return config.data_dir() / "sale_lesson" / "lessons.jsonl"


def list_lessons(kind: str | None = None) -> list[dict]:
    if kind is not None and kind not in KINDS:
        raise ValueError("Unknown sale type.")
    path = _path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = [json.loads(line) for line in lines if line.strip()]
    return [row for row in rows if kind is None or row.get("sale_type") == kind]


def lessons_for_preparation(kind: str) -> list[dict]:
    """Read seller-authored reminders for a new sale of this type; never imply a causal finding."""
    return [{"text": row["seller_lesson"], "sale_type": row["sale_type"],
             "source_dispute_id": row["dispute_id"], "source_capture_id": row["capture_id"],
             "recorded_outcome": row["paypal_outcome_code"], "created_at": row["created_at"],
             "attribution": "Seller interpretation; not a PayPal-confirmed cause"}
            for row in list_lessons(kind)]


def record_lesson(dispute_id: str, capture_id: str, sale_type: str, outcome_code: str, lesson: str,
                  *, now: datetime | None = None) -> dict:
    if not DISPUTE_ID.fullmatch(dispute_id or "") or not CAPTURE_ID.fullmatch(capture_id or ""):
        raise ValueError("Case or sale reference is invalid.")
    if sale_type not in KINDS:
        raise ValueError("Choose a sale type.")
    if outcome_code not in OUTCOMES:
        raise ValueError("A closed PayPal-recorded outcome is required.")
    lesson = str(lesson or "")
    if "\n" in lesson or "\r" in lesson:
        raise ValueError("Enter one seller-authored lesson of 1 to 180 printable characters.")
    lesson = " ".join(lesson.split())
    if not lesson or len(lesson) > 180 or not lesson.isprintable():
        raise ValueError("Enter one seller-authored lesson of 1 to 180 printable characters.")
    rows = list_lessons()
    if any(row.get("dispute_id") == dispute_id for row in rows):
        raise ValueError("A lesson is already recorded for this case.")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    row = {"dispute_id": dispute_id, "capture_id": capture_id, "sale_type": sale_type,
           "paypal_outcome_code": outcome_code, "seller_lesson": lesson, "created_at": stamp,
           "lesson_source": "seller-entered", "outcome_source": "PayPal dispute GET"}
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
    return row
