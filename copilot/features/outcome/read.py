"""One GET of a dispute, reduced to what the outcome page states. The outcome is shown only as PayPal states it.

Outcome codes PayPal documents for `dispute_outcome.outcome_code`: RESOLVED_BUYER_FAVOUR, RESOLVED_SELLER_FAVOUR,
RESOLVED_WITH_PAYOUT, CANCELED_BY_BUYER, ACCEPTED, DENIED, NONE, OTHER. Each gets a plain label by code, always
printed with "as stated by PayPal"; nothing here says what the code means for the seller's chances. `NONE` is PayPal's
own word for no outcome, so it does not count as a decision. An unknown code is shown verbatim."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


from copilot.core import paypal_words
from copilot.core.context import SRC_DISPUTE, Limitation
from copilot.core.disputes import normalize
from step3_dispute import _ZERO_DECIMAL, money_to_minor  # noqa: E402  (pilot/ is on sys.path via copilot.core.disputes)

OP = "outcome_read"
OUTCOME_LABELS = {
    "RESOLVED_BUYER_FAVOUR": "Resolved in the buyer's favor",
    "RESOLVED_SELLER_FAVOUR": "Resolved in the seller's favor",
    "RESOLVED_WITH_PAYOUT": "Resolved with a payout",
    "CANCELED_BY_BUYER": "Canceled by the buyer",
    "ACCEPTED": "Accepted",
    "DENIED": "Denied",
    "NONE": "No outcome",
    "OTHER": "Other outcome",
}


def failure_message(exc: Exception, what: str) -> str | None:
    """Plain-English text for an expected PayPal failure; None for a programming error."""
    return paypal_words.could_not(exc, what)


@dataclass(frozen=True)
class OutcomeRead:
    stage: str | None
    status: str | None
    outcome_code: str | None
    amount_refunded_cents: int | None
    decided: bool
    links: tuple
    appeal_link_present: bool
    seller_response_due_date: str | None
    limitations: tuple = ()
    norm: dict | None = None          # the normalised dispute, for the gaps check (not shown on the page)
    amount_refunded_currency: str | None = None

    @property
    def label(self) -> str | None:
        return outcome_label(self.outcome_code)

    @property
    def refunded_text(self) -> str | None:
        """'15.00 USD': money from integer cents and PayPal's currency code, never a float."""
        cents = self.amount_refunded_cents
        if cents is None:
            return None
        whole_units = self.amount_refunded_currency in _ZERO_DECIMAL
        whole, frac = divmod(cents, 1 if whole_units else 100)
        number = str(whole) if whole_units else f"{whole}.{frac:02d}"
        return f"{number} {self.amount_refunded_currency}" if self.amount_refunded_currency else number


def outcome_label(code: str | None) -> str | None:
    if not code:
        return None
    return OUTCOME_LABELS.get(code, f"Outcome code {code}")


def read_outcome(client, dispute_id: str) -> OutcomeRead:
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", OP)
    except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
        msg = failure_message(exc, "read the dispute")
        if msg is None:
            raise
        return OutcomeRead(None, None, None, None, False, (), False, None, (Limitation(SRC_DISPUTE, msg, paypal_words.technical_of(exc)),))
    norm = normalize(raw, datetime.now(timezone.utc))
    outcome = raw.get("dispute_outcome") if isinstance(raw, dict) else None
    outcome = outcome if isinstance(outcome, dict) else {}
    code = outcome.get("outcome_code") or None
    limits = []
    refunded = currency = None
    if outcome.get("amount_refunded") is not None:
        try:
            refunded, currency = money_to_minor(outcome["amount_refunded"])
        except ValueError:
            limits.append(Limitation(SRC_DISPUTE, "PayPal returned a refund amount that could not be read."))
    links = tuple(sorted(norm.get("action_links") or {}))
    return OutcomeRead(norm.get("stage"), norm.get("status"), code, refunded,
                       norm.get("status") == "RESOLVED" or (code is not None and code != "NONE"), links,
                       "appeal" in links, norm.get("seller_response_due_date"), tuple(limits), norm, currency)
