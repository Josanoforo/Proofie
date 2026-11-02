"""The hosted demo shows the judge pool, plus the record case read-only, and nothing else of the PayPal sandbox account (D-114 item 5).

The account behind the demo holds the operator's own dispute history. In the hosted demo every list of disputes, every count made
from a list, and every page reached by a dispute id is bound to these sets:

  writable  the operator's allowlist COPILOT_JUDGE_POOL, while the judge-pool flag is on: the cases a judge can work on;
  record    the cut's record dispute PP-R-HJE-10194967 (strategy: "stays visible to judges as the read-only record case"): listed
            while judge mode is on with a pool set, openable by URL, never written;
  replay    the replay case, while the replay flag is on.

Anything else is refused by URL (404) and left out of every list. The local app (COPILOT_HOSTED_DEMO off) is not touched by any
function here.

This module imports no feature package. It keeps its own copy of the allowlist reading and of the record-dispute list;
tests/features/judge_pool/test_pool_inbox.py compares it with copilot.features.judge_pool.service so the two cannot drift."""
from __future__ import annotations

import os
import re

from copilot import config

POOL_ID = re.compile(r"^PP-R-[A-Z0-9]{3}-\d{6,}$")
# The cut's record dispute: read by the automated run, shown to judges read-only (same set as judge_pool.service.RECORD_DISPUTES).
RECORD_DISPUTES = frozenset({"PP-R-HJE-10194967"})

REFUSAL_TITLE = "This dispute is not part of this demo"
REFUSAL_MESSAGE = ("This case is not part of this demo, so it cannot be opened here. The demo shows only the cases its host chose "
                   "for judges. Nothing was sent to PayPal, and nothing was read from it.")
RECORD_TITLE = "This is the record case: it is read-only"
RECORD_MESSAGE = ("This case is the record case of the demo. You can read it, but nothing can be prepared, sent or changed on it. "
                  "Nothing was sent to PayPal.")
RECORD_NOTICE = ("Record case: read-only. It is shown as an example of a real sandbox dispute; nothing can be prepared, sent or "
                 "changed here.")
NO_POOL_REASON = ("The host has not set a judge pool for this demo, so no case is listed. The demo never lists the other disputes "
                  "of its PayPal sandbox account.")
POOL_EMPTY_REASON = ("This demo lists only the cases of its judge pool, and none of them is in the list PayPal returned just now. "
                     "The demo never lists the other disputes of its PayPal sandbox account.")


def active() -> bool:
    return config.hosted_demo()


def pool_ids() -> list[str]:
    seen, out = set(), []
    for item in os.environ.get("COPILOT_JUDGE_POOL", "").split(","):
        item = item.strip()
        if POOL_ID.match(item) and item not in RECORD_DISPUTES and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def pool_configured() -> bool:
    return bool(pool_ids()) and config.feature_enabled("judge_pool")


def writable_ids() -> frozenset[str]:
    """The cases a hosted visitor can work on: the pool, while judge mode is on."""
    return frozenset(pool_ids()) if config.feature_enabled("judge_pool") else frozenset()


def listed_ids() -> frozenset[str]:
    """The ids the hosted inbox may list. Empty when judge mode is off or the pool is unset (and replay is off)."""
    ids = set(writable_ids())
    if pool_configured():
        ids |= RECORD_DISPUTES
    if config.replay_enabled():
        ids.add(config.REPLAY_DEFAULT_CASE)
    return frozenset(ids)


def is_record(dispute_id: str | None) -> bool:
    return dispute_id in RECORD_DISPUTES


def readable(dispute_id: str | None) -> bool:
    """May a hosted visitor open this id by URL (a read)? The record case always; the rest only what the inbox may list."""
    return (not active()) or is_record(dispute_id) or dispute_id in listed_ids()


def writable(dispute_id: str | None) -> bool:
    """May anything be prepared, sent or changed on this id? Outside the hosted demo, always."""
    return (not active()) or dispute_id in writable_ids()


def filter_items(items: list[dict]) -> list[dict]:
    """PayPal's list, cut to the ids the hosted inbox may list. Unchanged outside the hosted demo."""
    if not active():
        return items
    listed = listed_ids()
    return [item for item in items if item.get("dispute_id") in listed]
