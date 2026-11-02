"""Read existing records without creating tables or reconstructing missing history."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from copilot.core.checklist_input import MANUAL_FIELD_NAMES
from copilot.web.security import validate_dispute_id

_SHA = re.compile(r'[a-fA-F0-9]{64}')
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})')
_ENUMS = frozenset(('vendor', 'system', 'ai', 'human_edit', 'auto_manual', 'model', 'edited',
    'verified', 'seller_statement', 'buyer_statement', 'model_interpretation', 'unverifiable',
    'conflict', 'missing', 'present_unverifiable', 'not_applicable', 'confirmed', 'failed',
    'pending', 'sent_unconfirmed', 'blocked', 'abandoned', 'preparing', 'ready', 'needs_documents',
    'sent_confirmed', 'sent_pending_settle', 'refused_422', 'offer', 'message', 'rejected_headers',
    'limitation', 'unverified', 'published_policy', 'case_request', 'app_quality_rule', 'none'))
_TIMES = {'documents': 'uploaded_at', 'packages': 'created_at', 'document_confirmations': 'confirmed_at',
    'approvals': 'approved_at', 'audit_log': 'ts', 'manual_fields': 'updated_at', 'auto_prep': 'updated_at',
    'send_attempts': 'ts', 'draft_messages': 'created_at', 'outcome_appeals': 'created_at',
    'resolution_actions': 'created_at', 'webhook_events': 'received_at', 'active_approval_receipts': 'confirmed_at'}
_LABELS = {'documents': 'Uploaded document (current exclusion status)', 'packages': 'Prepared package',
    'claims': 'Current statement text', 'document_confirmations': 'Current confirmed reading',
    'package_attachments': 'Current attached document', 'approvals': 'Local package approval',
    'audit_log': 'Recorded app action', 'manual_fields': 'Current seller-entered fields',
    'auto_prep': 'Current preparation result', 'send_attempts': 'Current send-attempt record',
    'draft_messages': 'Prepared message', 'outcome_appeals': 'Recorded appeal attempt',
    'resolution_actions': 'Recorded resolution attempt', 'webhook_events': 'Recorded webhook event',
    'active_approval_receipts': 'Individual statement review'}
_JOIN = {'claims': ('packages', 'package_id'), 'package_attachments': ('packages', 'package_id'),
    'approvals': ('packages', 'package_id'), 'active_approval_receipts': ('packages', 'package_id'),
    'document_confirmations': ('documents', 'document_id')}
_HASH_FIELDS = ('sha256', 'approval_sha256', 'request_sha256', 'readback_sha256', 'body_sha256',
    'review_sha256', 'prompt_sha256', 'raw_sha256', 'model_digest', 'version_hash', 'package_sha256')
_DIGEST_FIELDS = ('text', 'extracted_text', 'raw_output', 'review_flags_json', 'fields_json', 'value',
    'sufficiency_override', 'detail', 'request_input_json', 'request_json', 'readback_json', 'sheet_json',
    'en', 'es', 'model', 'problems_json', 'reasons_json', 'source_ids_json', 'reason', 'limitation_text',
    'event_id', 'event_type', 'transmission_id', 'confirmations_json', 'suggested_value')
_INT_FIELDS = ('package_id', 'document_id', 'approval_id', 'claim_index', 'evidence_index', 'version',
    'size_bytes', 'extraction_ok', 'excluded', 'validation_ok', 'edited_by_human', 'settled', 'http_status')
_ENUM_FIELDS = ('actor', 'approved_by', 'confirmed_by', 'basis', 'source', 'status', 'state', 'action', 'verification')
_LIMITS = (
    'This record shows stored records and audit entries, not a complete event history.',
    'Earlier statement text, replaced manual readings and attachment removals cannot be recovered from current rows.',
    'A local approval hash is not proof that PayPal received evidence. Remote status is only a stored readback.',
    'Fulfillment files are not included: no proven dispute association exists in that store.',
    'Private text, request bodies and other identifiers appear only as fingerprints (SHA-256), not copied into this record.',
    'Only calls naming this exact dispute are included; account and transaction lookups cannot be tied to one dispute here.',
)


def canonical(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                          default=str).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def stamp(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _hash(value):
    if isinstance(value, str) and _SHA.fullmatch(value):
        return value.lower()
    return None


def _enum(value):
    return value if isinstance(value, str) and value in _ENUMS else '[redacted]'


def _object(text, fallback):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def _confirmations(text):
    units = _object(text, [])
    if not isinstance(units, list):
        return []
    out = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        sources = []
        for src in unit.get('sources', []) if isinstance(unit.get('sources'), list) else []:
            if isinstance(src, dict):
                field = src.get('field')
                sources.append({'artifact_id_sha256': digest(src.get('artifact_id')),
                    'field': field if field in MANUAL_FIELD_NAMES else '[redacted]',
                    'sha256': _hash(src.get('sha256')), 'value_sha256': _hash(src.get('value_sha256')),
                    'check_basis': _enum(src.get('check_basis')), 'check_state': _enum(src.get('check_state'))})
        out.append({'claim_id': _integer(unit.get('claim_id')), 'sentence_sha256': _hash(unit.get('sentence_sha256')),
                    'claim_basis': _enum(unit.get('claim_basis')), 'verdict': _enum(unit.get('verdict')),
                    'decision': _enum(unit.get('decision')), 'sources': sources})
    return out


def _project(table, row):
    metadata = {k: _integer(row[k]) for k in _INT_FIELDS if k in row}
    metadata.update({k: _enum(row[k]) for k in _ENUM_FIELDS if k in row})
    metadata.update({k: _hash(row[k]) for k in _HASH_FIELDS if k in row})
    metadata.update({k + '_stored_value_sha256': digest(row[k]) for k in _DIGEST_FIELDS
                     if k in row and row[k] is not None})
    if table == 'approvals':
        metadata['invalidated_at'] = stamp(row.get('invalidated_at'))
        metadata['invalidated'] = row.get('invalidated_at') is not None
    if table == 'audit_log':
        detail = _object(row.get('detail'), {})
        if isinstance(detail, dict):
            metadata['identifiers'] = {k: _integer(detail[k]) for k in _INT_FIELDS if k in detail}
            if detail.get('via') == 'mcp':
                metadata['via'] = 'mcp'          # a row written for a connected assistant is not Proofie's own
        metadata['action_sha256'] = digest(row.get('action'))
    if table == 'manual_fields':
        fields = _object(row.get('fields_json'), {})
        metadata['fields'] = {k: digest(v) for k, v in fields.items() if k in MANUAL_FIELD_NAMES} if isinstance(fields, dict) else {}
    if table == 'active_approval_receipts':
        metadata['confirmations'] = _confirmations(row.get('confirmations_json'))
        metadata['typed_confirmation_recorded'] = row.get('phrase') is not None
    if table == 'webhook_events':
        metadata['readback_status_sha256'] = digest(row.get('readback_status'))
    actor = row.get('actor', row.get('approved_by', row.get('confirmed_by')))
    if table == 'claims' and row.get('edited_by_human') == 1:
        actor = 'vendor'
    return {'origin': table, 'record_id': _integer(row.get('ledger_rowid')), 'label': _LABELS[table],
            'timestamp': stamp(row.get(_TIMES.get(table))), 'actor': _enum(actor) if actor else 'unknown',
            'metadata': metadata}


def configured_evidence():
    """Explicit file binding only; never discover files by scanning a folder."""
    root = os.environ.get('PROOFIE_EVIDENCE_DIR', '').strip()
    names = _object(os.environ.get('PROOFIE_LEDGER_EVIDENCE_FILES', '[]'), None)
    if not root and not names:
        return ()
    if not root or not isinstance(names, list) or any(not isinstance(n, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+\.jsonl', n) or '..' in n for n in names):
        raise ValueError('The evidence file binding is not valid.')
    folder = Path(root).absolute()
    return tuple(folder / name for name in sorted(set(names)))


def _linked(path):
    return any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (path, *path.parents))


def _calls(files, dispute_id, limitations):
    events = []
    if not files:
        limitations.append('No sanitized call files were explicitly bound; call history is unavailable.')
    prefix = f'/v1/customer/disputes/{dispute_id}'
    for file in files:
        path = Path(file)
        if _linked(path) or not path.is_file():
            limitations.append('A bound sanitized call file is unavailable or linked; it was not read.')
            continue
        with path.open('rb') as handle:
            for line_number, line in enumerate(handle, 1):
                if len(line) > 2_000_000:
                    limitations.append('A call record exceeded the read limit and was omitted.')
                    continue
                record = _object(line, None)
                if not isinstance(record, dict):
                    limitations.append('A call record was malformed and was omitted.')
                    continue
                endpoint = record.get('path')
                if not isinstance(endpoint, str) or not re.fullmatch(re.escape(prefix) + r'(?:/[a-z][a-z-]{0,40})?', endpoint):
                    continue
                method = record.get('method')
                metadata = {'method': method if method in ('GET', 'POST', 'PATCH', 'DELETE', 'PUT') else '[redacted]',
                    'http_status': _integer(record.get('status')), 'source_line': line_number,
                    'source_line_sha256': hashlib.sha256(line).hexdigest(),
                    'source_file_id_sha256': digest(path.name),
                    'op_sha256': digest(record.get('op')), 'request_id_sha256': digest(record.get('request_id')),
                    'debug_id_sha256': digest(record.get('debug_id')), 'issue_sha256': digest(record.get('issue')),
                    'sanitized_payload_sha256': digest(record.get('body'))}
                events.append({'origin': 'sanitized_calls', 'record_id': line_number, 'label': 'Recorded sandbox call',
                    'timestamp': stamp(record.get('ts')), 'actor': 'unknown', 'metadata': metadata})
    return events


def read(db_path, dispute_id, evidence_files=()):
    problem = validate_dispute_id(dispute_id)
    if problem:
        raise ValueError('Open a valid dispute identifier.')
    path = Path(db_path).absolute()
    limitations, events, available = list(_LIMITS), [], []
    if _linked(path):
        raise ValueError('The database location is linked; the record cannot be read safely.')
    if not path.is_file():
        limitations.append('No local database exists for this record; it was not created.')
    else:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in _LABELS:
                if table not in present:
                    limitations.append(f'{table}: no stored source table; history unavailable.')
                    continue
                if table in _JOIN:
                    parent, key = _JOIN[table]
                    sql = f'SELECT t.rowid AS ledger_rowid, t.* FROM {table} t JOIN {parent} p ON p.id=t.{key} WHERE p.dispute_id=?'
                    if table == 'package_attachments':
                        sql += ' AND EXISTS (SELECT 1 FROM documents d WHERE d.id=t.document_id AND d.dispute_id=p.dispute_id)'
                    if table == 'active_approval_receipts':
                        sql += ' AND EXISTS (SELECT 1 FROM approvals a WHERE a.id=t.approval_id AND a.package_id=p.id)'
                else:
                    sql = f'SELECT t.rowid AS ledger_rowid, t.* FROM {table} t WHERE t.dispute_id=?'
                try:
                    rows = conn.execute(sql, (dispute_id,)).fetchall()
                    events.extend(_project(table, dict(row)) for row in rows)
                    available.append(table)
                except (sqlite3.DatabaseError, ValueError, TypeError):
                    limitations.append(f'{table}: stored source shape could not be read; no data inferred.')
        finally:
            conn.close()
    events.extend(_calls(evidence_files, dispute_id, limitations))
    events.sort(key=lambda e: (e['timestamp'] is None, e['timestamp'] or '', e['origin'],
                               e['record_id'] if e['record_id'] is not None else -1,
                               digest(e['metadata'])))
    return {'schema': 'proofie.ledger.v1', 'dispute_id': dispute_id, 'available_sources': sorted(available),
            'records': events, 'limitations': sorted(set(limitations))}


def export(data):
    """Build the canonical download and its hash in memory; never write a file."""
    body = canonical(data)
    return body, hashlib.sha256(body).hexdigest()
