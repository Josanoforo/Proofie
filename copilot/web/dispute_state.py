"""Display predicates only; no reads, writes, action eligibility or feature imports."""
from copilot import config

BUYER_CLOSED_LABEL = "Canceled by the buyer (as stated by PayPal)"
# Shown in place of a message that invites adding or fixing proof while PayPal reviews the claim (no seller action exists).
REVIEWING_NEUTRAL_TEXT = "PayPal is reviewing this claim; nothing can be added now."


def is_buyer_closed(outcome_code: object) -> bool:
    return config.feature_enabled("outcome") and outcome_code == "CANCELED_BY_BUYER"


def buyer_closed_raw(raw: object) -> bool:
    outcome = raw.get("dispute_outcome") if isinstance(raw, dict) else None
    return is_buyer_closed(outcome.get("outcome_code") if isinstance(outcome, dict) else None)
