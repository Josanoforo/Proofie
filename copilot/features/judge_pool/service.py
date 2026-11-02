"""Judge mode on the hosted demo: a pool of open sandbox disputes, one case per judge session, one re-arm per case.

The module's ONLY PayPal write is PayPal's `escalate` action, sent to the href of the link PayPal gave in a fresh read of the
case (never to a URL built here), on the sandbox host, for a case in the operator's allowlist COPILOT_JUDGE_POOL. There is no
code in this module for adjudicate, require-evidence, win or lose, and no route that reaches them (tests enforce both).
A case is offered only when PayPal's own read of it says so; nothing here is a guess kept from an earlier read."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from urllib.parse import urlparse

from copilot import config
from copilot.core import disputes, write_guard

SANDBOX_HOST = "api-m.sandbox.paypal.com"
LOG_NAME = disputes.REARM_LOG[: -len(".jsonl")]          # the helper's log name: the re-arm note exception reads this file
OP = disputes.REARM_OP                                      # the helper's op name, so the exception recognises a hosted re-arm
NOTE = disputes.REARM_NOTE
ACTION = "judge_rearm"
# The cut's record dispute is read by the automated run: refused here even when an operator lists it.
RECORD_DISPUTES = frozenset({"PP-R-HJE-10194967"})
DISPUTE_ID = re.compile(r"^PP-R-[A-Z0-9]{3}-\d{6,}$")

LOCK_TTL_SECONDS = 2 * 60 * 60
START_GAP_SECONDS = 10 * 60
STARTS_PER_SESSION_PER_DAY = 3
STARTS_PER_DAY_GLOBAL = 20
DAY = 24 * 60 * 60

# States of a pool case, as the judge reads them.
START, ANSWER, TAKEN, USED, UNAVAILABLE = "start", "answer", "taken", "used", "unavailable"
LABELS = {START: "Ready to start", ANSWER: "Ready to answer", TAKEN: "Taken by another visitor",
          USED: "Used", UNAVAILABLE: "Not available now"}

_DDL = """
CREATE TABLE IF NOT EXISTS judge_case_locks (
    dispute_id TEXT PRIMARY KEY, session TEXT NOT NULL, taken_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS judge_starts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session TEXT NOT NULL, dispute_id TEXT NOT NULL);
"""


class Refused(Exception):
    """Nothing was sent. The message is the sentence the judge reads."""


# ---------------------------------------------------------------------------------------------------------- the pool
def pool_ids() -> list[str]:
    """The operator's allowlist, in order, de-duplicated, only well-formed ids, never a record dispute."""
    seen, out = set(), []
    for item in os.environ.get("COPILOT_JUDGE_POOL", "").split(","):
        item = item.strip()
        if DISPUTE_ID.match(item) and item not in RECORD_DISPUTES and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def enabled() -> bool:
    """Judge mode exists only in the hosted demo, with the flag on and a non-empty allowlist."""
    return config.hosted_demo() and config.feature_enabled("judge_pool") and bool(pool_ids())


def session_hash(cookie: str) -> str:
    """What the evidence file and the lock table keep of a session: a prefix of a hash, never the cookie."""
    return hashlib.sha256(("proofie-judge:" + cookie).encode("utf-8")).hexdigest()[:12]


def case_state(norm: dict, *, holder: str | None = None, session: str | None = None) -> str:
    """One case, from PayPal's read (normalised with the re-arm note exception) and the lock holder."""
    if norm.get("dispute_id") in RECORD_DISPUTES or norm.get("dispute_id") not in pool_ids():
        return UNAVAILABLE
    if norm.get("evidence_submitted_by_seller"):
        return USED
    links = norm.get("action_links") or {}
    if norm.get("status") == "WAITING_FOR_SELLER_RESPONSE" and "provide-evidence" in links:
        state = ANSWER
    elif norm.get("status") == "WAITING_FOR_BUYER_RESPONSE" and norm.get("stage") == "INQUIRY" and "escalate" in links:
        state = START
    else:
        return UNAVAILABLE
    return TAKEN if holder and holder != session else state


def escalate_path(norm: dict) -> str:
    """The path of the escalate link PayPal gave: sandbox host, this dispute, POST, nothing else."""
    link = (norm.get("action_links") or {}).get("escalate") or {}
    parsed = urlparse(str(link.get("href") or ""))
    expected = f"/v1/customer/disputes/{norm.get('dispute_id')}/escalate"
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != SANDBOX_HOST or parsed.path != expected or parsed.query:
        raise Refused("PayPal's escalate action for this case is not usable here; nothing was sent.")
    if str(link.get("method") or "POST").upper() != "POST":
        raise Refused("PayPal's escalate action for this case is not usable here; nothing was sent.")
    return parsed.path


# ---------------------------------------------------------------------------------------------------------- storage
def _connect() -> sqlite3.Connection:
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def holder_of(dispute_id: str, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
    conn = _connect()
    try:
        row = conn.execute("SELECT session, taken_at FROM judge_case_locks WHERE dispute_id=?", (dispute_id,)).fetchone()
        return row["session"] if row and now - row["taken_at"] < LOCK_TTL_SECONDS else None
    finally:
        conn.close()


def holders(now: float | None = None) -> dict[str, str]:
    now = time.time() if now is None else now
    conn = _connect()
    try:
        return {r["dispute_id"]: r["session"] for r in conn.execute("SELECT dispute_id, session, taken_at FROM judge_case_locks")
                if now - r["taken_at"] < LOCK_TTL_SECONDS}
    finally:
        conn.close()


def take(dispute_id: str, session: str, now: float | None = None) -> bool:
    """One transaction: the case is this session's, or free (no row, or an expired one). A session holds one case at a time:
    asking for another while the first has been started is refused; asking before any start moves the lock."""
    now = time.time() if now is None else now
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT session, taken_at FROM judge_case_locks WHERE dispute_id=?", (dispute_id,)).fetchone()
            if row and row["session"] != session and now - row["taken_at"] < LOCK_TTL_SECONDS:
                conn.execute("COMMIT")
                return False
            mine = conn.execute("SELECT dispute_id, taken_at FROM judge_case_locks WHERE session=? AND dispute_id<>?",
                                (session, dispute_id)).fetchall()
            for other in mine:
                if now - other["taken_at"] >= LOCK_TTL_SECONDS:
                    conn.execute("DELETE FROM judge_case_locks WHERE dispute_id=?", (other["dispute_id"],))
                    continue
                started = conn.execute("SELECT 1 FROM judge_starts WHERE session=? AND dispute_id=?",
                                       (session, other["dispute_id"])).fetchone()
                if started:
                    conn.execute("ROLLBACK")
                    raise Refused("You already have a case in progress. Finish it first.")
                conn.execute("DELETE FROM judge_case_locks WHERE dispute_id=?", (other["dispute_id"],))
            conn.execute("INSERT INTO judge_case_locks(dispute_id, session, taken_at) VALUES (?,?,?) "
                         "ON CONFLICT(dispute_id) DO UPDATE SET session=excluded.session, taken_at=excluded.taken_at",
                         (dispute_id, session, now))
            conn.execute("COMMIT")
            return True
        except Refused:
            raise
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def record_start(session: str, dispute_id: str, now: float | None = None) -> None:
    conn = _connect()
    try:
        conn.execute("INSERT INTO judge_starts(ts, session, dispute_id) VALUES (?,?,?)",
                     (time.time() if now is None else now, session, dispute_id))
    finally:
        conn.close()


def check_limits(session: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    conn = _connect()
    try:
        last = conn.execute("SELECT MAX(ts) AS t FROM judge_starts WHERE session=?", (session,)).fetchone()["t"]
        day_mine = conn.execute("SELECT COUNT(*) AS n FROM judge_starts WHERE session=? AND ts>?", (session, now - DAY)).fetchone()["n"]
        day_all = conn.execute("SELECT COUNT(*) AS n FROM judge_starts WHERE ts>?", (now - DAY,)).fetchone()["n"]
    finally:
        conn.close()
    if last is not None and now - last < START_GAP_SECONDS:
        raise Refused("You started a case a few minutes ago. Wait a little before starting another.")
    if day_mine >= STARTS_PER_SESSION_PER_DAY or day_all >= STARTS_PER_DAY_GLOBAL:
        raise Refused("No more cases can be started right now. Use the local path in the judge guide.")


# ---------------------------------------------------------------------------------------------------------- reads
def read_case(client, dispute_id: str) -> tuple[dict, dict]:
    """(raw, norm) from a fresh GET. norm is the app's normalisation, with the re-arm note exception."""
    from datetime import datetime, timezone
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "judge_pool_get")
    norm = disputes.normalize(raw, datetime.now(timezone.utc))
    if norm.get("dispute_id") != dispute_id:
        raise Refused("PayPal returned a different case; nothing was sent.")
    return raw, norm


# ---------------------------------------------------------------------------------------------------------- the write
def request_sha(dispute_id: str) -> str:
    return hashlib.sha256(f"{dispute_id}|escalate|{NOTE}".encode("utf-8")).hexdigest()


def start_case(read_client, write_client, dispute_id: str, session: str) -> dict:
    """The re-arm: every rule is re-read from PayPal right now. Returns {"status", "stage", "state"} of the read-back, or raises
    Refused. `read_client` and `write_client` are SandboxClient-like (the second logs to the helper's file)."""
    if dispute_id not in pool_ids():
        raise Refused("This case is not in the pool.")
    _raw, norm = read_case(read_client, dispute_id)
    holder = holder_of(dispute_id)
    state = case_state(norm, holder=holder, session=session)
    if state == TAKEN:
        raise Refused("Another visitor took this case.")
    if state == ANSWER:
        return {"status": norm["status"], "stage": norm["stage"], "state": ANSWER, "started": False}
    if state != START:
        raise Refused("PayPal does not offer this case to start now; nothing was sent.")
    path = escalate_path(norm)
    check_limits(session)
    if not take(dispute_id, session):
        raise Refused("Another visitor took this case.")
    sha = request_sha(dispute_id)
    claim = write_guard.reserve(dispute_id, ACTION, sha)
    if not claim.reserved:
        raise Refused("This case was already started.")
    final = write_guard.UNKNOWN
    try:
        try:
            write_client.call("POST", path, OP, json_body={"note": NOTE}, log_extra={"judge_session": session})
            final = write_guard.SENT
        except disputes.PayPalError as exc:
            final = write_guard.REFUSED if getattr(exc, "status", None) else write_guard.UNKNOWN
            raise Refused("PayPal refused the request; nothing changed.") from exc
    finally:
        write_guard.finish(dispute_id, ACTION, sha, final)
    record_start(session, dispute_id)
    _raw, after = read_case(read_client, dispute_id)
    write_client._log({"op": "judge_pool_readback", "dispute_id": dispute_id, "judge_session": session,
                       "status": after.get("status"), "stage": after.get("stage")})
    return {"status": after.get("status"), "stage": after.get("stage"), "state": START, "started": True}
