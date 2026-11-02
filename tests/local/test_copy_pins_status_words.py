"""The two templates that once labelled a status with replace('_', ' ') read it through a closed word table.

Each template is rendered by the real Jinja environment with a known value and an unknown one. An unknown value reads a
neutral word and never prints as the raw enum. Display only."""
import re

import pytest

from copilot.web.routes_disputes import templates

RAW_ENUMS = ("ZZ_NEW_STATUS", "zz new status", "zz_new_status")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _dossier(status: str) -> str:
    record = {"source": "PayPal's tracker listing", "tracking_number": "1Z999", "carrier": "FEDEX", "carrier_name": "FedEx",
              "status": status, "read_back_at": "2026-11-01T10:00:00", "sha256": "a" * 64, "note": "note"}
    report = {"source_label": "s", "reason_explanation": "r", "kind_explanation": "k", "proof_matrix": {"basis": "b", "items": []},
              "paypal_tracker_record": record, "chronology": [], "claims": [], "annexes": []}
    return _text(templates.env.get_template("features/evidence_dossier/index.html").render(
        request=None, samples={}, raw="", csrf="c", report=report, error=None))


def _ship_form(file_status: str) -> str:
    offer = {"state": "registered", "file_status": file_status, "file_href": "/fulfilment/X/file/1", "href": "/x", "fields": {}}
    return _text(templates.env.get_template("features/ship_moment_capture/form.html").render(
        request=None, capture_id="CAP000000000000001", record=None, offer=offer, dispute_id=None, from_package=None,
        case_documents=[], package_url=None, csrf_token="c", error=None))


@pytest.mark.parametrize("status, words", [("SHIPPED", "status: Shipped."), ("IN_TRANSIT", "status: In transit."),
                                           ("DELIVERED", "status: Delivered, as recorded by PayPal.")])
def test_dossier_reads_paypal_tracker_status_in_paypals_words(status, words):
    assert words in _dossier(status)


def test_dossier_unknown_tracker_status_reads_a_neutral_word():
    out = _dossier("ZZ_NEW_STATUS")
    assert "status: Not available." in out and not any(raw in out for raw in RAW_ENUMS)


@pytest.mark.parametrize("status, words", [("registered", "(Registered and confirmed)"),
                                           ("registered_unconfirmed", "(Accepted by PayPal, not confirmed)"),
                                           ("unknown", "(Send attempted - not confirmed by PayPal)")])
def test_ship_form_reads_the_tracking_file_status_from_the_closed_table(status, words):
    out = _ship_form(status)
    assert words in out and "registered_unconfirmed" not in out


def test_ship_form_unknown_file_status_reads_a_neutral_word():
    out = _ship_form("ZZ_NEW_STATUS")
    assert "(Status not recognized)" in out and not any(raw in out for raw in RAW_ENUMS)
