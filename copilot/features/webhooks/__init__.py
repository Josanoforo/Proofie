"""Webhooks: an optional listener for PayPal's dispute events (behind COPILOT_FEATURE_WEBHOOKS). Re-exports only the
pure verification helpers, never the routes."""
from copilot.features.webhooks.verify import Verification, verification_body, verify_signature

__all__ = ["Verification", "verification_body", "verify_signature"]
