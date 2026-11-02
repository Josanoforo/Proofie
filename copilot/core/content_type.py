"""Content-type detection by magic bytes (never by filename extension) and validation of upload/attachment
limits against PayPal's documented values
(developer.paypal.com/disputes/supported-file-sizes-types, read 2026-10-04):
types .jpg/.jpeg/.gif/.png/.pdf, under 10 MB per file, up to 50 MB total per provide-evidence call.
"""
from __future__ import annotations

_MAGIC = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# PayPal's supported evidence file types; values are what detect_content_type() returns above.
ALLOWED_CONTENT_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif"}
MAX_FILE_BYTES = 10 * 1024 * 1024        # strictly under 10 MB per file ("< 10 MB")
MAX_CALL_BYTES = 50 * 1024 * 1024        # up to 50 MB total per provide-evidence call


def detect_content_type(data: bytes) -> str | None:
    """Sniffs the first bytes against known magic numbers. None if nothing recognised (never guess from
    a filename extension - an attacker or a mislabeled file can put any extension on any content)."""
    for magic, content_type in _MAGIC:
        if data.startswith(magic):
            return content_type
    return None


# the seller sees one fixed sentence per problem (no MIME lists, no byte counts, no echo of the file
# name). The codes are what the checks return; the sentences are the only text that reaches a page.
PROBLEM_NO_FILE = "no_file"
PROBLEM_TYPE = "type"
PROBLEM_TOO_LARGE = "too_large"
PROBLEM_EMPTY = "empty"
PROBLEM_UNREADABLE = "unreadable"
PROBLEM_TOTAL = "total"

PROBLEM_MESSAGES = {
    PROBLEM_NO_FILE: "Select a document to upload",
    PROBLEM_TYPE: "The selected file must be a PDF, PNG, JPEG or GIF",
    PROBLEM_TOO_LARGE: "The selected file must be smaller than 10 MB",
    PROBLEM_EMPTY: "The selected file is empty",
    PROBLEM_UNREADABLE: "The file could not be uploaded. Choose the file again.",
    PROBLEM_TOTAL: "Files in the response must be 50 MB or less in total. Remove a file first.",
}


def problem_message(code: str) -> str:
    return PROBLEM_MESSAGES.get(code, PROBLEM_MESSAGES[PROBLEM_UNREADABLE])


def upload_problem_codes(data: bytes) -> list[str]:
    """One file, checked by its actual sniffed content and real size. Empty list = OK."""
    if not data:
        return [PROBLEM_EMPTY]
    codes = []
    content_type = detect_content_type(data)
    if content_type is None or content_type not in ALLOWED_CONTENT_TYPES:
        codes.append(PROBLEM_TYPE)
    if len(data) >= MAX_FILE_BYTES:
        codes.append(PROBLEM_TOO_LARGE)
    return codes


def validate_upload(filename: str, data: bytes) -> list[str]:
    """The fixed sentences for upload_problem_codes(data). `filename` is accepted for the old signature and never
    appears in a message. Empty list = OK."""
    return [problem_message(code) for code in upload_problem_codes(data)]


def total_problem_codes(sizes_bytes: list[int]) -> list[str]:
    return [PROBLEM_TOTAL] if sum(sizes_bytes) > MAX_CALL_BYTES else []


def validate_call_total(sizes_bytes: list[int]) -> list[str]:
    """All attachments on a single provide-evidence call together. Empty list = OK."""
    return [problem_message(code) for code in total_problem_codes(sizes_bytes)]
