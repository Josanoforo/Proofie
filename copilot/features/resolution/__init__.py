"""Resolution: offer, wait, talk, defend (behind COPILOT_FEATURE_RESOLUTION). Re-exports nothing that
imports routes or touches the network."""
from copilot.features.resolution.order import Actions, allowed_actions, is_settling, next_step_text

__all__ = ["Actions", "allowed_actions", "is_settling", "next_step_text"]
