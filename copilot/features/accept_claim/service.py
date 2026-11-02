"""Accept the claim, the seller's last option (COPILOT_FEATURE_ACCEPT_CLAIM).

PayPal's `accept-claim` link refunds the buyer and closes the dispute. Proofie offers it LAST, never proposes it and no
model or assistant tool can reach it. It is offered only while a fresh GET of the dispute carries PayPal's own link. The
seller reads the consequence, the amount and currency from PayPal's own record, types the amount phrase and approves the
exact request by hash; the send is a separate click. One POST per approved request (copilot.core.write_guard, action
"accept_claim"), then one read-back GET. Own tables in the app database; the resolution tables are not touched.

Local installs, and on the hosted demo the judge's own pool case (`hosted_refusal`); every other hosted case is refused."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from copilot import config
from copilot.core import paypal_words, write_guard
from copilot.core.context import SRC_DISPUTE, Limitation
from copilot.core.disputes import PayPalError, normalize, write_audit
from copilot.core.send import sha256_json
from copilot.features.resolution import actions as act
from copilot.features.resolution.service import ALLOWED_HOST, _definite_refusal, _read, failure_message

ACTION = "accept_claim"
LINK = "accept-claim"
READ_OP, WRITE_OP, READBACK_OP = "accept_claim_read", "accept_claim_post", "accept_claim_readback"
CONSEQUENCE = "This refunds the buyer and closes the dispute. Proofie cannot undo it."
NOTE = "I accept the buyer's claim."
CLAIM_TYPE = "REFUND"
CHECK_TTL_SECONDS = 60 * 60
STATUS_CONFIRMED, STATUS_UNCONFIRMED, STATUS_FAILED, STATUS_REFUSED = "sent_confirmed", "sent_unconfirmed", "failed", "refused_422"
# PayPal's sandbox answers the accept with the link gone and the disputed transaction REFUNDED while the case is still under its review
# (OCQ, 2026-11-02: UNDER_PAYPAL_REVIEW at the read-back, RESOLVED about two minutes later). Decided from the response only.
STATUS_REVIEW = "sent_refunded_in_review"
SENT_STATUSES = (STATUS_CONFIRMED, STATUS_REVIEW, STATUS_UNCONFIRMED)      # the accept may have reached PayPal: counted by the caps and the pool
_NS = uuid.UUID("5d0b6a64-2f3a-4c3e-9d79-7a1b3f6e0c42")
_DDL = """
CREATE TABLE IF NOT EXISTS accept_claim_checks (
    token TEXT PRIMARY KEY, dispute_id TEXT NOT NULL, body_json TEXT NOT NULL, approval_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL, superseded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS accept_claim_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dispute_id TEXT NOT NULL, approval_sha256 TEXT NOT NULL, created_at REAL NOT NULL,
    status TEXT NOT NULL, http_status INTEGER, readback_json TEXT NOT NULL, text TEXT, session TEXT);
"""


# Hosted-copy caps (D-112). Each acceptance refunds the buyer and closes a pool case for good; local installs have none of these.
POOL_FLOOR = 2                         # an acceptance is refused when it would leave fewer than this many unused pool cases
ACCEPTS_PER_SESSION = 1                # per judge session
ACCEPTS_PER_DAY = 1                    # across all sessions, per America/Mexico_City calendar day
DAY_ZONE = "America/Mexico_City"
COOKIE_LEN = (20, 80)       # the judge cookie's accepted length, the same as judge mode's own check


def enabled() -> bool:
    """The setting is on and this is a local install. The hosted demo answers per case: see `allowed`."""
    return config.feature_enabled("accept_claim") and not config.hosted_demo()


def session_of(cookies) -> str | None:
    """The judge session hash of a request's cookies, or None when there is no valid judge cookie. Only the hosted path reads it."""
    from copilot.features.judge_pool import service as pool
    value = (cookies or {}).get("proofie_judge") or ""
    return pool.session_hash(value) if COOKIE_LEN[0] <= len(value) <= COOKIE_LEN[1] else None


def hosted_refusal(dispute_id: str, session: str | None) -> str | None:
    """None when the hosted demo may take this action for this case, else the sentence the visitor reads. Allowed on the
    judge's own pool case (judge mode on, the case in the operator's pool, not a record dispute, held by this session); every other case is refused."""
    off = "Accepting a claim is allowed here on your own judge case, and judge mode is off. Nothing was sent to PayPal."
    if not config.feature_enabled("judge_pool"):
        return off
    from copilot.features.judge_pool import service as pool
    if not pool.enabled():
        return off
    if dispute_id in pool.RECORD_DISPUTES:
        return "This is a record dispute; it is read-only here. Nothing was sent to PayPal."
    if dispute_id not in pool.pool_ids():
        return "This case is not in the judge pool; accepting a claim is allowed here on your own pool case and refused on every other case. Nothing was sent to PayPal."
    if session is None or pool.holder_of(dispute_id) != session:
        return "This case is not yours in this session. Nothing was sent to PayPal."
    return None


def allowed(dispute_id: str, session: str | None = None) -> bool:
    """Local install: the setting. Hosted demo: the setting plus `hosted_refusal` is None for this case and session."""
    if not config.feature_enabled("accept_claim"):
        return False
    return hosted_refusal(dispute_id, session) is None if config.hosted_demo() else True


def _day_start(now: float | None = None) -> float:
    """The timestamp of 00:00 today in America/Mexico_City (a fixed UTC-6 if the zone database is missing: Mexico has no DST now)."""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(DAY_ZONE)
    except Exception:  # noqa: BLE001
        zone = timezone(timedelta(hours=-6))
    local = datetime.fromtimestamp(time.time() if now is None else now, zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def cap_refusal(session: str | None, now: float | None = None) -> str | None:
    """Hosted copy only: the sentence the visitor reads when this session or this day has used its acceptance, else None.
    Counts the rows PayPal confirmed, shows refunded under its review, or left unconfirmed (an unknown answer counts: the claim may have been accepted)."""
    conn = _connect()
    try:
        marks = ",".join("?" * len(SENT_STATUSES))
        mine = conn.execute(f"SELECT COUNT(*) AS n FROM accept_claim_sent WHERE session=? AND status IN ({marks})", (session, *SENT_STATUSES)).fetchone()["n"] \
            if session else 0
        today = conn.execute(f"SELECT COUNT(*) AS n FROM accept_claim_sent WHERE created_at>=? AND status IN ({marks})",
                             (_day_start(now), *SENT_STATUSES)).fetchone()["n"]
    finally:
        conn.close()
    if mine >= ACCEPTS_PER_SESSION:
        return "You have already accepted a claim in this session; the hosted copy allows one per session. Nothing was sent to PayPal."
    if today >= ACCEPTS_PER_DAY:
        return "A claim was already accepted on the hosted copy today; it allows one per day. Try again tomorrow, or use the local path in the judge guide. Nothing was sent to PayPal."
    return None


def floor_refusal(client, dispute_id: str) -> str | None:
    """Hosted copy only: refused when accepting this case would leave fewer than POOL_FLOOR unused pool cases. Unused = PayPal's
    fresh read offers the case to start or answer (or another visitor holds it) and no claim was accepted on it. Reads only."""
    from copilot.features.judge_pool import service as pool
    held = pool.holders()
    unused = 0
    for pid in pool.pool_ids():
        if pid == dispute_id or was_accepted(pid):
            continue
        try:
            _raw, norm = pool.read_case(client, pid)
        except Exception:  # noqa: BLE001 - a case that cannot be read is not counted as unused
            continue
        if pool.case_state(norm, holder=held.get(pid), session=None) in (pool.START, pool.ANSWER, pool.TAKEN):
            unused += 1
    if unused < POOL_FLOOR:
        return ("Accepting this claim would leave fewer than %d unused cases in the judge pool, so it is refused. "
                "Nothing was sent to PayPal." % POOL_FLOOR)
    return None


def was_accepted(dispute_id: str) -> bool:
    """This install has sent the accept for this dispute (PayPal confirmed it, or the answer is not known): the pool shows it used."""
    conn = _connect()
    try:
        row = conn.execute(f"SELECT 1 FROM accept_claim_sent WHERE dispute_id=? AND status IN ({','.join('?' * len(SENT_STATUSES))}) LIMIT 1",
                           (dispute_id, *SENT_STATUSES)).fetchone()
    finally:
        conn.close()
    return row is not None


def _connect() -> sqlite3.Connection:
    path = Path(config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    if "session" not in {r["name"] for r in conn.execute("PRAGMA table_info(accept_claim_sent)")}:
        conn.execute("ALTER TABLE accept_claim_sent ADD COLUMN session TEXT")      # a database made before the hosted caps
        conn.commit()
    return conn


def has_link(norm: dict | None) -> bool:
    links = (norm or {}).get("action_links") or {}
    return bool((links.get(LINK) or links.get(LINK.replace("-", "_")) or {}).get("href"))


def offered(norm: dict | None, dispute_id: str = "", session: str | None = None) -> bool:
    """The page shows the control: the setting is on (hosted: your own pool case) and PayPal's fresh read has the link."""
    return allowed(dispute_id or (norm or {}).get("dispute_id") or "", session) and has_link(norm)


def phrase_for(amount: str, currency: str) -> str:
    return f"REFUND {amount} {currency}"


def _link_problem(norm: dict, dispute_id: str) -> str | None:
    links = norm.get("action_links") or {}
    link = links.get(LINK) or links.get(LINK.replace("-", "_")) or {}
    parsed = urlparse(link.get("href") or "")
    if not link.get("href"):
        return "PayPal does not offer to accept this claim now. Nothing was sent."
    if parsed.scheme != "https" or parsed.netloc != ALLOWED_HOST or parsed.path != f"/v1/customer/disputes/{dispute_id}/accept-claim" \
            or (link.get("method") or "").upper() != "POST":
        return "PayPal gave an unexpected link for this action. Nothing was sent."
    return None


def _money(norm: dict) -> tuple[str, str] | None:
    cents, currency = norm.get("amount_cents"), norm.get("currency")
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0 or not currency or not str(currency).isalpha():
        return None
    return act.cents_to_str(cents, currency), currency


def approval_body(dispute_id: str, amount: str, currency: str) -> dict:
    """What the approval hash binds: this dispute, the action, PayPal's amount and currency, and the exact request."""
    return {"action": ACTION, "dispute_id": dispute_id, "amount": amount, "currency": currency,
            "request": {"note": NOTE, "accept_claim_type": CLAIM_TYPE}}


def approval_sha(body: dict) -> str:
    return sha256_json(body)


@dataclass(frozen=True)
class Prepared:
    ok: bool
    reason: str = ""
    body: dict | None = None
    sha256: str = ""


def prepare(client, dispute_id: str, session: str | None = None) -> Prepared:
    """A fresh GET; a check only when the setting is on and PayPal shows the link. Nothing is written to PayPal."""
    if not allowed(dispute_id, session):
        return Prepared(False, "Accepting a claim is switched off in this installation. Nothing was sent.")
    _raw, norm, _limits = _read(client, dispute_id, READ_OP)
    if norm is None:
        return Prepared(False, "PayPal could not be read. Nothing was sent.")
    problem = _link_problem(norm, dispute_id)
    if problem:
        return Prepared(False, problem)
    money = _money(norm)
    if money is None:
        return Prepared(False, "PayPal's record does not show the disputed amount. Nothing was sent.")
    body = approval_body(dispute_id, *money)
    return Prepared(True, "", body, approval_sha(body))


def save_check(dispute_id: str, body: dict, sha: str, now=None) -> str:
    token = secrets.token_urlsafe(24)
    conn = _connect()
    try:
        conn.execute("INSERT INTO accept_claim_checks (token, dispute_id, body_json, approval_sha256, created_at) VALUES (?,?,?,?,?)",
                     (token, dispute_id, json.dumps(body, sort_keys=True, ensure_ascii=False), sha, time.time() if now is None else now))
        conn.commit()
    finally:
        conn.close()
    return token


def get_check(token: str, dispute_id: str, now=None, include_spent: bool = False) -> dict | None:
    if not isinstance(token, str) or not token or len(token) > 200:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM accept_claim_checks WHERE token=? AND dispute_id=?", (token, dispute_id)).fetchone()
    finally:
        conn.close()
    if row is None or (time.time() if now is None else now) - row["created_at"] > CHECK_TTL_SECONDS \
            or (row["superseded"] and not include_spent):
        return None
    body = json.loads(row["body_json"])
    if approval_sha(body) != row["approval_sha256"]:
        return None
    return {"token": token, "dispute_id": dispute_id, "body": body, "approval_sha256": row["approval_sha256"],
            "spent": bool(row["superseded"])}


def spend_check(token: str, dispute_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE accept_claim_checks SET superseded=1 WHERE token=? AND dispute_id=?", (token, dispute_id))
        conn.commit()
    finally:
        conn.close()


def save_sent(dispute_id: str, sha: str, status: str, http_status, readback: dict, text: str | None, session: str | None = None) -> int:
    conn = _connect()
    try:
        cur = conn.execute("INSERT INTO accept_claim_sent (dispute_id, approval_sha256, created_at, status, http_status, readback_json, text, session) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (dispute_id, sha, time.time(), status, http_status, json.dumps(readback, sort_keys=True), text, session))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_sent(dispute_id: str, row_id) -> dict | None:
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM accept_claim_sent WHERE id=? AND dispute_id=?", (row_id, dispute_id)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    out["readback"] = json.loads(out.pop("readback_json"))
    return out


def find_sent(dispute_id: str, sha: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT id FROM accept_claim_sent WHERE dispute_id=? AND approval_sha256=? AND status NOT IN (?, ?) "
                           "ORDER BY id DESC LIMIT 1", (dispute_id, sha, STATUS_FAILED, STATUS_REFUSED)).fetchone()
    finally:
        conn.close()
    return get_sent(dispute_id, row["id"]) if row else None


def update_sent(row_id: int, status: str, readback: dict) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE accept_claim_sent SET status=?, readback_json=? WHERE id=?", (status, json.dumps(readback, sort_keys=True), row_id))
        conn.commit()
    finally:
        conn.close()


def transaction_status(raw) -> str | None:
    """The disputed transaction's status from PayPal's raw response (normalize does not carry it): REFUNDED when every disputed
    transaction PayPal lists is refunded, else the first one that is not refunded, else None."""
    states = [str(t.get("transaction_status") or "").upper() for t in (raw or {}).get("disputed_transactions") or [] if isinstance(t, dict)]
    states = [s for s in states if s]
    if not states:
        return None
    return "REFUNDED" if all(s == "REFUNDED" for s in states) else next(s for s in states if s != "REFUNDED")


def _readback(norm: dict | None, raw: dict | None = None) -> dict:
    return {"status": (norm or {}).get("status"), "outcome": (norm or {}).get("outcome"), "transaction_status": transaction_status(raw),
            "link_present": has_link(norm), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def _closed(norm: dict | None) -> bool:
    return bool(norm) and str(norm.get("status") or "").upper() in ("RESOLVED", "CLOSED") and not has_link(norm)


def _in_review(raw: dict | None, norm: dict | None) -> bool:
    """PayPal's response: the accept_claim link is gone, the disputed transaction is REFUNDED, the case is not closed yet."""
    return bool(norm) and not has_link(norm) and not _closed(norm) and transaction_status(raw) == "REFUNDED"


def classify(raw: dict | None, norm: dict | None) -> str:
    """The stored status for a read-back, from PayPal's response only: closed, refunded under review, or not known."""
    if _closed(norm):
        return STATUS_CONFIRMED
    return STATUS_REVIEW if _in_review(raw, norm) else STATUS_UNCONFIRMED


@dataclass(frozen=True)
class Outcome:
    status: str      # sent_refunded_in_review | refused_cap | rejected_hash | rejected_phrase | rejected_link | rejected_stale | read_failed | already_sent | disabled | sent_* | refused_422 | failed
    reason: str = ""
    row_id: int | None = None
    existing: dict | None = None
    http_status: int | None = None


def register(client, dispute_id: str, body: dict, sha256: str, phrase: str, session: str | None = None) -> Outcome:
    """Validate, POST once, read back once. Order: switch, hash, phrase, repeat, FRESH GET (link, same amount and currency)."""
    if not allowed(dispute_id, session):
        return Outcome("disabled", "Accepting a claim is switched off in this installation. Nothing was sent.")
    if config.hosted_demo() and not was_accepted(dispute_id):
        reason = cap_refusal(session)
        if reason:
            return Outcome("refused_cap", reason)
    if not isinstance(body, dict) or approval_sha(body) != sha256 or body.get("action") != ACTION or body.get("dispute_id") != dispute_id:
        return Outcome("rejected_hash", "The approval does not match this request. Nothing was sent.")
    if (phrase or "").strip() != phrase_for(body["amount"], body["currency"]):
        return Outcome("rejected_phrase", "The phrase does not match. Type it exactly as shown. Nothing was sent.")
    repeat = find_sent(dispute_id, sha256)
    if repeat is not None or write_guard.is_blocking(write_guard.lookup(dispute_id, ACTION, sha256)):
        return Outcome("already_sent", "This claim was already accepted from this approval. Nothing was sent again.",
                       row_id=repeat["id"] if repeat else None, existing=repeat)
    _raw, norm, _limits = _read(client, dispute_id, READ_OP)
    if norm is None:
        return Outcome("read_failed", "PayPal could not be read. Nothing was sent.")
    problem = _link_problem(norm, dispute_id)
    if problem:
        return Outcome("rejected_link", problem)
    if _money(norm) != (body["amount"], body["currency"]):
        return Outcome("rejected_stale", "PayPal's record changed since this page was made. Nothing was sent. Review it again.")
    if config.hosted_demo():
        reason = floor_refusal(client, dispute_id)
        if reason:
            return Outcome("refused_cap", reason)
    claim = write_guard.reserve(dispute_id, ACTION, sha256)
    if not claim.reserved:
        return Outcome("already_sent", "This claim was already accepted from this approval. Nothing was sent again.")
    final = write_guard.UNKNOWN
    path = f"/v1/customer/disputes/{dispute_id}/accept-claim"
    try:
        try:
            client.call("POST", path, WRITE_OP, json_body=body["request"], request_id=str(uuid.uuid5(_NS, f"{dispute_id}:{sha256}")),
                        log_extra=write_audit(sha256, sha256))
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "status", None)
            if isinstance(exc, PayPalError) and code == 422:
                text, status = "PayPal refused this now. Nothing was changed.", STATUS_REFUSED
            else:
                text = failure_message(exc, "accept the claim")
                if text is None:
                    raise
                status = STATUS_FAILED if _definite_refusal(code) else STATUS_UNCONFIRMED
            final = write_guard.REFUSED if _definite_refusal(code) else write_guard.UNKNOWN
            return Outcome(status, text, save_sent(dispute_id, sha256, status, code if isinstance(code, int) else None, {}, text, session),
                           http_status=code if isinstance(code, int) else None)
        final = write_guard.SENT
        try:
            after_raw, after, _l = _read(client, dispute_id, READBACK_OP)        # the one read-back GET
        except Exception:  # noqa: BLE001 - the POST already happened; the row must exist
            after_raw, after = None, None
        status = classify(after_raw, after)
        return Outcome(status, "", save_sent(dispute_id, sha256, status, None, _readback(after, after_raw), None, session))
    finally:
        write_guard.finish(dispute_id, ACTION, sha256, final)


def recheck(client, dispute_id: str, row_id: int) -> dict | None:
    """One GET; the stored status follows PayPal's response, never the text of a model. Writes nothing to PayPal."""
    row = get_sent(dispute_id, row_id)
    if row is None or row["status"] in (STATUS_CONFIRMED, STATUS_FAILED, STATUS_REFUSED):
        return row
    raw, norm, _l = _read(client, dispute_id, READBACK_OP)
    if norm is not None:
        seen = classify(raw, norm)
        update_sent(row["id"], row["status"] if seen == STATUS_UNCONFIRMED else seen, _readback(norm, raw))
    return get_sent(dispute_id, row_id)
