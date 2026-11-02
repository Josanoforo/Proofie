"""Move a SANDBOX dispute to a chosen state with PayPal's sandbox calls (via pilot/client.SandboxClient).

Usage (run with the .venv python):
  sandbox_dispute_state.py --lose <dispute_id>          POST .../adjudicate {"adjudication_outcome": "BUYER_FAVOR"}
                                                        the seller loses: RESOLVED, outcome RESOLVED_BUYER_FAVOR
  sandbox_dispute_state.py --win <dispute_id>           the same call with SELLER_FAVOR: RESOLVED_SELLER_FAVOR
  sandbox_dispute_state.py --reset-seller <dispute_id>  POST .../require-evidence {"action": "SELLER_EVIDENCE"}
                                                        PayPal asks the seller again: WAITING_FOR_SELLER_RESPONSE
  sandbox_dispute_state.py --need-buyer <dispute_id>    the same call with BUYER_EVIDENCE: WAITING_FOR_BUYER_RESPONSE
  sandbox_dispute_state.py --rearm <dispute_id>         POST .../escalate: a case waiting for the buyer (inquiry stage)
                                                        goes to CHARGEBACK / WAITING_FOR_SELLER_RESPONSE (about 3 min) with
                                                        no buyer click; escalating is a real stage change (see below)
  sandbox_dispute_state.py --plan <dispute_id>          read only: one GET, then which flag (if any) fits the case now;
                                                        allowed on any dispute id, it writes nothing
  add --dry-run to print the plan (the GET is still made) and send nothing.

PayPal's reference: adjudicate and require-evidence are "for sandbox use only"; the dispute must be UNDER_REVIEW and the
`adjudicate` / `require_evidence` link must be present in the GET. This tool reads the dispute first and refuses (no POST)
when the state or the link is missing. `--rearm` needs WAITING_FOR_BUYER_RESPONSE and the `escalate` link. Side effects of
escalate: the stage becomes CHARGEBACK and the escalation note is listed as the seller's evidence of type OTHER.

Guards, all in code: the host must be api-m.sandbox.paypal.com (the client's address is fixed; this tool checks it again and
refuses any other); the dispute id must look like PP-R-...; the cut's record dispute is refused. A GET is made before and
after the POST; the evidence lines (append-only JSONL under the evidence folder) hold both reads and the POST, and a
`state_transition` line holds the before/after status. Exit codes: 0 reached and read back, 1 refused here (nothing sent),
2 PayPal refused the call, 3 the POST was accepted but the read-back did not show the expected state in time.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pilot"))

from client import SANDBOX, PayPalError, SandboxClient  # noqa: E402

SANDBOX_HOST = "api-m.sandbox.paypal.com"
LOG_NAME = "sandbox_dispute_state"
DISPUTE_ID = re.compile(r"^PP-R-[A-Z0-9]{3}-\d{6,}$")
# The cut's record dispute: read by the automated run, never written by this tool.
PROTECTED_DISPUTES = frozenset({"PP-R-HJE-10194967"})

# Observed live (HZY, 2026-11-01): the POST answered 200 at 16:57:44, the adjudicate link vanished at 16:59:17 and the GET read
# RESOLVED at 17:01:20, about 3.5 min after the POST. The vanished link is the transition under way, not a failure.
# PayPal's read-back spells the outcome RESOLVED_BUYER_FAVOUR (with a U) while the request body says BUYER_FAVOR.
ADJUDICATE_WAIT_S = 420.0
POLL_S = 15.0

REARM_NOTE = "DEMO DATA - sandbox re-arm by escalate; no buyer action."

ADJUDICATE = {
    "lose": {"outcome": "BUYER_FAVOR", "code": "RESOLVED_BUYER_FAVOR"},
    "win": {"outcome": "SELLER_FAVOR", "code": "RESOLVED_SELLER_FAVOR"},
}


def _resolved_as(*codes: str):
    return lambda d: d.get("status") == "RESOLVED" and outcome_code(d) in codes


def _waiting(status: str, link: str | None = None):
    return lambda d: d.get("status") == status and (link is None or link in rels(d))


# One row per action: what the dispute must show before the POST, the POST, and what the read-back must show.
ACTIONS = {
    "lose": {"path": "adjudicate", "op": "adjudicate_lose", "body": {"adjudication_outcome": "BUYER_FAVOR"},
             "need_status": "UNDER_REVIEW", "need_link": "adjudicate", "wait_s": ADJUDICATE_WAIT_S,
             "done": _resolved_as("RESOLVED_BUYER_FAVOR", "RESOLVED_BUYER_FAVOUR"), "want": "RESOLVED / RESOLVED_BUYER_FAVO(U)R"},
    "win": {"path": "adjudicate", "op": "adjudicate_win", "body": {"adjudication_outcome": "SELLER_FAVOR"},
            "need_status": "UNDER_REVIEW", "need_link": "adjudicate", "wait_s": ADJUDICATE_WAIT_S,
            "done": _resolved_as("RESOLVED_SELLER_FAVOR", "RESOLVED_SELLER_FAVOUR"), "want": "RESOLVED / RESOLVED_SELLER_FAVO(U)R"},
    "reset-seller": {"path": "require-evidence", "op": "require_evidence_seller", "body": {"action": "SELLER_EVIDENCE"},
                     "need_status": "UNDER_REVIEW", "need_link": "require_evidence", "wait_s": 120.0,
                     "done": _waiting("WAITING_FOR_SELLER_RESPONSE"), "want": "WAITING_FOR_SELLER_RESPONSE"},
    "need-buyer": {"path": "require-evidence", "op": "require_evidence_buyer", "body": {"action": "BUYER_EVIDENCE"},
                   "need_status": "UNDER_REVIEW", "need_link": "require_evidence", "wait_s": 120.0,
                   "done": _waiting("WAITING_FOR_BUYER_RESPONSE"), "want": "WAITING_FOR_BUYER_RESPONSE"},
    "rearm": {"path": "escalate", "op": "escalate_rearm", "body": {"note": REARM_NOTE},
              "need_status": "WAITING_FOR_BUYER_RESPONSE", "need_link": "escalate", "wait_s": 600.0,
              "done": _waiting("WAITING_FOR_SELLER_RESPONSE", "provide_evidence"),
              "want": "WAITING_FOR_SELLER_RESPONSE with the provide_evidence link"},
}


class Refused(Exception):
    """The tool will not send; nothing was written."""


def check_host(base_url: str) -> None:
    host = (urlparse(str(base_url)).hostname or "").lower()
    if host != SANDBOX_HOST:
        raise Refused(f"host {host or '(none)'} is not {SANDBOX_HOST}; nothing sent")


def check_dispute_id(dispute_id: str, *, write: bool = True) -> None:
    if not DISPUTE_ID.match(dispute_id or ""):
        raise Refused(f"{dispute_id!r} does not look like a dispute id (PP-R-XXX-NNNNNNNN); nothing sent")
    if write and dispute_id in PROTECTED_DISPUTES:
        raise Refused(f"{dispute_id} is the cut's record dispute (read only); nothing sent")


def rels(dispute: dict) -> set[str]:
    return {link.get("rel") for link in dispute.get("links") or [] if isinstance(link, dict)}


def outcome_code(dispute: dict) -> str | None:
    outcome = dispute.get("dispute_outcome")
    return outcome.get("outcome_code") if isinstance(outcome, dict) else None


def snapshot(dispute: dict) -> dict:
    """State fields only: status, stage, outcome, link names. No payer data."""
    return {"status": dispute.get("status"), "stage": dispute.get("dispute_life_cycle_stage"),
            "outcome": outcome_code(dispute), "links": sorted(r for r in rels(dispute) if r)}


def get_dispute(client, dispute_id: str, tag: str) -> dict:
    return client.call("GET", f"/v1/customer/disputes/{dispute_id}", f"dispute_state_get_{tag}")


def perform(client, dispute_id: str, name: str, *, dry_run: bool = False, wait_s: float | None = None,
            poll_s: float = POLL_S, sleep=time.sleep) -> tuple[int, str]:
    """GET, one POST, GET until the expected state. Returns (exit code, message)."""
    plan = ACTIONS[name]
    wait_s = plan["wait_s"] if wait_s is None else wait_s
    check_host(getattr(getattr(client, "_http", None), "base_url", SANDBOX))
    check_dispute_id(dispute_id)
    before = get_dispute(client, dispute_id, "before")
    snap_before = snapshot(before)
    if before.get("status") != plan["need_status"] or plan["need_link"] not in rels(before):
        return 1, (f"NOT SENT: {plan['path']} needs status {plan['need_status']} and the {plan['need_link']} link; the "
                   f"dispute is {snap_before['status']} / {snap_before['stage']} with links {snap_before['links']}")
    if dry_run:
        return 0, f"DRY: would POST /v1/customer/disputes/{dispute_id}/{plan['path']} {plan['body']}; nothing sent"
    try:
        client.call("POST", f"/v1/customer/disputes/{dispute_id}/{plan['path']}", f"dispute_state_{plan['op']}",
                    json_body=plan["body"])
    except PayPalError as exc:
        return 2, f"PayPal refused: {exc.status} {exc.issue} (debug_id {exc.debug_id}); not retried"
    waited = 0.0
    while True:
        after = get_dispute(client, dispute_id, "after")
        if plan["done"](after):
            break
        if waited >= wait_s:
            _record(client, dispute_id, name, snap_before, snapshot(after), reached=False)
            return 3, (f"POST accepted but the read-back shows {snapshot(after)['status']} / {snapshot(after)['outcome']} "
                       f"after {int(waited)} s, expected {plan['want']}")
        sleep(poll_s)
        waited += poll_s
    snap_after = snapshot(after)
    _record(client, dispute_id, name, snap_before, snap_after, reached=True)
    return 0, (f"{dispute_id}: {snap_before['status']} / {snap_before['stage']} -> {snap_after['status']} / "
               f"{snap_after['stage']}{' (' + snap_after['outcome'] + ')' if snap_after['outcome'] else ''}, read back by GET")


def plan_for(dispute: dict) -> tuple[str | None, str]:
    """Which flag fits this case now: (flag or None, one sentence). Pure: reads the fields of one GET."""
    snap, links = snapshot(dispute), rels(dispute)
    where = f"{snap['status']} / {snap['stage']}"
    if snap["status"] == "RESOLVED":
        return None, f"{where}: closed; these calls do not reopen a resolved case, a new case needs the buyer's click"
    if snap["status"] == "WAITING_FOR_SELLER_RESPONSE" and "provide_evidence" in links:
        return None, f"{where}: PayPal already waits for the seller, nothing to reset"
    if snap["status"] == "WAITING_FOR_BUYER_RESPONSE" and "escalate" in links:
        return "--rearm", f"{where}: --rearm escalates it back to the seller (stage becomes CHARGEBACK)"
    if snap["status"] == "UNDER_REVIEW" and "require_evidence" in links:
        return "--reset-seller", f"{where}: --reset-seller asks the seller for evidence again"
    return None, f"{where}: no reset fits now (links {snap['links']})"


def plan(client, dispute_id: str) -> tuple[int, str]:
    """Read only. Allowed on the record dispute too: the protection is for writes."""
    check_host(getattr(getattr(client, "_http", None), "base_url", SANDBOX))
    check_dispute_id(dispute_id, write=False)
    flag, sentence = plan_for(get_dispute(client, dispute_id, "plan"))
    return 0, f"PLAN {dispute_id}: {sentence}" + (f" [flag {flag}]" if flag else " [no flag]")


def adjudicate(client, dispute_id: str, side: str, **kw) -> tuple[int, str]:
    """side is 'lose' or 'win'."""
    return perform(client, dispute_id, side, **kw)


def _record(client, dispute_id: str, action: str, before: dict, after: dict, *, reached: bool) -> None:
    client._log({"op": "state_transition", "dispute_id": dispute_id, "action": action, "before": before, "after": after,
                 "reached": reached})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--lose", metavar="DISPUTE_ID", help="adjudicate BUYER_FAVOR (the seller loses)")
    group.add_argument("--win", metavar="DISPUTE_ID", help="adjudicate SELLER_FAVOR (the seller wins)")
    group.add_argument("--reset-seller", metavar="DISPUTE_ID", help="require-evidence SELLER_EVIDENCE")
    group.add_argument("--need-buyer", metavar="DISPUTE_ID", help="require-evidence BUYER_EVIDENCE")
    group.add_argument("--rearm", metavar="DISPUTE_ID", help="escalate a case waiting for the buyer, so the seller can answer again")
    group.add_argument("--plan", metavar="DISPUTE_ID", help="read only: which flag fits the case now")
    ap.add_argument("--dry-run", action="store_true", help="GET and print the plan; send nothing")
    ap.add_argument("--wait", type=float, default=None, help="seconds to wait for the read-back (default: 420 adjudicate, 120 require-evidence, 600 rearm)")
    args = ap.parse_args(argv)
    name, dispute_id = next((flag.replace("_", "-"), getattr(args, flag)) for flag in
                            ("lose", "win", "reset_seller", "need_buyer", "rearm", "plan") if getattr(args, flag))
    try:
        client = SandboxClient(LOG_NAME)
        if name == "plan":
            code, message = plan(client, dispute_id)
        else:
            code, message = perform(client, dispute_id, name, dry_run=args.dry_run, wait_s=args.wait)
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
