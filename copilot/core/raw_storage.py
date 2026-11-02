"""Raw uploaded file bytes, content-addressed by sha256 under copilot.config.UPLOADS_DIR (a real
send needs the original bytes back to attach a file; copilot/db/schema.py's documents table only ever
stored the extracted TEXT and that hash, by design, so this is a second, independent store keyed purely
by content - no document_id, no dispute_id, so two documents with identical bytes share one file."""
from __future__ import annotations

from pathlib import Path


def write_raw(uploads_dir: Path, sha256: str, data: bytes) -> None:
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / sha256).write_bytes(data)


def read_raw(uploads_dir: Path, sha256: str) -> bytes | None:
    path = uploads_dir / sha256
    return path.read_bytes() if path.exists() else None


__all__ = ["write_raw", "read_raw"]
