"""Money per case, as PayPal's record of the dispute states it: kept, lost, or still held, with totals per currency.

What decides each case is PayPal's GET of the dispute, never model text:
  held      the dispute is not RESOLVED: the disputed amount is still held;
  lost      RESOLVED, and `money_movements` lists a SELLER DEBIT of reason DISPUTE_SETTLEMENT: that debit is the amount lost.
            A second SELLER DEBIT of reason DISPUTE_SETTLEMENT_FEE is kept apart ("fees on PayPal's record") and is never
            added into the amount lost;
  kept      RESOLVED, no SELLER DEBIT at all in `money_movements`, and an outcome code that leaves the sale with the seller
            (KEPT_CODES): the disputed amount is kept;
  unstated  a RESOLVED record that fits neither pattern: PayPal's outcome words are shown and the amount is not stated.

Money is integer minor units per currency, turned into text by code; currencies are never converted or added together.
A RESOLVED record is stored once it is read (own table `outcome_money_reads`, keyed by dispute id and PayPal's update_time),
so the totals page asks PayPal again only for cases it has not read at that update_time."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from copilot import config
from copilot.core import pool_scope
from copilot.core.disputes import fetch_all_disputes
from copilot.features.outcome.read import failure_message, outcome_label
from step3_dispute import _ZERO_DECIMAL, money_to_minor  # noqa: E402  (pilot/ is on sys.path via copilot.core.disputes)

OP = "outcome_money_read"
HELD, KEPT, LOST, UNSTATED = "held", "kept", "lost", "unstated"
KEPT_CODES = frozenset({"RESOLVED_WITH_PAYOUT", "RESOLVED_SELLER_FAVOUR", "CANCELED_BY_BUYER"})
WORDS = {KEPT: "Kept", LOST: "Lost", HELD: "Still held", UNSTATED: "Amount not stated by PayPal"}
SENTENCES = {
    KEPT: "You keep this sale. PayPal's record shows no debit to you.",
    LOST: "PayPal's record shows this debit to you.",
    HELD: "PayPal has not decided this case yet.",
    UNSTATED: "PayPal's record of this case does not state an amount kept or lost.",
}
FEES_LABEL = "Fees on PayPal's record"
MAX_READS = 30          # fresh PayPal reads one load of the totals page may make; the rest are counted as not read yet
_DDL = """
CREATE TABLE IF NOT EXISTS outcome_money_reads (
    dispute_id  TEXT PRIMARY KEY,
    update_time TEXT NOT NULL,
    read_at     TEXT NOT NULL,
    facts_json  TEXT NOT NULL
);
"""


def money_text(cents: int | None, currency: str | None) -> str | None:
    """219900, "MXN" -> "2,199.00 MXN": integer arithmetic only."""
    if isinstance(cents, bool) or not isinstance(cents, int) or not currency:
        return None
    if currency in _ZERO_DECIMAL:
        return f"{cents:,} {currency}"
    whole, frac = divmod(cents, 100)
    return f"{whole:,}.{frac:02d} {currency}"


@dataclass(frozen=True)
class CaseMoney:
    dispute_id: str | None
    state: str                       # held | kept | lost | unstated
    amount_cents: int | None
    currency: str | None
    fee_cents: int | None = None     # the DISPUTE_SETTLEMENT_FEE debits, apart from the amount lost
    fee_currency: str | None = None
    status: str | None = None
    outcome_code: str | None = None

    @property
    def word(self) -> str:
        return WORDS[self.state]

    @property
    def sentence(self) -> str:
        return SENTENCES[self.state]

    @property
    def amount_text(self) -> str | None:
        return money_text(self.amount_cents, self.currency)

    @property
    def fee_text(self) -> str | None:
        return money_text(self.fee_cents, self.fee_currency)

    @property
    def outcome_words(self) -> str | None:
        return outcome_label(self.outcome_code)

    def facts(self) -> dict:
        return {"dispute_id": self.dispute_id, "state": self.state, "amount_cents": self.amount_cents, "currency": self.currency,
                "fee_cents": self.fee_cents, "fee_currency": self.fee_currency, "status": self.status,
                "outcome_code": self.outcome_code}


def _minor(money) -> tuple[int | None, str | None]:
    try:
        return money_to_minor(money)
    except ValueError:
        return None, None


def _sum(movements: list) -> tuple[int | None, str | None]:
    """The movements' amounts added in one currency; (None, None) when one cannot be read or the currencies differ."""
    total, currency = 0, None
    for item in movements:
        cents, cur = _minor(item.get("amount"))
        if cents is None or (currency is not None and cur != currency):
            return None, None
        total, currency = total + cents, cur
    return (total, currency) if movements else (None, None)


def classify(raw: dict) -> CaseMoney:
    """One dispute as PayPal returned it (the full GET) -> what happened to the seller's money. Never a guess."""
    raw = raw if isinstance(raw, dict) else {}
    dispute_id, status = raw.get("dispute_id"), raw.get("status")
    outcome = raw.get("dispute_outcome") if isinstance(raw.get("dispute_outcome"), dict) else {}
    code = outcome.get("outcome_code") or None
    amount, currency = _minor(raw.get("dispute_amount"))
    if status != "RESOLVED":
        return CaseMoney(dispute_id, HELD if amount is not None else UNSTATED, amount, currency, status=status, outcome_code=code)
    movements = raw.get("money_movements")
    movements = [m for m in movements if isinstance(m, dict)] if isinstance(movements, list) else []
    debits = [m for m in movements if m.get("affected_party") == "SELLER" and m.get("type") == "DEBIT"]
    settlement = [m for m in debits if m.get("reason") == "DISPUTE_SETTLEMENT"]
    fee_cents, fee_currency = _sum([m for m in debits if m.get("reason") == "DISPUTE_SETTLEMENT_FEE"])
    if settlement:
        lost, lost_currency = _sum(settlement)
        if lost is not None:
            return CaseMoney(dispute_id, LOST, lost, lost_currency, fee_cents, fee_currency, status, code)
    elif not debits and code in KEPT_CODES and amount is not None:
        return CaseMoney(dispute_id, KEPT, amount, currency, status=status, outcome_code=code)
    return CaseMoney(dispute_id, UNSTATED, None, None, status=status, outcome_code=code)


@dataclass
class CurrencyTotal:
    currency: str
    kept_cents: int = 0
    kept_cases: int = 0
    lost_cents: int = 0
    lost_cases: int = 0
    held_cents: int = 0
    held_cases: int = 0
    fee_cents: int = 0
    fee_cases: int = 0

    def text(self, name: str) -> str:
        return money_text(getattr(self, name + "_cents"), self.currency)


def totals(cases) -> list[CurrencyTotal]:
    """Totals per currency, never converted. A fee in another currency than its case goes to that currency's own total."""
    out: dict[str, CurrencyTotal] = {}
    for case in cases:
        if case.state in (KEPT, LOST, HELD) and case.amount_cents is not None and case.currency:
            row = out.setdefault(case.currency, CurrencyTotal(case.currency))
            setattr(row, case.state + "_cents", getattr(row, case.state + "_cents") + case.amount_cents)
            setattr(row, case.state + "_cases", getattr(row, case.state + "_cases") + 1)
        if case.fee_cents is not None and case.fee_currency:
            row = out.setdefault(case.fee_currency, CurrencyTotal(case.fee_currency))
            row.fee_cents, row.fee_cases = row.fee_cents + case.fee_cents, row.fee_cases + 1
    return [out[key] for key in sorted(out)]


# ---- stored read-backs of resolved cases ---------------------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    path = Path(config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_DDL)
    return conn


def stored(dispute_id: str, update_time: str | None) -> CaseMoney | None:
    """The stored read of a resolved case, only when it was read at this same PayPal update_time."""
    if not dispute_id or not update_time:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT facts_json FROM outcome_money_reads WHERE dispute_id = ? AND update_time = ?",
                           (dispute_id, update_time)).fetchone()
    finally:
        conn.close()
    return CaseMoney(**json.loads(row[0])) if row else None


def store(case: CaseMoney, update_time: str | None) -> None:
    if not case.dispute_id or not update_time or case.status != "RESOLVED":
        return
    conn = _connect()
    try:
        conn.execute("INSERT OR REPLACE INTO outcome_money_reads (dispute_id, update_time, read_at, facts_json) VALUES (?, ?, ?, ?)",
                     (case.dispute_id, update_time, time.strftime("%Y-%m-%dT%H:%M:%S%z"), json.dumps(case.facts(), sort_keys=True)))
        conn.commit()
    finally:
        conn.close()


# ---- the two pages' data -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseView:
    case: CaseMoney | None
    limitation: str | None = None


def case_view(client, dispute_id: str) -> CaseView:
    """One GET of the dispute (through the request's client, which is the scoped one in the hosted demo)."""
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", OP)
    except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
        message = failure_message(exc, "read the dispute")
        if message is None:
            raise
        return CaseView(None, message)
    case = classify(raw)
    store(case, raw.get("update_time") if isinstance(raw, dict) else None)
    return CaseView(case)


@dataclass(frozen=True)
class AccountView:
    cases: tuple                      # CaseMoney, in PayPal's list order
    totals: tuple                     # CurrencyTotal per currency
    unstated: tuple                   # resolved cases whose record states no amount
    not_read: int = 0                 # resolved cases this load did not read (MAX_READS reached or PayPal did not answer)
    cut: bool = False                 # PayPal's list was not read to its last page
    fresh_reads: int = 0
    counts: dict = field(default_factory=dict)


def account_view(client, max_reads: int = MAX_READS) -> AccountView:
    """Every case the app may list (in the hosted demo: the pool only), each classified from PayPal's record.

    A case that is not RESOLVED is held at the amount in PayPal's list. A RESOLVED case needs its full record: the list item is used
    when it already carries the record (the hosted demo reads its cases one by one), then a stored read at the same update_time,
    then one GET, at most `max_reads` per load."""
    items, _pages = fetch_all_disputes(client)
    full_records = pool_scope.active()        # the hosted demo's list is already one GET per pool case: no second read
    cases, not_read, fresh = [], 0, 0
    for item in items:
        if not isinstance(item, dict):
            continue
        dispute_id, update_time = item.get("dispute_id"), item.get("update_time")
        if item.get("status") != "RESOLVED" or full_records:
            case = classify(item)             # an open case, or a full record already in hand
            store(case, update_time)
            cases.append(case)
            continue
        case = stored(dispute_id, update_time)
        if case is None and fresh < max_reads:
            fresh += 1
            view = case_view(client, dispute_id)
            case = view.case if view.case is not None and view.case.status == "RESOLVED" else None
            if case is not None:
                store(case, update_time)      # under the LIST's update_time (PayPal's list drops the milliseconds the record has)
        if case is None:
            not_read += 1
            continue
        cases.append(case)
    counts = {state: sum(1 for c in cases if c.state == state) for state in (KEPT, LOST, HELD, UNSTATED)}
    return AccountView(tuple(cases), tuple(totals(cases)), tuple(c for c in cases if c.state == UNSTATED), not_read,
                       bool(getattr(items, "cut", False)), fresh, counts)
