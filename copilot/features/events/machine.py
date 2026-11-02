"""Pure decisions over verified GET versions, never an event payload or a write."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def dispute_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"PP-[A-Z0-9-]{1,80}", value))


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    dispute_id: str
    version: str
    status: str
    due: str | None
    body_hash: str
    source: str
    checked_at: str
    review_version: str | None = None
    appeal_until: str | None = None


def snapshot(body, source, now, expected_id=None):
    """Called only with the detail GET result; stores no text or response body."""
    did, version, status = body.get("dispute_id"), instant(body.get("update_time")), body.get("status")
    if not dispute_id(did) or (expected_id is not None and did != expected_id) or version is None:
        raise ValueError("detail GET needs matching dispute ID and aware update_time")
    if not isinstance(status, str) or not re.fullmatch(r"[A-Z_]{1,80}", status):
        raise ValueError("detail GET needs a status token")
    v = version.isoformat()
    due = instant(body.get("seller_response_due_date"))
    # A known appeal deadline must be supplied explicitly by the verified response, never estimated.
    appeal = instant(body.get("appeal_response_due_date"))
    return Snapshot(did, v, status, due.isoformat() if due else None, digest(body), source,
                    now.isoformat(), v if status == "UNDER_REVIEW" else None, appeal.isoformat() if appeal else None)


def choose(current, observed):
    if current is None:
        return observed, "accepted"
    old, new = instant(current.version), instant(observed.version)
    if new < old:
        return current, "stale"
    if new == old:
        return current, "duplicate" if current.body_hash == observed.body_hash else "conflict"
    return observed, "accepted"  # no status rank; this also ends the previous review window


def unknown_readback(unknown, observed):
    """A GET records observation for a human; unchanged/older versions cannot prove failure."""
    if instant(observed.version) <= instant(unknown["baseline_version"]):
        return "not_confirmed"
    if observed.status == unknown["expected_status"]:
        return "observed_match"
    return "not_confirmed"


def eligible(rows, now):
    maximum = datetime.max.replace(tzinfo=timezone.utc)
    return sorted((r for r in rows if r.status != "RESOLVED" or
                   (instant(r.appeal_until) is not None and instant(r.appeal_until) > now)),
                  key=lambda r: (instant(r.due) or maximum, r.dispute_id))
