import pytest
from copilot.features.buyer_note.incident_proposal import initial_message


def test_explicit_locale_and_unresolved_sources():
    context = {"ok": True, "fields": {"tracking_number": {"status": "confirmed_by_seller", "value": "EDITED-1"}, "carrier": {"status": "conflict", "value": None}}}
    result = initial_message("buyer_question", "es", context)
    assert "EDITED-1" in result and "consulta" in result
    assert "Transportista registrado" not in result
    with pytest.raises(ValueError): initial_message("delay", "unknown", context)
    with pytest.raises(ValueError): initial_message("delay", "en", {"ok": False})
