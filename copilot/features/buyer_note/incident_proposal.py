"""Deterministic local incident wording using resolved, attributed inputs."""


def initial_message(incident, locale, gap_context):
    """Prepare editable text only; callers must verify case, action and source freshness."""
    if incident not in {"delivered_not_received", "delay", "missing", "buyer_question"}:
        raise ValueError("Choose a supported issue.")
    if locale not in {"en", "es"}:
        raise ValueError("Choose a language explicitly.")
    if not isinstance(gap_context, dict) or gap_context.get("ok") is not True:
        raise ValueError("Case and sale context are unavailable.")
    openings = {
        "en": {"delivered_not_received": "I am reviewing your report that the shipment has not arrived.", "delay": "I am reviewing your report of a delay.", "missing": "I am reviewing the available details of the shipment.", "buyer_question": "Thank you for your question. I am reviewing the information available for this sale."},
        "es": {"delivered_not_received": "Estoy revisando tu reporte de que el envío no ha llegado.", "delay": "Estoy revisando tu reporte de retraso.", "missing": "Estoy revisando los datos disponibles del envío.", "buyer_question": "Gracias por tu consulta. Estoy revisando la información disponible de esta venta."},
    }
    parts = [openings[locale][incident]]
    fields = gap_context.get("fields") or {}
    for name, labels in (("carrier", ("Recorded carrier", "Transportista registrado")), ("tracking_number", ("Recorded tracking reference", "Guía registrada"))):
        row = fields.get(name) or {}
        value = row.get("value")
        if row.get("status") in {"confirmed_by_seller", "recorded_provider"} and isinstance(value, str) and value.strip():
            parts.append(f"{labels[0 if locale == 'en' else 1]}: {value}.")
    parts.append("I will share an update after reviewing the recorded details." if locale == "en" else "Compartiré una actualización después de revisar los datos registrados.")
    return " ".join(parts)
