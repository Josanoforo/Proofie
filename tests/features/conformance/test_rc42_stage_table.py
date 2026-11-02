"""The stage table of copilot/web/presentation.py is exactly PayPal's pinned dispute_lifecycle_stage enum,
in the schema's order (lives with the conformance tests: it reads the pinned schema fixture, which the flags-off export omits)."""
import json
from pathlib import Path

from copilot.web import presentation

FIXTURE = Path(__file__).resolve().parents[3] / "copilot" / "features" / "conformance" / "fixtures" / "disputes.json"


def test_the_stage_table_equals_the_pinned_schema_enum():
    schema = json.loads(FIXTURE.read_text(encoding="utf-8"))["components"]["schemas"]["dispute_lifecycle_stage"]["enum"]
    assert [value for value, _ in presentation.STAGE_TABLE] == schema
    assert "CLAIM" not in schema and "CLAIM" in presentation.STAGE_LEGACY_ALIASES
