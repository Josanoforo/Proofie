"""Pilot step 3: read seller-side disputes from the PayPal sandbox and normalise them by code.

Usage (from the pilot/ folder, with the project venv):
  python step3_dispute.py list            -> GET /v1/customer/disputes, following PayPal's own `next` link
                                              across pages, normalised summary per item
  python step3_dispute.py show <id> [--out file.json]
                                          -> GET /v1/customer/disputes/{id}, normalised dispute (+ saved copy)

Read-only: this module never writes to a dispute. Amounts become integer minor units by Decimal parsing
(no floats); deadlines and time remaining are computed here, never by the LLM. Every call is appended to
evidence/step3_dispute.jsonl through SandboxClient.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from client import EVIDENCE, SANDBOX, PayPalError, SandboxClient

STATE = EVIDENCE / "step2_state.json"
# PayPal currencies without minor units (the rest use 2 decimals).
_ZERO_DECIMAL = {"JPY", "HUF", "TWD"}


def _load_mx_tz() -> tzinfo:
    """America/Mexico_City; falls back to the fixed -06:00 offset (no DST in Mexico since 2022-10-30)
    when the Windows Python has no tz database (tzdata is not installed in the project venv)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Mexico_City")
    except Exception:  # ZoneInfoNotFoundError and friends
        return timezone(timedelta(hours=-6), "America/Mexico_City (fixed -06:00)")


MX_TZ = _load_mx_tz()


def money_to_minor(money: dict | None) -> tuple[int | None, str | None]:
    """{'currency_code':'USD','value':'25.00'} -> (2500, 'USD'). None when missing; ValueError when the
    value has more decimals than the currency allows (never rounds silently)."""
    if not isinstance(money, dict) or "value" not in money or "currency_code" not in money:
        return None, None
    currency = money["currency_code"]
    exp = 0 if currency in _ZERO_DECIMAL else 2
    try:
        value = Decimal(str(money["value"]))
    except InvalidOperation as exc:
        raise ValueError(f"invalid money value {money['value']!r}") from exc
    scaled = value * (Decimal(10) ** exp)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{money['value']} has more decimals than {currency} allows")
    return int(scaled), currency


def minor_to_str(cents: int | None, currency: str | None) -> str | None:
    """Inverse of money_to_minor, integer arithmetic only: (2500,'USD') -> '25.00', (1500,'JPY') -> '1500'."""
    if cents is None or currency is None:
        return None
    if currency in _ZERO_DECIMAL:
        return str(cents)
    sign, cents = ("-" if cents < 0 else ""), abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_utc(ts: str | None) -> datetime | None:
    """ISO-8601 with an explicit offset (PayPal sends '...Z'); naive or malformed -> None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def remaining(deadline: datetime | None, now: datetime) -> dict:
    """Time left until the deadline, in whole hours/days computed here. now must be timezone-aware."""
    if deadline is None:
        return {"deadline_cdmx": None, "remaining_hours": None, "remaining_days": None,
                "remaining_text": "no deadline in response", "overdue": None}
    secs = int((deadline - now).total_seconds())
    overdue = secs < 0
    hours = abs(secs) // 3600
    text = f"{hours // 24}d {hours % 24}h"
    return {"deadline_cdmx": deadline.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M %Z"),
            "remaining_hours": -hours if overdue else hours,
            "remaining_days": -(hours // 24) if overdue else hours // 24,
            "remaining_text": ("overdue by " + text) if overdue else text,
            "overdue": overdue}


def normalize(d: dict, now: datetime, expected_capture: str | None = None) -> dict:
    """Reduce a PayPal dispute (list item or detail) to the fields the product uses."""
    cents, currency = money_to_minor(d.get("dispute_amount"))
    txs = d.get("disputed_transactions") or []
    seller_tx = [t.get("seller_transaction_id") for t in txs if t.get("seller_transaction_id")]
    buyer_tx = [t.get("buyer_transaction_id") for t in txs if t.get("buyer_transaction_id")]
    due_raw = d.get("seller_response_due_date")
    due = parse_utc(due_raw)
    # The docs say "provide-evidence" but the real API sends "provide_evidence": canonicalise to hyphens and
    # keep PayPal's own href/method so writes use the link PayPal gave, never a URL we built.
    action_links = {l["rel"].replace("_", "-"): {"href": l.get("href"), "method": l.get("method")}
                    for l in (d.get("links") or []) if l.get("rel") and l["rel"] != "self"}
    rels = sorted(action_links)
    evidences = d.get("evidences") or []
    out = {
        "dispute_id": d.get("dispute_id"),
        "reason": d.get("reason"),
        "status": d.get("status"),
        "stage": d.get("dispute_life_cycle_stage"),
        "amount_cents": cents,
        "amount_display": minor_to_str(cents, currency),
        "currency": currency,
        "seller_transaction_ids": seller_tx,
        "buyer_transaction_ids": buyer_tx,
        "capture_matches_expected": (expected_capture in seller_tx) if expected_capture and seller_tx else None,
        "create_time": d.get("create_time"),
        "update_time": d.get("update_time"),
        "seller_response_due_date": due_raw,
        "deadline_parse_ok": (due is not None) if due_raw else None,
        "available_actions": rels,
        "action_links": action_links,
        "can_provide_evidence": "provide-evidence" in action_links,
        "evidence_requested_from_seller": [e.get("evidence_type") for e in evidences
                                           if e.get("source") == "REQUESTED_FROM_SELLER"],
        "evidence_submitted_by_seller": [e.get("evidence_type") for e in evidences
                                         if e.get("source") == "SUBMITTED_BY_SELLER"],
    }
    out.update(remaining(due, now))
    return out


def _expected_capture() -> str | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("capture_id")
    except (OSError, ValueError):
        return None


def _next_page_path(body: dict) -> str | None:
    """PayPal's own `rel: next` href, as a path; never a URL we build ourselves. None past the last page."""
    link = next((l for l in (body.get("links") or []) if l.get("rel") == "next" and l.get("href")), None)
    if link is None:
        return None
    href = link["href"]
    return href[len(SANDBOX):] if href.startswith(SANDBOX) else href


def fetch_all_disputes(c: SandboxClient, max_pages: int = 20) -> tuple[list[dict], int]:
    """GET /v1/customer/disputes, following PayPal's `next` link until exhausted. max_pages is a
    local safety cap against a pathological/self-referencing link; it never silently drops a real next page
    below that cap."""
    items: list[dict] = []
    path, pages = "/v1/customer/disputes", 0
    while path is not None:
        if pages >= max_pages:
            raise RuntimeError(f"stopped after {max_pages} pages without reaching the last one "
                               f"(possible pagination loop); not silently truncating")
        body = c.call("GET", path, "list_disputes")
        items += body.get("items") or []
        pages += 1
        path = _next_page_path(body)
    return items, pages


def cmd_list(c: SandboxClient) -> int:
    items, pages = fetch_all_disputes(c)
    now, cap = datetime.now(timezone.utc), _expected_capture()
    print(f"items: {len(items)} across {pages} page(s)")
    for it in items:
        print(json.dumps(normalize(it, now, cap), ensure_ascii=False))
    return 0


def cmd_show(c: SandboxClient, dispute_id: str, out: str | None = None) -> int:
    body = c.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    text = json.dumps(normalize(body, datetime.now(timezone.utc), _expected_capture()), ensure_ascii=False, indent=1)
    print(text)
    if out:  # snapshot of the normalised dispute, the model's input in step4
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


def main(argv: list[str]) -> int:
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2:]
    if not argv or argv[0] not in ("list", "show") or (argv[0] == "show" and len(argv) != 2) or (
            "--out" in sys.argv and not out):
        print(__doc__)
        return 2
    c = SandboxClient("step3_dispute")
    try:
        return cmd_list(c) if argv[0] == "list" else cmd_show(c, argv[1], out)
    except PayPalError as e:
        print(f"PAYPAL ERROR status={e.status} issue={e.issue} debug_id={e.debug_id}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
