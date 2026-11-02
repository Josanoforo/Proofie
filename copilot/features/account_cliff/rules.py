"""The rule's constants, each with its source. Money only as integer cents, text made by code."""
from __future__ import annotations

from dataclasses import dataclass

SOURCE_HELP350 = "PayPal Help Center article 350 (read 2026-10-05)"
FEES_PAGE_DATE = "October 1, 2026"
SOURCE_FEES_PAGE = f"PayPal US business fees page dated {FEES_PAGE_DATE} (saved copy)"
SCHEDULE_LIMITATION = (f"Figures from PayPal's US business fees page dated {FEES_PAGE_DATE}; "
                      "your account's schedule may differ. Proofie has no Mexico fee schedule.")


@dataclass(frozen=True)
class Constant:
    name: str
    value: int | str
    display: str
    source: str


HIGH_VOLUME_MIN_SALES = Constant("High-volume minimum sales (strictly more than)", 100, "more than 100", SOURCE_HELP350)
HIGH_VOLUME_RATE_BPS = Constant("High-volume dispute rate (basis points)", 150, "1.50 % (150 bps)", SOURCE_HELP350)
STANDARD_FEE_CENTS = Constant("Standard Dispute Fee", 1500, "15.00 USD", SOURCE_FEES_PAGE)
HIGH_VOLUME_FEE_CENTS = Constant("High Volume Dispute Fee (not reimbursed even on a won appeal)", 3000,
                                 "30.00 USD", SOURCE_FEES_PAGE)
CURRENCY = Constant("Fee currency", "USD", "USD", SOURCE_FEES_PAGE)
WINDOW_MONTHS = Constant("Window (full calendar months before the month of the case)", 3, "3 months", SOURCE_HELP350)

ALL_CONSTANTS = (HIGH_VOLUME_MIN_SALES, HIGH_VOLUME_RATE_BPS, STANDARD_FEE_CENTS, HIGH_VOLUME_FEE_CENTS,
                 CURRENCY, WINDOW_MONTHS)


def cents_to_usd_text(cents: int) -> str:
    """1500 -> "15.00". Integer arithmetic only."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"
