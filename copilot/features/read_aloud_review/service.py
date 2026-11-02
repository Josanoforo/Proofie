"""Recover the exact note field from the same code builder used by the PayPal send flow."""
from copilot.core.send import build_request_input


def outgoing_notes(claims, tracking_number, carrier) -> str:
    body = build_request_input(claims, tracking_number, carrier)
    return body["evidences"][0]["notes"]
