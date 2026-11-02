"""The fact sheet: what the draft may say, built by code from PayPal's own data. Pure; no model; no float.

It uses the same sources as the resolution feature's message template (`actions.message_text`): the normalised dispute, the
transaction fields and the tracker that `copilot.core.context.fetch_context` returns, so the sheet and the template agree. The
model input includes the shipping address line typed by the buyer, so fact values are not free of injection. Code refuses
bounded lexical echoes and refund wording; the seller reviews. These checks are not a proof against arbitrary injection."""
from __future__ import annotations

import re
from dataclasses import dataclass

from copilot.features.resolution import actions

# Every digit run of 1+ digits: the validator refuses any number the facts do not carry, "14" in "within 14 days" included. The sheet's
# `numbers` use the same pattern, so a fact like 2026-07-01 yields 2026, 07, 01 and the amount 40.00 yields 40, 00.
DIGIT_RUN = re.compile(r"\d+")


@dataclass(frozen=True)
class FactSheet:
    order_date: str | None
    amount_text: str | None
    currency: str | None
    shipping_address_line: str | None
    tracking_number: str | None
    carrier: str | None
    tracking_status: str | None
    delivery_estimate: str | None
    dispute_id: str | None
    dispute_stage: str | None
    literals: tuple = ()       # every non-empty value above, as text
    numbers: tuple = ()        # every digit run (1+ digits) found in those values

    def labelled(self) -> list:
        """(label, value) for each fact that is present, in a fixed order (the prompt and the page both use it)."""
        pairs = (("order date", self.order_date), ("amount", self.amount_text), ("currency", self.currency),
                 ("shipping address", self.shipping_address_line), ("tracking number", self.tracking_number),
                 ("carrier", self.carrier), ("last tracking status", self.tracking_status),
                 ("delivery estimate", self.delivery_estimate), ("dispute id", self.dispute_id),
                 ("dispute stage", self.dispute_stage))
        return [(label, value) for label, value in pairs if value]


def money_text(cents, currency) -> str | None:
    """1000, "USD" -> "10.00 USD" by integer arithmetic (the resolution feature's own helper, through this public wrapper)."""
    return actions._money_text(cents, currency)  # noqa: SLF001 - the one place the private helper is reached


def address_line(address: dict | None) -> str | None:
    return actions._address_line(address)  # noqa: SLF001


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def fact_sheet(norm: dict | None, txn_fields: dict, tracking: dict | None) -> FactSheet:
    """`txn_fields`: payment_date, total_amount_cents, total_currency, shipping_address. `tracking`: tracking_number, carrier,
    tracking_status, delivery_estimate. Anything missing is None and left out of `literals`."""
    norm, txn_fields, tracking = norm or {}, txn_fields or {}, tracking or {}
    date = _clean(str(txn_fields.get("payment_date") or norm.get("create_time") or "")[:10])
    cents = txn_fields.get("total_amount_cents") if isinstance(txn_fields.get("total_amount_cents"), int) \
        else norm.get("amount_cents")
    currency = _clean(txn_fields.get("total_currency") or norm.get("currency"))
    values = dict(order_date=date, amount_text=money_text(cents, currency) if isinstance(cents, int) else None,
                  currency=currency, shipping_address_line=address_line(txn_fields.get("shipping_address")),
                  tracking_number=_clean(tracking.get("tracking_number")), carrier=_clean(tracking.get("carrier")),
                  tracking_status=_clean(tracking.get("tracking_status")),
                  delivery_estimate=_clean(tracking.get("delivery_estimate")), dispute_id=_clean(norm.get("dispute_id")),
                  dispute_stage=_clean(norm.get("stage")))
    literals = tuple(v for v in values.values() if v)
    numbers = tuple(dict.fromkeys(n for lit in literals for n in DIGIT_RUN.findall(lit)))
    return FactSheet(**values, literals=literals, numbers=numbers)
