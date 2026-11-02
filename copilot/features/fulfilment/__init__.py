"""Fulfilment: the pre-dispute file born at the sale (behind COPILOT_FEATURE_FULFILMENT).
Re-exports only the lookup so importing the package never imports routes."""
from copilot.features.fulfilment.lookup import file_for_transaction

__all__ = ["file_for_transaction"]
