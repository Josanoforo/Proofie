"""D-101 r2 (R2-D): the inbox can watch for a case that has just been opened, so nobody refreshes the page or pastes an id.

One read-only route. The inbox page carries the dispute ids it shows; its small script asks this route every few seconds while the
seller's "Watch for new cases" box is ticked, and reloads the inbox once when PayPal lists an id the page did not have. The seller
can untick the box at any time; nothing here writes to PayPal."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from copilot.core.disputes import SandboxClient, fetch_all_disputes
from copilot.web.routes_disputes import get_client
from copilot.web.security import validate_dispute_id

router = APIRouter()
MAX_SEEN = 500


def split_seen(raw: str) -> set[str]:
    """The ids the page already shows: comma separated, each one a valid dispute id, at most MAX_SEEN of them."""
    ids = [part for part in (raw or "").split(",") if part][:MAX_SEEN]
    return {part for part in ids if not validate_dispute_id(part)}


@router.get("/inbox/watch")
def inbox_watch(request: Request, seen: str = "", client: SandboxClient = Depends(get_client)):
    known = split_seen(seen)
    items, _pages = fetch_all_disputes(client)
    listed = [raw.get("dispute_id") for raw in items if isinstance(raw, dict) and isinstance(raw.get("dispute_id"), str)]
    response = JSONResponse({"new": [i for i in listed if i not in known], "listed": len(listed), "cut": bool(getattr(items, "cut", False)),
                             "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    response.headers["Cache-Control"] = "no-store"
    return response
