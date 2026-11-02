"""Synthetic ledger schema v1, a frozen copy; no optional-feature imports."""
import json
import sqlite3

SCHEMA_SOURCE = "83efa1c40a9484ef5133972d2f311e8705f7d8ce"
RECEIPT_DDL = """
CREATE TABLE IF NOT EXISTS active_approval_receipts (
 approval_id INTEGER PRIMARY KEY REFERENCES approvals(id), package_id INTEGER NOT NULL,
 review_sha256 TEXT NOT NULL, confirmed_at TEXT NOT NULL,
 actor TEXT NOT NULL CHECK(actor = 'vendor'), phrase TEXT,
 reasons_json TEXT NOT NULL, confirmations_json TEXT NOT NULL
);
"""
DRAFT_DDL = """
CREATE TABLE IF NOT EXISTS draft_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, dispute_id TEXT,
 transaction_id TEXT, sheet_json TEXT NOT NULL, en TEXT NOT NULL, es TEXT NOT NULL,
 model TEXT NOT NULL, seed INTEGER NOT NULL, raw_sha256 TEXT, validation_ok INTEGER NOT NULL,
 problems_json TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('model','edited'))
);
"""
OUTCOME_DDL = """
CREATE TABLE IF NOT EXISTS outcome_appeals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, dispute_id TEXT NOT NULL, created_at TEXT NOT NULL,
 request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL, approval_sha256 TEXT NOT NULL,
 readback_json TEXT NOT NULL, status TEXT NOT NULL, limitation_text TEXT
);
"""
RESOLUTION_DDL = """
CREATE TABLE IF NOT EXISTS resolution_actions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, dispute_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('offer','message')), created_at TEXT NOT NULL,
 request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL, approval_sha256 TEXT NOT NULL,
 readback_json TEXT NOT NULL, settled INTEGER NOT NULL, status TEXT NOT NULL, limitation_text TEXT
);
"""
WEBHOOK_DDL = """
CREATE TABLE IF NOT EXISTS webhook_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, event_id TEXT NOT NULL,
 event_type TEXT NOT NULL, resource_type TEXT, dispute_id TEXT, transmission_id TEXT,
 body_sha256 TEXT NOT NULL, verification TEXT NOT NULL, readback_status TEXT,
 readback_sha256 TEXT, limitation_text TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_event_id ON webhook_events(event_id);
CREATE INDEX IF NOT EXISTS idx_webhook_dispute ON webhook_events(dispute_id);
"""
ATTEMPT_DDL = """
CREATE TABLE IF NOT EXISTS send_attempts (
 dispute_id TEXT NOT NULL, package_sha256 TEXT NOT NULL, state TEXT NOT NULL,
 request_id TEXT, http_status INTEGER, reason TEXT, request_input_json TEXT,
 attachment_filenames_json TEXT, ts TEXT NOT NULL,
 PRIMARY KEY(dispute_id, package_sha256)
);
"""
OPTIONAL_DDLS = (DRAFT_DDL, OUTCOME_DDL, RESOLUTION_DDL, WEBHOOK_DDL, ATTEMPT_DDL)

def save_receipt(db, approval_id, package_id, review_sha, reasons, phrase, confirmations):
    with sqlite3.connect(db) as conn:
        conn.executescript(RECEIPT_DDL)
        conn.execute("INSERT INTO active_approval_receipts VALUES (?, ?, ?, ?, 'vendor', ?, ?, ?)",
                     (approval_id, package_id, review_sha, "2026-10-01T10:00:00Z", phrase or None,
                      json.dumps(reasons), json.dumps(confirmations, ensure_ascii=False, sort_keys=True)))
