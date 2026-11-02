"""Only the metadata missing from core approvals: individual human review snapshots."""
import json
import sqlite3
from contextlib import contextmanager

from copilot.db import repo
from copilot.features.claims.model import digest

DDL = """
CREATE TABLE IF NOT EXISTS active_approval_receipts (
    approval_id INTEGER PRIMARY KEY REFERENCES approvals(id),
    package_id INTEGER NOT NULL,
    review_sha256 TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    actor TEXT NOT NULL CHECK(actor = 'vendor'),
    phrase TEXT,
    reasons_json TEXT NOT NULL,
    confirmations_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_send_reservations (
    approval_id INTEGER PRIMARY KEY REFERENCES approvals(id),
    token TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    local_sha256 TEXT NOT NULL,
    review_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved', 'finished', 'changed'))
);
"""


@contextmanager
def connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def connection(db_path, conn=None):
    if conn is not None:
        yield conn
    else:
        with connect(db_path) as opened:
            yield opened


@contextmanager
def transaction(db_path):
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def local_snapshot(db_path, dispute_id, package_id, *, conn=None):
    """Hash a consistent snapshot of every local input to the review, without storing its text."""
    if conn is None:
        opened = sqlite3.connect(str(db_path))
        try:
            opened.execute("BEGIN")
            return local_snapshot(db_path, dispute_id, package_id, conn=opened)
        finally:
            opened.close()
    queries = (
        ("SELECT * FROM packages WHERE id=?", (package_id,)),
        ("SELECT * FROM claims WHERE package_id=? ORDER BY id", (package_id,)),
        ("SELECT * FROM documents WHERE dispute_id=? ORDER BY id", (dispute_id,)),
        ("SELECT c.* FROM document_confirmations c JOIN documents d ON d.id=c.document_id "
         "WHERE d.dispute_id=? ORDER BY c.id", (dispute_id,)),
        ("SELECT * FROM manual_fields WHERE dispute_id=?", (dispute_id,)),
        ("SELECT * FROM package_attachments WHERE package_id=? ORDER BY document_id", (package_id,)),
    )
    return digest([[list(row) for row in conn.execute(sql, args)] for sql, args in queries])


def previous(db_path, package_id, review_sha, *, conn=None):
    with connection(db_path, conn) as conn:
        row = conn.execute("SELECT a.* FROM approvals a JOIN active_approval_receipts r ON a.id=r.approval_id "
                           "WHERE r.package_id=? AND r.review_sha256=? AND a.invalidated_at IS NULL "
                           "AND NOT EXISTS (SELECT 1 FROM active_send_reservations s WHERE s.approval_id=a.id) "
                           "ORDER BY a.id DESC LIMIT 1", (package_id, review_sha)).fetchone()
    return dict(row) if row else None


def save(db_path, approval_id, package_id, review_sha, reasons, phrase, confirmations, *, conn=None):
    with connection(db_path, conn) as conn:
        conn.execute("INSERT INTO active_approval_receipts VALUES (?, ?, ?, ?, 'vendor', ?, ?, ?)",
                     (approval_id, package_id, review_sha, repo._now(), phrase or None,
                      json.dumps(reasons), json.dumps(confirmations, ensure_ascii=False, sort_keys=True)))


def receipt(db_path, approval_id):
    # Reading a page does not create a feature table.
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='active_approval_receipts'").fetchone():
            return None
        row = conn.execute("SELECT * FROM active_approval_receipts WHERE approval_id=?", (approval_id,)).fetchone()
    return dict(row) if row else None


def reservation_state(db_path, approval_id):
    """Observe a completed reservation without creating or changing feature tables."""
    with sqlite3.connect(str(db_path)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='active_send_reservations'").fetchone():
            return None
        row = conn.execute("SELECT state FROM active_send_reservations WHERE approval_id=?", (approval_id,)).fetchone()
    return row[0] if row else None
