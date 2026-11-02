"""Offline tests for copilot/core/document_ingest.py. Builds a minimal real PDF by hand (a text operator
in its content stream - pypdfium2 cannot write text, only read it, same approach already proven working
in the install check) rather than depending on an external PDF generator.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.document_ingest import MIN_MEANINGFUL_TEXT_CHARS, extract_text


def _minimal_pdf_with_text(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 100]/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)


PDF_WITH_TEXT = _minimal_pdf_with_text("DEMO TRACKING DEMO8800123456")
GARBAGE_PDF = b"%PDF-1.4\nthis is not a valid pdf body at all, just garbage\n%%EOF"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 50


def test_extracts_real_text_from_a_minimal_pdf():
    text, ok = extract_text("application/pdf", PDF_WITH_TEXT)
    assert ok is True
    assert "DEMO TRACKING DEMO8800123456" in text


def test_garbage_pdf_bytes_fail_cleanly_not_invented_text():
    text, ok = extract_text("application/pdf", GARBAGE_PDF)
    assert ok is False and text is None


def test_image_content_types_are_never_ocrd_in_v1():
    """An image always reports extraction_ok=False (no OCR in v1); the vendor fills in what it shows
    by hand rather than the app guessing."""
    text, ok = extract_text("image/jpeg", JPEG_BYTES)
    assert ok is False and text is None


def test_unknown_or_unexpected_content_type_fails_cleanly():
    text, ok = extract_text("application/zip", b"PK\x03\x04")
    assert ok is False and text is None


def test_empty_text_pdf_reports_failure_not_empty_string_success():
    """A PDF that parses but yields nothing (e.g. a scanned image embedded with no text layer) must be
    extraction_ok=False, not a 'successful' empty extraction - those are different things for the UI/S7."""
    empty_pdf = _minimal_pdf_with_text("")
    text, ok = extract_text("application/pdf", empty_pdf)
    assert ok is False and text is None


def test_a_scrap_of_text_under_the_meaningful_length_is_also_unreadable():
    """A page with no real text layer can still yield a few stray characters (a
    watermark, a page number) - below MIN_MEANINGFUL_TEXT_CHARS that is not enough to extract any field
    from, so it is treated the same as a pure image, not a 'successful' extraction."""
    short_pdf = _minimal_pdf_with_text("x" * (MIN_MEANINGFUL_TEXT_CHARS - 1))
    text, ok = extract_text("application/pdf", short_pdf)
    assert ok is False and text is None

    long_pdf = _minimal_pdf_with_text("x" * MIN_MEANINGFUL_TEXT_CHARS)
    text, ok = extract_text("application/pdf", long_pdf)
    assert ok is True and text is not None
