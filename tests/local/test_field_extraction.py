"""Offline tests for copilot/core/field_extraction.py, against the real demo fixtures in
copilot/fixtures/m1/*.txt (synthetic documents) plus a few edge cases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.field_extraction import extract_fields

FIX = Path(__file__).resolve().parents[2] / "copilot" / "fixtures" / "m1"


def test_empty_or_none_text_is_simply_absent():
    assert extract_fields(None) == {}
    assert extract_fields("   ") == {}


def test_unlabelled_free_text_extracts_nothing_rather_than_guessing():
    assert extract_fields("Thanks for your order! We hope you enjoy it.") == {}


def test_estafeta_capture_fixture():
    text = (FIX / "doc_estafeta_capture.txt").read_text(encoding="utf-8")
    f = extract_fields(text)
    assert f["tracking_number"]["value"] == "DEMO-EST-000005"
    assert "transito" in f["delivery_status"]["value"].lower() or "tránsito" in f["delivery_status"]["value"]
    assert f["ship_date"]["value"] == "2026-09-11"
    # The fixture's "Fecha de entrega: NULL" and "Nombre de quien recibe: NULL"
    # have the label present with an uninterpretable value - illegible (present_unverifiable downstream in
    # checklist.py), never "missing" (which would wrongly claim the document said nothing about them).
    assert f["delivery_date"] == {"value": None, "confidence": "illegible"}
    assert f["receiver_name"] == {"value": None, "confidence": "illegible"}
    assert f["capture_date"]["value"] == "2026-09-30"
    assert f["carrier"]["value"].lower() == "estafeta"
    assert f["postal_code"]["value"] == "06600"


def test_fedex_letter_fixture():
    text = (FIX / "doc_fedex_letter.txt").read_text(encoding="utf-8")
    f = extract_fields(text)
    assert f["tracking_number"]["value"] == "DEMO-FDX-000001"
    assert f["ship_date"]["value"] == "2026-09-11"
    assert f["delivery_date"]["value"] == "2026-09-14"
    assert f["receiver_name"]["value"] == "Ana Demo"
    assert f["signature"]["value"] == "online"
    assert f["carrier"]["value"].lower() == "fedex"
    assert f["street"]["value"].startswith("Calle Falsa 123")
    assert f["city"]["value"] == "Ciudad de Mexico"
    assert f["state"]["value"] == "CDMX"
    assert f["postal_code"]["value"] == "06600"
    assert f["delivery_type"]["value"] == "home"


def test_aggregator_receipt_fixture_is_a_branch_pickup():
    text = (FIX / "doc_aggregator_receipt.txt").read_text(encoding="utf-8")
    f = extract_fields(text)
    assert f["tracking_number"]["value"] == "DEMO-SKY-000010"
    assert f["delivery_type"]["value"] == "branch_pickup"
    assert f["city"]["value"] == "Cuernavaca"
    assert f["state"]["value"] == "Morelos"
    assert f["postal_code"]["value"] == "62000"
    assert f["carrier"]["value"].lower() == "skydropx"


def test_every_emitted_field_is_read_or_illegible_never_a_blank_read():
    for fx in ("doc_estafeta_capture.txt", "doc_fedex_letter.txt", "doc_aggregator_receipt.txt"):
        f = extract_fields((FIX / fx).read_text(encoding="utf-8"))
        for name, v in f.items():
            assert v["confidence"] in ("read", "illegible"), name
            if v["confidence"] == "read":
                assert v["value"], name
            else:
                assert v["value"] is None, name


def test_usps_is_an_extracted_display_hint_only():
    f = extract_fields("Carrier: USPS\nTracking number: 9400111899223856928491")
    assert f["carrier"]["value"] == "USPS"

def test_id_de_captura_is_not_a_capture_date():
    f = extract_fields("ID de captura: 2026102112345678")
    assert "capture_date" not in f

def test_explicit_capture_date_still_extracts():
    f = extract_fields("Fecha de captura: 2026-10-21")
    assert f["capture_date"]["value"] == "2026-10-21"
