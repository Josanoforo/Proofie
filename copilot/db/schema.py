"""Full SQLite schema for the app's own data (docs/ARCHITECTURE.md section 5), everything EXCEPT send_attempts
(that table is owned by copilot/core/attempt_store.py, which manages its own connection/locking so it can
be tested and reasoned about in isolation - see that module's docstring). All DDL here is idempotent
(CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS), safe to call on every app startup.

PayPal state (dispute status/reason/amount/deadlines/evidence received) is NEVER stored here as if it
were true - only app-owned data: uploaded documents, generated/edited packages, claims, which documents
are attached to a send, approvals, and the audit log. See docs/ARCHITECTURE.md section 3 "source of truth".
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    content_type    TEXT NOT NULL,       -- detected from file content, never trusted from the extension
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    extracted_text  TEXT,                -- NULL until extraction runs; '' is a legitimate empty result
    extraction_ok   INTEGER NOT NULL,    -- 0/1: did extraction succeed (never invent content on failure)
    excluded        INTEGER NOT NULL DEFAULT 0,  -- 0/1: vendor removed it from checklist/package input
                                                  -- (a checklist conflict's "remove the document" option);
                                                  -- the row and its history stay, only its use stops
    uploaded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_dispute ON documents(dispute_id);

CREATE TABLE IF NOT EXISTS packages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id          TEXT NOT NULL,
    version             INTEGER NOT NULL,     -- 1, 2, 3... per dispute; never overwritten (traceability)
    source              TEXT NOT NULL,        -- 'ai', 'human_edit', or 'auto_manual' (a still-empty
                                               -- shell auto_prep.py created automatically, nothing typed in)
    model               TEXT,                 -- NULL for a pure human_edit package built from scratch
    model_digest        TEXT,
    prompt_sha256       TEXT,
    raw_output          TEXT,                 -- the model's raw JSON text, if source='ai'
    validation_ok       INTEGER NOT NULL,
    effective_next_step TEXT,                 -- enforce_sufficiency()'s output, NULL if validation_ok=0
    review_flags_json   TEXT NOT NULL,        -- JSON list[str]
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packages_dispute ON packages(dispute_id);

CREATE TABLE IF NOT EXISTS claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id      INTEGER NOT NULL REFERENCES packages(id),
    evidence_index  INTEGER NOT NULL,
    claim_index     INTEGER NOT NULL,
    text            TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,         -- JSON list[str]
    basis           TEXT NOT NULL,         -- verified/seller_statement/buyer_statement/model_interpretation
    edited_by_human INTEGER NOT NULL DEFAULT 0,
    suggested_field TEXT,                  -- NULL unless this claim is a vision suggestion that maps to
                                            -- one of checklist.py's field names (copilot/core/vision_model.py)
    suggested_value TEXT                   -- the suggested value for suggested_field; NULL together with it.
                                            -- Confirming it (documents/{id}/confirm-reading) does not change
                                            -- this row - it writes document_confirmations below instead, so
                                            -- "what the model said" and "what the vendor confirmed" are two
                                            -- separate, independently-audited facts.
);
CREATE INDEX IF NOT EXISTS idx_claims_package ON claims(package_id);

CREATE TABLE IF NOT EXISTS document_confirmations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents(id),
    field_name    TEXT NOT NULL,          -- one of checklist.py's field names (copilot/core/
                                           -- checklist_input.py's MANUAL_FIELD_NAMES is the same vocabulary)
    value         TEXT NOT NULL,
    confirmed_by  TEXT NOT NULL CHECK (confirmed_by = 'vendor'),  -- never the model - enforced by the
                                                                   -- database itself)
    confirmed_at  TEXT NOT NULL,
    UNIQUE(document_id, field_name)       -- one current confirmed value per field per document; confirming
                                           -- again replaces it (INSERT OR REPLACE), it is not additive
);
CREATE INDEX IF NOT EXISTS idx_doc_confirmations_document ON document_confirmations(document_id);

CREATE TABLE IF NOT EXISTS package_attachments (
    package_id   INTEGER NOT NULL REFERENCES packages(id),
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    sha256       TEXT NOT NULL,            -- copied from documents.sha256 at attach time (immutable record)
    PRIMARY KEY (package_id, document_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id           INTEGER NOT NULL REFERENCES packages(id),
    approval_sha256      TEXT NOT NULL,
    approved_by          TEXT NOT NULL,
    approved_at          TEXT NOT NULL,
    invalidated_at       TEXT,             -- NULL while still valid; set, never deleted, once invalidated
    sufficiency_override TEXT              -- NULL unless the vendor used "Mark as seller statement and
                                            -- continue"; the human-readable reason
);
CREATE INDEX IF NOT EXISTS idx_approvals_package ON approvals(package_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL CHECK (actor IN ('vendor', 'system')),  -- never 'model': enforced by the
                                                                       -- database itself, not only by
                                                                       -- repo.py's Python check (defense
                                                                       -- in depth)
    action      TEXT NOT NULL,
    dispute_id  TEXT NOT NULL,
    detail      TEXT NOT NULL      -- JSON object, free-form per action
);
CREATE INDEX IF NOT EXISTS idx_audit_dispute ON audit_log(dispute_id);

-- Pre-dispute seller events belong to a real capture, never a fabricated dispute key.
CREATE TABLE IF NOT EXISTS sale_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL CHECK (actor = 'vendor'),
    action      TEXT NOT NULL CHECK (action = 'buyer_note_response'),
    capture_id  TEXT NOT NULL CHECK (length(capture_id) = 17 AND capture_id NOT GLOB '*[^A-Za-z0-9]*'),
    detail      TEXT NOT NULL CHECK (length(detail) BETWEEN 2 AND 256)
);
CREATE INDEX IF NOT EXISTS idx_sale_audit_capture ON sale_audit_log(capture_id, id);

CREATE TABLE IF NOT EXISTS manual_fields (
    dispute_id  TEXT PRIMARY KEY,
    fields_json TEXT NOT NULL,            -- JSON {name: {"value": ..., "confidence": "read"}}, vendor-typed
                                           -- shipment facts (checklist.py's field vocabulary); never
                                           -- overwrites a document-read value, only fills what no document gave
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_prep (
    dispute_id   TEXT PRIMARY KEY,
    version_hash TEXT NOT NULL,     -- fingerprint of the inputs this prep was built from (PayPal's
                                     -- update_time + the dispute's own uploaded-document sha256s/manual
                                     -- fields) - "a single preparation per dispute AND per data version"
    claim_token  TEXT NOT NULL,     -- a fresh value every time claim_auto_prep() wins,
                                     -- even for the SAME version_hash (a stale 'preparing' reclaim gets a
                                     -- new token) - set_auto_prep_result() requires it too, so a run that
                                     -- was reclaimed out from under it can never land its late result just
                                     -- because the version_hash still matches.
    status       TEXT NOT NULL CHECK (status IN ('preparing', 'ready', 'needs_documents', 'failed')),
    package_id   INTEGER REFERENCES packages(id),   -- set once status='ready'
    detail       TEXT,              -- plain-language note for the inbox (e.g. what is needed, or why it failed)
    updated_at   TEXT NOT NULL      -- also a heartbeat during 'preparing' (prepare_dispute() bumps this
                                     -- between stages) - see AUTO_PREP_STALE_PREPARING_AFTER_SECONDS
);
"""


def ensure_schema(db_path: Path | str, conn: sqlite3.Connection | None = None) -> None:
    """Creates every app-data table if missing. Pass an existing connection to run inside it (e.g. the
    same connection a repo function already opened); otherwise a short-lived connection is used."""
    owns_conn = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA)
        try:
            conn.execute("ALTER TABLE documents ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # a database created before this column existed already has it added on a prior run,
                 # or (the common case) CREATE TABLE above just created it fresh with the column already
        for alter in ("ALTER TABLE claims ADD COLUMN suggested_field TEXT",
                     "ALTER TABLE claims ADD COLUMN suggested_value TEXT",
                     "ALTER TABLE auto_prep ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"):
            try:
                conn.execute(alter)
            except sqlite3.OperationalError:
                pass  # same idempotent migration pattern as documents.excluded above
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()
