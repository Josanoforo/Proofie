"""How the card networks and PayPal read dispute ratios, shown beside the account cliff. Every number and date is a
constant here with its source and the day it was read; the template only renders them. The network figures come from an
industry benchmark summary read on 2026-10-06, not from Visa's or Mastercard's own documents (not yet checked there)."""
from __future__ import annotations

from dataclasses import dataclass

from copilot.features.account_cliff.rules import HIGH_VOLUME_MIN_SALES, HIGH_VOLUME_RATE_BPS, SOURCE_HELP350

SOURCE_BENCHMARK = "industry benchmark summary of card-network programs, secondary source"
READ_DATE_BENCHMARK = "2026-10-06"
READ_DATE_HELP350 = "2026-10-05"


@dataclass(frozen=True)
class NetworkFact:
    name: str
    display: str
    source: str
    read_date: str


VISA_VAMP = NetworkFact(
    "Visa VAMP",
    "A merchant is \"Excessive\" at 150 basis points (1.50 %) in Latin America and the Caribbean since 2025-06-01. "
    "The ratio is card-not-present fraud reports plus disputes, divided by settled transactions.",
    SOURCE_BENCHMARK + " (Visa VAMP, program dates as reported there)", READ_DATE_BENCHMARK)
MASTERCARD_ECM = NetworkFact(
    "Mastercard ECM",
    "At least 100 chargebacks and at least 1.5 % of the previous month's transactions.",
    SOURCE_BENCHMARK + " (Mastercard Excessive Chargeback Merchant program)", READ_DATE_BENCHMARK)
PAYPAL_HIGH_VOLUME = NetworkFact(
    "PayPal high-volume dispute rule",
    f"More than {HIGH_VOLUME_MIN_SALES.value} sales in the prior three full months and a dispute rate of "
    f"{HIGH_VOLUME_RATE_BPS.value / 100:.2f} % or more.",
    SOURCE_HELP350, READ_DATE_HELP350)

WON_DISPUTE_SENTENCE = "A dispute you win still counts in these ratios."

NETWORK_FACTS = (VISA_VAMP, MASTERCARD_ECM, PAYPAL_HIGH_VOLUME)
