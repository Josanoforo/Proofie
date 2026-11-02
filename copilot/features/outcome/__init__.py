"""Outcome: the file outlives the case (behind COPILOT_FEATURE_OUTCOME). Re-exports only the pure reader."""
from copilot.features.outcome.read import OutcomeRead, read_outcome

__all__ = ["OutcomeRead", "read_outcome"]
