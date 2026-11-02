"""The inbox as the seller reads it: one next step per dispute, one summary line, the
order the cases appear in.

DISPLAY ONLY. Nothing here writes, calls PayPal or decides whether anything may be sent. It reads the stored draft rows
(`auto_prep` and the claims of the prepared package) and PayPal's own dispute fields that the caller already fetched.
The inbox never offers a send: every button is a link to a page where the seller reviews and approves.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from copilot import config
from copilot.core import send_service
from copilot.core.disputes import humanize_reason, inbox_sort_key
from copilot.core.auto_prep import PREPARED_REASON
from copilot.db import repo
from copilot.web import next_step as next_step_rules
from copilot.web import status, timefmt
from copilot.core.disputes import normalize

NO_DEADLINE = "No verified deadline available"
NO_RESPONSE_DUE = "No response due from you"
SORT_CAPTION = "Disputes waiting for you come first, soonest due date first."
# Shown once under the summary (never per row). No computed ten-day date: only the due date PayPal gives per case.
INACTION_BEFORE = ("If you do nothing, Proofie sends nothing. If the buyer escalates the case without your response, "
                   "PayPal's help says PayPal resolves it in the buyer's favor (")
INACTION_LINK = "PayPal Help HELP1083"
INACTION_AFTER = "). The “Respond by” date is the one PayPal gives for this case."
HELP1083_URL = "https://www.paypal.com/us/cshelp/article/HELP1083"
NOT_RECEIVED_ONLY_LINE = "Proofie prepares proof of shipment only. Check whether that answers this case."
AI_LINE = "Some statements were written by AI (local model)."

# Hosted demo only: what a visitor can still do with a case. The same two rules as the dispute page
# (next_step.seller_evidence_listed, send_service.provide_evidence_offered), read from the case's own detail because
# PayPal's list carries neither the evidences nor the action links.
WALK_OPEN = "Available for a full walk."
WALK_ANSWERED = "Already answered by a visitor."
WALK_CLOSED = "PayPal is not asking for evidence on this dispute now."     # the dispute page's own sentence (next_step.py)
WALK_READS_MAX = 25                    # detail reads per inbox load; further cases are left unmarked
WALK_TTL_SECONDS = 120                 # a read is reused for this long while the case's update_time is unchanged
_WALK_CACHE: dict = {}
_WALK_LOCK = threading.Lock()


def clear_walk_cache() -> None:
    with _WALK_LOCK:
        _WALK_CACHE.clear()


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _has_ai_claims(db_path, prep: dict | None) -> bool:
    """True when the prepared package holds a statement the local model wrote (basis model_interpretation). Read only."""
    if not prep or prep.get("package_id") is None:
        return False
    try:
        return any(c.get("basis") == "model_interpretation" for c in repo.list_claims(db_path, prep["package_id"]))
    except Exception:  # noqa: BLE001 - a display hint only; the review page shows each statement's basis anyway
        return False


def _next_step(norm: dict, view: "status.StatusView", prep: dict | None, ai: bool) -> dict:
    """{'label', 'href', 'lines', 'text'}: a button (label + href) or plain words, plus progress lines under it."""
    did = norm["dispute_id"]
    step = {"label": None, "href": None, "lines": [], "text": None}
    if view.code == "settling":
        step["text"] = "PayPal is updating this dispute. Check again in a minute."
    elif view.code == "claim_under_review":
        step["text"] = "Nothing to do now. PayPal is reviewing this claim."
    elif view.code != "waiting_for_you":
        step["text"] = (view.label if view.code == "unknown" else f"Nothing to do now: PayPal shows {view.label}.")
    elif prep is None:
        step.update(label="Respond to this dispute", href=f"/disputes/{did}")
    elif prep.get("status") == "preparing":
        step["text"] = "Preparing a draft. Reload this page to see it."
    elif prep.get("status") == "needs_documents":
        step.update(label="Upload documents", href=f"/disputes/{did}/workspace/prepare#documents")
        step["lines"].append("Draft: needs documents")
    elif prep.get("status") == "ready" and prep.get("has_claims"):
        step.update(label="Review the draft", href=f"/disputes/{did}/packages/{prep['package_id']}")
        step["lines"].append("Draft prepared. Check each statement.")
        if ai:
            step["lines"].append(AI_LINE)
    elif prep.get("status") == "ready":
        step.update(label="Add the details", href=f"/disputes/{did}/packages/{prep['package_id']}")
        step["lines"].append("Draft started. Details are missing.")
    else:
        step.update(label="Start the response", href=f"/disputes/{did}/workspace/prepare#package")
        step["lines"].append("Could not prepare automatically. Start the response yourself.")
    if view.code == "waiting_for_you" and norm.get("reason") != PREPARED_REASON:
        step["lines"].append(NOT_RECEIVED_ONLY_LINE)
    return step


def _counts_sentence(rows: list) -> str:
    closed = sum(1 for r in rows if r["status"].code in ("resolved", "buyer_closed"))
    with_others = len(rows) - closed - sum(1 for r in rows if r["waiting"])
    parts = []
    if with_others:
        parts.append(f"{with_others} {_plural(with_others, 'case is', 'cases are')} with PayPal or the buyer")
    if closed:
        parts.append(f"{closed} closed")
    return "Nothing needs your response." + (" " + "; ".join(parts) + "." if parts else "")


def _summary(rows: list) -> dict:
    needs = [r for r in rows if r["waiting"]]
    if not needs:
        text = _counts_sentence(rows) if rows else "Nothing needs your response at the last PayPal read."
        return {"text": text, "needs": 0}
    first_due = next((r["due"] for r in needs if r["due"]), None)
    head = f"{len(needs)} {_plural(len(needs), 'dispute needs', 'disputes need')} your response."
    if first_due:
        lowered = first_due["remaining"][:1].lower() + first_due["remaining"][1:]
        return {"text": f"{head} Soonest: respond by {first_due['text'][len('Respond by '):]} ({lowered}), "
                        "at the last PayPal read.", "needs": len(needs)}
    return {"text": f"{head} {NO_DEADLINE}, at the last PayPal read.", "needs": len(needs)}


def _walk_of(norm: dict, reader, now: datetime, budget: list) -> dict | None:
    """The mark of one case, or None when it can not be read (never a guess). One GET, kept per (id, update_time)."""
    key = (norm.get("dispute_id"), norm.get("update_time") or "")
    with _WALK_LOCK:
        hit = _WALK_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < WALK_TTL_SECONDS:
        return hit[1]
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    try:
        raw = reader.call("GET", f"/v1/customer/disputes/{key[0]}", "show_dispute")
        detail = normalize(raw, now)
        if detail.get("dispute_id") != key[0]:
            return None
    except Exception:  # noqa: BLE001 - a display hint only; the dispute page reads the case itself
        return None
    # The dispute page's own order (next_step.for_dispute): no provide-evidence action -> "not asking" first, then
    # evidence listed from the seller -> "already has evidence from you", otherwise the case can be walked.
    if not send_service.provide_evidence_offered(detail):
        walk = {"state": "closed", "text": WALK_CLOSED}
    elif next_step_rules.seller_evidence_listed(detail):
        walk = {"state": "answered", "text": WALK_ANSWERED}
    else:
        walk = {"state": "open", "text": WALK_OPEN}
    with _WALK_LOCK:
        if len(_WALK_CACHE) >= 500:
            _WALK_CACHE.clear()
        _WALK_CACHE[key] = (time.monotonic(), walk)
    return walk


def build(rows: list, db_path, now: datetime, urgency=None, detail_reader=None) -> dict:
    """rows: dicts with 'norm', 'raw' (PayPal's dispute, for the buyer-closed reading) and 'auto_prep' (the stored draft
    row with 'has_claims', or None). `urgency` is the optional callable of the deadlines feature (norm, now) -> tier; the
    caller passes it only when that feature is on. Returns {'rows', 'summary', 'needs_count'}; the rows are sorted
    (core.disputes.inbox_sort_key) and the first row that needs the seller and has a button gets the one primary.
    `detail_reader` (hosted demo only) is the request's client: with it each row gets a `walk` mark read from that case's
    own detail; without it, or outside the hosted demo, `walk` is None and nothing is read."""
    view_rows = []
    marking = detail_reader is not None and config.hosted_demo()
    budget = [WALK_READS_MAX]
    for item in rows:
        norm, raw, prep = item["norm"], item.get("raw"), item.get("auto_prep")
        view = status.dispute_status(norm, raw)
        waiting = view.needs_seller
        due = timefmt.due_display(norm, now) if waiting else None
        step = _next_step(norm, view, prep, _has_ai_claims(db_path, prep) if waiting else False)
        view_rows.append({
            "norm": norm, "raw": raw, "status": view, "waiting": waiting, "auto_prep": prep,
            "reason_text": humanize_reason(norm.get("reason")), "due": due,
            "due_note": None if waiting else NO_RESPONSE_DUE,
            "urgency": urgency(norm, now) if (urgency and waiting) else None,
            "next_step": step, "primary": False,
            "walk": _walk_of(norm, detail_reader, now, budget) if marking else None,
        })
    view_rows.sort(key=lambda r: inbox_sort_key(r["norm"], r["raw"]))
    first = next((r for r in view_rows if r["waiting"] and r["next_step"]["label"]), None)
    if first:
        first["primary"] = True
    summary = _summary(view_rows)
    return {"rows": view_rows, "summary": summary, "needs_count": summary["needs"]}
