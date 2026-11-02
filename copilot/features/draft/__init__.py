"""Draft: a bilingual message draft by the local model, validated by code, reviewed by the seller (behind
COPILOT_FEATURE_DRAFT). Re-exports only the pure fact sheet and validator; never the routes and never the model."""
from copilot.features.draft.check import Validation, validate_draft
from copilot.features.draft.facts import FactSheet, fact_sheet

__all__ = ["FactSheet", "Validation", "fact_sheet", "validate_draft"]
