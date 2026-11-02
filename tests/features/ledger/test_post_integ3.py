"""An unverified event must not look like a verification result."""
from copilot.features.ledger import service


def test_unverified_webhook_is_neutral_and_keeps_its_value():
    record = service._project("webhook_events", {"ledger_rowid": 1, "verification": "unverified"})
    assert record["metadata"]["verification"] == "unverified"
    assert record["label"] == "Recorded webhook event"
