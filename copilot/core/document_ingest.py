"""Document text extraction (PDF via pypdfium2; images are never OCR'd in v1: a document whose text could
not be extracted is marked as such, never invented - an image goes
straight to extraction_ok=False and the vendor fills in what it shows by hand, same as an unreadable PDF).
Type must already be validated by copilot.core.content_type.validate_upload before calling extract_text -
this module trusts the detected content type it's given, it does not re-sniff.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


class PdfReaderUnavailable(RuntimeError):
    """pypdfium2 is missing or broken: a problem with THIS install, never a property of the uploaded document."""

# Image-only or scanned PDF: a page with no real text layer still
# often yields a handful of stray characters from pypdfium2 (a watermark, a page number) - treating only
# a literally-empty string as failure missed that. Below this length the "text" is not enough to extract
# any real field from, so it is treated the same as a pure image: extraction_ok=False, never invented.
MIN_MEANINGFUL_TEXT_CHARS = 20


def extract_text(content_type: str, data: bytes) -> tuple[str | None, bool]:
    """Returns (extracted_text, extraction_ok). Never raises on a bad/unreadable file - that is exactly
    the case this function exists to report, not propagate as a crash. extraction_ok=False always pairs
    with extracted_text=None (never invent partial content for a failed extraction)."""
    if content_type != "application/pdf":
        return None, False
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        # with the dependency missing every PDF used to look unreadable, and the seller was told to
        # type values for a document that is fine. Reported apart, so the caller can say what is really wrong.
        logger.error("The PDF reader is not available: %s: %s", type(exc).__name__, exc)
        raise PdfReaderUnavailable("the PDF reader (pypdfium2) is not available") from exc
    try:
        pdf = pdfium.PdfDocument(io.BytesIO(data))
        parts = []
        for page in pdf:
            parts.append(page.get_textpage().get_text_range())
        text = "\n".join(parts)
    except Exception as exc:
        # the cause is logged (class and message only, never the document's content).
        logger.warning("A PDF could not be read: %s: %s", type(exc).__name__, exc)
        return None, False
    if len(text.strip()) < MIN_MEANINGFUL_TEXT_CHARS:
        return None, False
    return text, True
