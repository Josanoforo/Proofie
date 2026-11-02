"""Deterministic guidance; never changes the package checklist or infers a PayPal fact."""

from __future__ import annotations

import re


def guidance_for(saved_status: str | None) -> dict | None:
    """Return seller guidance for a saved status label, or None when it is not recognized."""
    value = re.sub(r"\s+", " ", str(saved_status or "")).strip().casefold()
    if any(token in value for token in ("in transit", "en tránsito", "en transito", "in_transit", "shipped", "enviado")):
        return {
            "status": "in_transit",
            "heading": "Shipment shown as in transit",
            "body": "Keep the carrier's acceptance and movement records, including any delay or exception notice. Use the saved status as entered; it does not show delivery.",
        }
    if any(token in value for token in ("delivered", "entregado", "entregada")) and not any(
            token in value for token in ("not delivered", "no entregado", "no entregada")):
        return {
            "status": "delivered",
            "heading": "Shipment shown as delivered",
            "body": "Review the delivery record, available proof of delivery, recipient/location details, and any mismatch the buyer reported. Availability depends on the carrier and purchased service; this saved status alone is not independent proof.",
        }
    return None
