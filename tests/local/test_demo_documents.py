"""Offline smoke tests for demo/documents/ - confirms each shipped file still
behaves the way demo/documents/README.md describes, so a future change to content_type.py/
document_ingest.py/field_extraction.py that breaks one of them is caught here, not by a judge.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.content_type import detect_content_type, validate_upload
from copilot.core.document_ingest import extract_text
from copilot.core.field_extraction import extract_fields

DEMO = Path(__file__).resolve().parents[2] / "demo" / "documents"


def _extracted(name: str) -> dict:
    data = (DEMO / name).read_bytes()
    content_type = detect_content_type(data)
    assert validate_upload(name, data) == []   # every demo file must pass real upload validation
    text, ok = extract_text(content_type, data)
    assert ok is True, name
    return extract_fields(text)


def test_coherent_delivery_is_fully_readable():
    f = _extracted("01_coherent_delivery.pdf")
    assert f["tracking_number"]["value"] == "DEMO-FDX-000001"
    assert date.fromisoformat(f["delivery_date"]["value"]) == date.fromisoformat(f["ship_date"]["value"]) + timedelta(days=2)
    assert date.fromisoformat(f["capture_date"]["value"]) == date.fromisoformat(f["delivery_date"]["value"]) + timedelta(days=1)
    assert f["receiver_name"]["value"] == "Ana Demo"


def test_wrong_order_document_has_a_different_tracking_and_postal_code():
    f = _extracted("02_wrong_order_tracking_and_postal_code.pdf")
    assert f["tracking_number"]["value"] == "DEMO-EST-999999"
    assert f["postal_code"]["value"] == "03100"


def test_in_transit_document_shows_in_transit_not_delivered():
    f = _extracted("03_in_transit.pdf")
    # checklist.py's own _plain()/_TRANSIT matching is accent-insensitive; checked the same way here so
    # this test never depends on this shell correctly displaying "tránsito" - only on ord() values.
    status = f["delivery_status"]["value"].lower()
    assert status.startswith("en tr") and status.endswith("nsito") and len(status) == 11


def test_injected_instruction_document_still_reads_its_real_fields():
    f = _extracted("04_injected_instruction.pdf")
    assert f["tracking_number"]["value"] == "DEMO-FDX-000001"
    assert "transito" in f["delivery_status"]["value"].lower()


def test_illegible_scan_is_a_valid_image_that_extracts_no_text():
    data = (DEMO / "05_illegible_scan.png").read_bytes()
    assert detect_content_type(data) == "image/png"
    assert validate_upload("05_illegible_scan.png", data) == []
    text, ok = extract_text("image/png", data)
    assert ok is False and text is None
