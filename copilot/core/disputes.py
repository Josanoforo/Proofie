"""Dispute reading, reused from pilot/step3_dispute.py (normalize, fetch_all_disputes) and pilot/client.py
(SandboxClient) unchanged, plus the plain-language layer the UI needs:
no raw PayPal state names or code jargon in the main view, and a deadline is shown only when PayPal
actually gave one - never computed generically.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import re

import httpx

from copilot import config
from copilot.core import pool_scope

_PILOT_DIR = Path(__file__).resolve().parents[2] / "pilot"
if str(_PILOT_DIR) not in sys.path:
    sys.path.insert(0, str(_PILOT_DIR))

from client import PayPalError, SandboxClient, evidence_dir, readback_audit, write_audit  # noqa: E402
from step3_dispute import fetch_all_disputes as _pilot_fetch_all_disputes  # noqa: E402
from step3_dispute import normalize as _pilot_normalize  # noqa: E402

__all__ = ["SandboxClient", "PayPalError", "fetch_all_disputes", "normalize", "humanize_status",
          "humanize_reason", "humanize_deadline", "humanize_evidence_type", "readback_audit", "write_audit",
          "inbox_sort_key", "ScopedClient", "DisputeList", "LIST_CUT_LINE", "list_cut_line"]

LIST_PAGE_SIZE = 50          # PayPal's maximum page size for GET /v1/customer/disputes (its default is 10)
LIST_CUT_LINE = "PayPal has more disputes than Proofie read in one go: {n} are shown."


class DisputeList(list):
    """PayPal's dispute list as read. `cut` is True when the read stopped before PayPal's last page (the page cap was
    reached or a later page failed): the list holds what was read, and the caller says so in one plain line."""
    cut = False


def list_cut_line(items) -> str | None:
    return LIST_CUT_LINE.format(n=len(items)) if getattr(items, "cut", False) else None


def _next_path(body: dict) -> str | None:
    link = next((l for l in (body.get("links") or []) if l.get("rel") == "next" and l.get("href")), None)
    if link is None:
        return None
    href = link["href"]
    base = "https://api-m.sandbox.paypal.com"
    return href[len(base):] if href.startswith(base) else href


def fetch_all_disputes(client, max_pages: int = 20):
    """PayPal's dispute list, the one reader every list consumer shares (the inbox, the deadline banner, the cliff line, the watch).
    In the hosted demo it returns only the judge pool's cases and the record case (copilot/core/pool_scope.py), and with nothing
    to show it does not read PayPal at all. Outside the hosted demo it is the pilot's function, unchanged."""
    if pool_scope.active():
        # the hosted demo never lists the account: it reads its own cases one by one, by id
        items = DisputeList()
        # (the replay case is a local recording, not a PayPal dispute: it is never asked of PayPal)
        for dispute_id in sorted(pool_scope.listed_ids() - {config.REPLAY_DEFAULT_CASE}):
            try:
                body = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
            except (PayPalError, httpx.HTTPError):
                continue                      # a case PayPal does not return is simply not listed
            if isinstance(body, dict) and body.get("dispute_id") == dispute_id:
                items.append(body)
        return items, 1 if items else 0
    items, pages = DisputeList(), 0
    path = f"/v1/customer/disputes?page_size={LIST_PAGE_SIZE}"
    while path is not None:
        if pages >= max_pages:
            items.cut = True                  # never a 500: what was read is shown, with one line saying so
            break
        try:
            body = client.call("GET", path, "list_disputes")
        except (PayPalError, httpx.HTTPError):
            if not pages:
                raise                         # nothing read at all: the caller's own PayPal-error page
            items.cut = True
            break
        items += body.get("items") or []
        pages += 1
        path = _next_path(body)
    return items, pages


_DISPUTE_PATH = re.compile(r"^/v1/customer/disputes/([^/?]+)")


class ScopedClient:
    """The request's client in the hosted demo: a call about a dispute outside the pool is refused before it leaves, whatever
    route or form field carried the id. A read passes for the pool and the record case; a write (any method but GET/HEAD) only
    for the pool. The list call (no id) passes; fetch_all_disputes cuts that list."""
    def __init__(self, inner):
        self._inner = inner

    def call(self, method, path, op, **kw):
        found = _DISPUTE_PATH.match(path or "")
        if found:
            check = pool_scope.readable if str(method).upper() in ("GET", "HEAD") else pool_scope.writable
            if not check(found.group(1)):
                raise PayPalError(404, "OUT_OF_POOL", None, {"message": pool_scope.REFUSAL_MESSAGE})
        return self._inner.call(method, path, op, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# A case re-armed by tools/sandbox_dispute_state.py --rearm carries one seller entry of type OTHER: the escalate note that call
# sends. It is not a send of evidence, so with COPILOT_FEATURE_REARM_NOTE_IGNORED an entry counts as "evidence already sent" unless
# it is the seller's, of type OTHER, and its notes equal the helper's note word for word, AND one of these holds:
#   - the dispute is in the hosted demo's judge pool (COPILOT_JUDGE_POOL): PayPal's own read of the case is the receipt, because a
#     hosted session's evidence folder is ephemeral and never holds the helper's file (LINK-FIX H1); or
#   - our own evidence file holds a 200 escalate POST by the helper for that dispute (one per entry ignored).
# Anything else, including every other seller entry in a pool case, still ends the send.
REARM_NOTE = "DEMO DATA - sandbox re-arm by escalate; no buyer action."
REARM_LOG = "sandbox_dispute_state.jsonl"
REARM_OP = "dispute_state_escalate_rearm"


def rearm_posts(dispute_id) -> int:
    """How many escalate POSTs (HTTP 200) the helper made for this dispute, counted from OUR evidence file."""
    try:
        lines = (evidence_dir() / REARM_LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    found = 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("op") == REARM_OP and row.get("method") == "POST" and row.get("status") == 200
                and str(row.get("path") or "").endswith(f"/{dispute_id}/escalate")):
            found += 1
    return found


def _is_rearm_note(entry) -> bool:
    return (isinstance(entry, dict) and entry.get("source") == "SUBMITTED_BY_SELLER" and entry.get("evidence_type") == "OTHER"
            and isinstance(entry.get("notes"), str) and entry["notes"].strip() == REARM_NOTE)


def normalize(d: dict, now, expected_capture=None) -> dict:
    """pilot's normalize, plus the one exception above behind COPILOT_FEATURE_REARM_NOTE_IGNORED."""
    out = _pilot_normalize(d, now, expected_capture)
    if config.feature_enabled("rearm_note_ignored"):
        entries = [e for e in (d.get("evidences") or []) if isinstance(e, dict) and e.get("source") == "SUBMITTED_BY_SELLER"]
        if any(_is_rearm_note(e) for e in entries):
            pooled = config.hosted_demo() and d.get("dispute_id") in pool_scope.writable_ids()
            budget, kept = (len(entries) if pooled else rearm_posts(d.get("dispute_id"))), []
            for e in entries:
                if budget and _is_rearm_note(e):
                    budget -= 1
                    continue
                kept.append(e.get("evidence_type"))
            out["evidence_submitted_by_seller"] = kept
    return out


# Plain language for the vendor, never the raw PayPal status/state names. Anything
# not in this table falls back to a literal-but-honest rendering (see _humanize) rather than hiding an
# unrecognised value - a seller must never be told nothing is happening when something is.
_STATUS_TEXT = {
    "OPEN": "Just opened",
    "WAITING_FOR_SELLER_RESPONSE": "Waiting for your response",
    "WAITING_FOR_BUYER_RESPONSE": "Waiting for the buyer",
    "UNDER_REVIEW": "Under review by PayPal",
    "RESOLVED": "Resolved",
}

# The three dispute reasons this app words for the seller; pilot/step4_evidence_ai.py VALID_TYPES_BY_REASON keys on the same three.
_REASON_TEXT = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Buyer says they did not receive the item or service",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Buyer says the item does not match its description",
    "UNAUTHORISED": "Buyer says they did not authorise this payment",
}

# The only evidence_type codes PayPal's Disputes API documents (pilot/step4_evidence_ai.py's own
# VALID_TYPES_BY_REASON lists the same three) - plain language for norm["evidence_requested_from_seller"].
_EVIDENCE_TYPE_TEXT = {
    "PROOF_OF_FULFILLMENT": "proof the order was shipped or fulfilled (tracking number, carrier, delivery "
                            "proof)",
    "PROOF_OF_REFUND": "proof a refund was already issued",
    "OTHER": "other supporting evidence",
}


# the words of an evidence type inside a CHECK label ("PayPal asked for: ..."), a closed table in
# place of replace('_', ' '). The same three types as above; any other type reads UNKNOWN_EVIDENCE_TYPE_WORD, never the enum.
EVIDENCE_TYPE_WORDS = {"PROOF_OF_FULFILLMENT": "Proof of fulfillment", "PROOF_OF_REFUND": "Proof of refund",
                       "OTHER": "Other"}
UNKNOWN_EVIDENCE_TYPE_WORD = "An evidence type Proofie does not name"


def evidence_type_words(raw) -> str:
    return EVIDENCE_TYPE_WORDS.get(raw, UNKNOWN_EVIDENCE_TYPE_WORD) if isinstance(raw, str) else UNKNOWN_EVIDENCE_TYPE_WORD


def _humanize(raw: str | None, table: dict, unknown_label: str) -> str:
    """Shared fallback: an unrecognised value still gets a readable sentence (not the enum name
    verbatim), flagged for a human to notice a new PayPal value needs its own table entry."""
    if not raw:
        return unknown_label
    return table.get(raw, f"{unknown_label.replace(' unknown', '')} not recognised: {raw}")


def humanize_status(raw_status: str | None) -> str:
    return _humanize(raw_status, _STATUS_TEXT, "Status unknown")


def humanize_reason(raw_reason: str | None) -> str:
    return _humanize(raw_reason, _REASON_TEXT, "Reason unknown")


def humanize_evidence_type(raw_evidence_type: str | None) -> str:
    return _humanize(raw_evidence_type, _EVIDENCE_TYPE_TEXT, "Evidence type unknown")


def humanize_deadline(norm: dict) -> str:
    """A deadline is shown ONLY if PayPal's own response carried one
    (normalize()'s `deadline_cdmx`/`remaining_text`, already computed by code, never by the model). No
    generic fallback is ever invented here."""
    if not norm.get("seller_response_due_date") or norm.get("deadline_cdmx") is None:
        return "No verified deadline available"
    if norm.get("overdue"):
        return f"Overdue ({norm['remaining_text']}) - as of PayPal {norm['deadline_cdmx']}"
    return f"{norm['remaining_text']} remaining - due {norm['deadline_cdmx']} (PayPal)"


def inbox_sort_key(norm: dict, raw: dict | None = None) -> tuple:
    """Order for the inbox and the PayPal due-dates page (display only; nothing decides or sends from it).
    Bucket 0: waiting for the seller and not closed by the buyer. Bucket 1: every other open case. Bucket 2: resolved or
    closed by the buyer. Inside a bucket the soonest due date first - only a due date PayPal gave that parses (a case
    without one sorts after the dated ones) - then the dispute id so the order never flickers."""
    outcome = raw.get("dispute_outcome") if isinstance(raw, dict) else None
    # the same reading as copilot/web/dispute_state.buyer_closed_raw (not imported: core never imports the web layer)
    closed = (config.feature_enabled("outcome") and isinstance(outcome, dict)
              and outcome.get("outcome_code") == "CANCELED_BY_BUYER")
    status = norm.get("status")
    if closed or status == "RESOLVED":
        bucket = 2
    elif status == "WAITING_FOR_SELLER_RESPONSE":
        bucket = 0
    else:
        bucket = 1
    due = datetime.max.replace(tzinfo=timezone.utc)
    if norm.get("deadline_parse_ok") and norm.get("seller_response_due_date"):
        try:
            parsed = datetime.fromisoformat(str(norm["seller_response_due_date"]))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            due = parsed
    return (bucket, due, norm.get("dispute_id") or "")
