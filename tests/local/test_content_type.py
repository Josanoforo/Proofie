"""Offline tests for copilot/core/content_type.py: detection by content, never by extension, and PayPal's
documented size/type limits."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core import content_type as ct

PDF_BYTES = b"%PDF-1.7\n%demo data, not a real pdf structure\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF_BYTES = b"GIF89a" + b"\x00" * 20


def test_detects_pdf_png_jpeg_gif_by_magic_bytes():
    assert ct.detect_content_type(PDF_BYTES) == "application/pdf"
    assert ct.detect_content_type(PNG_BYTES) == "image/png"
    assert ct.detect_content_type(JPEG_BYTES) == "image/jpeg"
    assert ct.detect_content_type(GIF_BYTES) == "image/gif"


def test_unrecognised_content_returns_none_regardless_of_filename():
    assert ct.detect_content_type(b"not a real file at all, just text") is None


def test_validate_upload_rejects_mislabeled_extension_with_wrong_content():
    """A file named invoice.pdf but containing plain text must be rejected - type is checked by content,
    not trusted from the name."""
    problems = ct.validate_upload("invoice.pdf", b"this is not actually a PDF")
    assert problems and "must be a PDF, PNG, JPEG or GIF" in problems[0]    # One fixed sentence


def test_validate_upload_accepts_a_real_small_pdf():
    assert ct.validate_upload("label.pdf", PDF_BYTES) == []


def test_validate_upload_rejects_a_disallowed_but_recognised_type():
    # a type our magic-byte table doesn't recognise should fail cleanly, never crash
    problems = ct.validate_upload("archive.zip", b"PK\x03\x04" + b"\x00" * 20)
    assert problems


def test_validate_upload_rejects_file_at_or_over_the_10mb_limit():
    big = PDF_BYTES + b"\x00" * ct.MAX_FILE_BYTES
    problems = ct.validate_upload("big.pdf", big)
    assert any("10 MB" in p for p in problems)


def test_validate_upload_accepts_file_just_under_the_limit():
    just_under = PDF_BYTES + b"\x00" * (ct.MAX_FILE_BYTES - len(PDF_BYTES) - 1)
    assert len(just_under) < ct.MAX_FILE_BYTES
    assert ct.validate_upload("ok.pdf", just_under) == []


def test_validate_call_total_rejects_over_50mb_combined():
    problems = ct.validate_call_total([30 * 1024 * 1024, 25 * 1024 * 1024])
    assert problems and "50 MB" in problems[0]


def test_validate_call_total_accepts_under_50mb_combined():
    assert ct.validate_call_total([10 * 1024 * 1024, 10 * 1024 * 1024]) == []
