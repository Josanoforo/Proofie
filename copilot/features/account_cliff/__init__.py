"""Account cliff: where the account stands against PayPal's High Volume Dispute Fee rule (reads only, no AI).
Off by default: COPILOT_FEATURE_ACCOUNT_CLIFF=true turns it on. Only `inbox_line` is re-exported, so importing
this package from routes_disputes never pulls in routes.py (which imports routes_disputes)."""
from copilot.features.account_cliff.service import inbox_line

__all__ = ["inbox_line"]
