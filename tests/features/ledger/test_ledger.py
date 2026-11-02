import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from _hosted import authenticated_client

from copilot import config
from copilot.app import create_app
from copilot.db import repo
from .fixture_schemas import save_receipt, OPTIONAL_DDLS
from copilot.features.ledger import service

DID = 'PP-R-DEMO-0000001'
OTHER = 'PP-R-DEMO-0000002'
SHA = 'a' * 64
PRIVATE = 'demo@example.com Bearer DEMO-ONLY PayPal-Request-Id: DEMO-ONLY'


@pytest.fixture
def records():
    db = config.db_path()
    package = repo.insert_package(db, DID, 'ai', True, [PRIVATE], model='qwen-demo', raw_output=PRIVATE)
    doc = repo.insert_document(db, DID, PRIVATE, 'application/pdf', 25, SHA, PRIVATE, True)
    repo.insert_claims(db, package, [{'evidence_index': 0, 'claim_index': 0, 'text': PRIVATE,
                                   'source_ids': [PRIVATE], 'basis': 'seller_statement'}])
    claim = repo.list_claims(db, package)[0]['id']
    repo.mark_claim_edited(db, claim, PRIVATE + ' edited')
    repo.confirm_document_reading(db, doc, 'tracking_number', PRIVATE)
    repo.attach_document(db, package, doc)
    aid = repo.insert_approval(db, package, SHA, 'vendor', PRIVATE)
    save_receipt(db, aid, package, SHA, [PRIVATE], PRIVATE, [{'claim_id': claim,
        'sentence_sha256': SHA, 'claim_basis': 'seller_statement', 'verdict': 'unverifiable',
        'decision': 'seller_statement', 'sources': [{'artifact_id': PRIVATE, 'field': PRIVATE,
        'sha256': SHA, 'value_sha256': SHA, 'check_basis': 'published_policy', 'check_state': 'missing'}]}])
    repo.log_action(db, 'vendor', 'edit_claim', DID, {'package_id': package, 'note': PRIVATE,
                    'links': [{'href': PRIVATE}]})
    other = repo.insert_package(db, OTHER, 'human_edit', True, [])
    repo.insert_claims(db, other, [{'evidence_index': 0, 'claim_index': 0, 'text': 'OTHER-DEMO-STATEMENT',
                                  'source_ids': [], 'basis': 'seller_statement'}])
    return db, package, doc, aid


def assert_private_absent(value):
    body = service.canonical(value).decode('utf-8')
    for text in ('@', 'Bearer', 'PayPal-', 'DEMO-ONLY', 'links', 'href', 'OTHER-DEMO-STATEMENT'):
        assert text not in body


def test_core_sources_read_only_and_private_projection(records):
    db, package, doc, aid = records
    before = db.read_bytes()
    result = service.read(db, DID)
    assert db.read_bytes() == before
    assert_private_absent(result)
    kinds = {r['origin'] for r in result['records']}
    assert {'documents', 'packages', 'claims', 'document_confirmations', 'package_attachments',
            'approvals', 'audit_log', 'active_approval_receipts'} <= kinds
    assert not any(r['metadata'].get('package_id') == package + 1 for r in result['records'])
    claim = next(r for r in result['records'] if r['origin'] == 'claims')
    assert claim['actor'] == 'vendor' and claim['timestamp'] is None
    approval = next(r for r in result['records'] if r['origin'] == 'approvals')
    assert approval['actor'] == 'vendor' and approval['metadata']['approval_sha256'] == SHA
    receipt = next(r for r in result['records'] if r['origin'] == 'active_approval_receipts')
    unit = receipt['metadata']['confirmations'][0]
    assert unit['decision'] == 'seller_statement' and unit['sources'][0]['value_sha256'] == SHA
    assert any('Earlier statement text' in text for text in result['limitations'])
    assert any('no stored source table' in text for text in result['limitations'])


def test_missing_database_is_not_created(tmp_path):
    db = tmp_path / 'missing.db'
    data = service.read(db, DID)
    assert not db.exists() and not data['records']
    assert any('not created' in text for text in data['limitations'])


def test_optional_sources_and_unknown_values_are_not_silently_inferred(records):
    db, package, _, _ = records
    with sqlite3.connect(db) as conn:
        for ddl in OPTIONAL_DDLS:
            conn.executescript(ddl)
        conn.execute('INSERT INTO manual_fields VALUES (?,?,?)', (DID, json.dumps({'tracking_number': PRIVATE}), 'bad-time'))
        conn.execute('INSERT INTO auto_prep VALUES (?,?,?,?,?,?,?)', (DID, SHA, PRIVATE, 'ready', package, PRIVATE, '2026-10-01T10:00:00Z'))
        conn.execute('INSERT INTO send_attempts VALUES (?,?,?,?,?,?,?,?,?)',
                     (DID, SHA, 'sent_unconfirmed', PRIVATE, 202, PRIVATE, PRIVATE, PRIVATE, '2026-10-01T10:00:00Z'))
        conn.execute('INSERT INTO draft_messages VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('2026-10-01T10:00:00Z', DID, PRIVATE, PRIVATE, PRIVATE, PRIVATE, PRIVATE, 1, SHA, 1, PRIVATE, 'model'))
        conn.execute('INSERT INTO outcome_appeals VALUES (NULL,?,?,?,?,?,?,?,?)',
                     (DID, '2026-10-01T10:00:00Z', PRIVATE, SHA, SHA, PRIVATE, 'sent_unconfirmed', PRIVATE))
        conn.execute('INSERT INTO resolution_actions VALUES (NULL,?,?,?,?,?,?,?,?,?,?)',
                     (DID, 'offer', '2026-10-01T10:00:00Z', PRIVATE, SHA, SHA, PRIVATE, 0, 'sent_unconfirmed', PRIVATE))
        conn.execute('INSERT INTO webhook_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)',
                     ('2026-10-01T10:00:00Z', PRIVATE, PRIVATE, PRIVATE, DID, PRIVATE, SHA, 'verified', PRIVATE, SHA, PRIVATE))
    result = service.read(db, DID)
    assert_private_absent(result)
    assert {'manual_fields', 'auto_prep', 'send_attempts', 'draft_messages', 'outcome_appeals',
            'resolution_actions', 'webhook_events'} <= {r['origin'] for r in result['records']}
    attempt = next(r for r in result['records'] if r['origin'] == 'send_attempts')
    assert attempt['metadata']['state'] == 'sent_unconfirmed'
    assert any('no proven dispute association' in t for t in result['limitations'])


def test_bad_timestamps_hashes_and_receipt_shapes_safe(records):
    db, _, _, aid = records
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE approvals SET approved_at=?, approval_sha256=?, approved_by=? WHERE id=?', (PRIVATE, PRIVATE, PRIVATE, aid))
        conn.execute('UPDATE active_approval_receipts SET confirmations_json=?', ('{"links": "bad"}',))
        conn.execute('UPDATE audit_log SET detail=?', ('malformed ' + PRIVATE,))
    result = service.read(db, DID)
    assert_private_absent(result)
    row = next(r for r in result['records'] if r['origin'] == 'approvals')
    assert row['timestamp'] is None and row['actor'] == '[redacted]'
    assert row['metadata']['approval_sha256'] is None


def test_chronological_sort_and_deterministic_export(records):
    db, _, _, _ = records
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE documents SET uploaded_at=?', ('2026-10-01T10:00:00-06:00',))
        conn.execute('UPDATE packages SET created_at=? WHERE dispute_id=?', ('2026-10-01T17:00:00Z', DID))
    data = service.read(db, DID)
    dated = [r['timestamp'] for r in data['records'] if r['timestamp']]
    assert dated == sorted(dated)
    before = {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()}
    body, sha = service.export(data)
    assert hashlib.sha256(body).hexdigest() == sha
    assert {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()} == before
    assert body == service.canonical(service.read(db, DID))
    assert service.export(data) == (body, sha)
    assert_private_absent(data)


def test_page_export_same_snapshot_and_no_network(records, monkeypatch, web_mode):
    db, *_ = records
    monkeypatch.setenv('COPILOT_FEATURE_LEDGER', 'true')
    monkeypatch.delenv('PROOFIE_LEDGER_EVIDENCE_FILES', raising=False)
    monkeypatch.delenv('PROOFIE_EVIDENCE_DIR', raising=False)
    web = authenticated_client(create_app(), client=('127.0.0.1', 12345))
    before = {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()}
    page = web.get(f'/disputes/{DID}/ledger')
    exported = web.get(f'/disputes/{DID}/ledger.json')
    assert page.status_code == exported.status_code == 200
    assert page.context['data'] == exported.json()
    shown = re.search(r'<pre id="ledger-data">(.*?)</pre>', page.text, re.S).group(1)
    assert json.loads(html.unescape(shown)) == exported.json()
    sha = exported.headers['X-Ledger-SHA256']
    assert hashlib.sha256(exported.content).hexdigest() == sha
    assert {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()} == before
    assert exported.headers['Content-Disposition'] == f'attachment; filename="ledger-{sha}.json"'
    for text in ('@', 'Bearer', 'PayPal-', 'DEMO-ONLY'):
        assert text not in page.text
    assert web.post(f'/disputes/{DID}/ledger').status_code == 405
    assert web.get('/disputes/not-valid/ledger').status_code == 400


def test_explicit_sanitized_jsonl_line_binding_and_dispute_separation(records, tmp_path, monkeypatch):
    db, *_ = records
    file = tmp_path / 'demo-calls.jsonl'
    rows = [{'ts': '2026-10-01T10:00:00Z', 'op': PRIVATE, 'method': 'GET',
             'path': f'/v1/customer/disputes/{DID}', 'status': 200,
             'request_id': PRIVATE, 'debug_id': PRIVATE, 'issue': PRIVATE,
             'body': {'email': '[email]', 'authorization': '[redacted]', 'links': [{'href': PRIVATE}]}},
            {'path': f'/v1/customer/disputes/{OTHER}', 'body': {'other': PRIVATE}},
            {'path': f'/v1/customer/disputes/{DID}EXTRA', 'body': {'other': PRIVATE}}]
    file.write_text('\n'.join(json.dumps(row) for row in rows) + '\nmalformed\n', encoding='utf-8')
    monkeypatch.setenv('PROOFIE_EVIDENCE_DIR', str(tmp_path))
    monkeypatch.setenv('PROOFIE_LEDGER_EVIDENCE_FILES', '["demo-calls.jsonl"]')
    result = service.read(db, DID, service.configured_evidence())
    calls = [r for r in result['records'] if r['origin'] == 'sanitized_calls']
    assert len(calls) == 1 and calls[0]['metadata']['source_line'] == 1
    assert calls[0]['metadata']['source_line_sha256'] == hashlib.sha256(file.read_bytes().splitlines(keepends=True)[0]).hexdigest()
    assert any('malformed' in t for t in result['limitations'])
    assert_private_absent(result)


@pytest.mark.parametrize('binding', ['["../demo.jsonl"]', '["C:/demo.jsonl"]', 'null', '{}', '[true]', 'bad'])
def test_evidence_binding_rejects_traversal_and_invalid_shapes(monkeypatch, tmp_path, binding):
    monkeypatch.setenv('PROOFIE_EVIDENCE_DIR', str(tmp_path))
    monkeypatch.setenv('PROOFIE_LEDGER_EVIDENCE_FILES', binding)
    with pytest.raises(ValueError):
        service.configured_evidence()


def test_export_needs_no_database_or_destination(tmp_path):
    data = service.read(tmp_path / 'absent.db', DID)
    body, sha = service.export(data)
    assert body == service.canonical(data)
    assert hashlib.sha256(body).hexdigest() == sha
    assert not list(tmp_path.iterdir())


def test_old_content_addressed_file_is_not_overwritten(records):
    db, *_ = records
    data = service.read(db, DID)
    target = db.parent / ('ledger-' + hashlib.sha256(service.canonical(data)).hexdigest() + '.json')
    target.write_bytes(b'corrupt-demo-export')
    before = {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()}
    body, sha = service.export(data)
    assert body == service.canonical(data) and hashlib.sha256(body).hexdigest() == sha
    assert {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()} == before
    assert target.read_bytes() == b'corrupt-demo-export'


def test_get_html_and_json_without_database_write_nothing(tmp_path, monkeypatch, web_mode):
    db = tmp_path / 'not-created' / 'missing.db'
    monkeypatch.setattr(config, '_db_path_override', db)
    monkeypatch.setenv('COPILOT_FEATURE_LEDGER', 'true')
    monkeypatch.delenv('PROOFIE_EVIDENCE_DIR', raising=False)
    monkeypatch.delenv('PROOFIE_LEDGER_EVIDENCE_FILES', raising=False)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with authenticated_client(create_app(), client=('127.0.0.1', 12345)) as web:
        page = web.get(f'/disputes/{DID}/ledger')
        response = web.get(f'/disputes/{DID}/ledger.json')
    assert page.status_code == response.status_code == 200
    assert page.context['data'] == response.json() and response.json()['records'] == []
    assert 'No local database exists' in page.text
    assert response.content == service.canonical(response.json())
    sha = hashlib.sha256(response.content).hexdigest()
    assert response.headers['X-Ledger-SHA256'] == sha
    assert response.headers['Content-Disposition'] == f'attachment; filename="ledger-{sha}.json"'
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert not db.exists() and not db.parent.exists()


def test_cross_dispute_attachment_not_projected(records):
    db, package, *_ = records
    other_doc = repo.insert_document(db, OTHER, 'demo-other.pdf', 'application/pdf', 1, 'b' * 64, 'DEMO', True)
    repo.attach_document(db, package, other_doc)
    data = service.read(db, DID)
    assert not any(r['origin'] == 'package_attachments' and r['metadata']['document_id'] == other_doc for r in data['records'])


def test_cross_package_confirmation_is_not_attributed(records):
    db, _, _, aid = records
    other_package = repo.list_packages(db, OTHER)[0]['id']
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE active_approval_receipts SET package_id=? WHERE approval_id=?', (other_package, aid))
    assert not any(r['origin'] == 'active_approval_receipts' for r in service.read(db, DID)['records'])
    assert not any(r['origin'] == 'active_approval_receipts' for r in service.read(db, OTHER)['records'])


def test_damaged_database_gets_safe_refusal(tmp_path, monkeypatch, web_mode):
    db = config.db_path()
    db.write_bytes(b'not-a-database ' + PRIVATE.encode())
    monkeypatch.setenv('COPILOT_FEATURE_LEDGER', 'true')
    monkeypatch.delenv('PROOFIE_EVIDENCE_DIR', raising=False)
    monkeypatch.delenv('PROOFIE_LEDGER_EVIDENCE_FILES', raising=False)
    web = authenticated_client(create_app(), client=('127.0.0.1', 12345))
    for suffix in ('ledger', 'ledger.json'):
        response = web.get(f'/disputes/{DID}/{suffix}')
        assert response.status_code == 400
        assert PRIVATE not in response.text


def test_linked_database_and_call_file_are_not_read(records, tmp_path, monkeypatch):
    db, *_ = records
    file = tmp_path / 'linked-demo.jsonl'
    file.write_text(json.dumps({'path': f'/v1/customer/disputes/{DID}', 'body': PRIVATE}), encoding='utf-8')
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda p: p in (db, file) or original(p))
    with pytest.raises(ValueError, match='linked'):
        service.read(db, DID)
    monkeypatch.setattr(Path, 'is_symlink', lambda p: p == file or original(p))
    data = service.read(db, DID, [file])
    assert not any(r['origin'] == 'sanitized_calls' for r in data['records'])
    assert any('linked; it was not read' in t for t in data['limitations'])


def test_dr1b_missing_history_uses_human_labels_but_export_is_exact(web_mode, monkeypatch):
    from _dom import parse
    monkeypatch.setenv("COPILOT_FEATURE_LEDGER", "true")
    monkeypatch.delenv("PROOFIE_LEDGER_EVIDENCE_FILES", raising=False)
    monkeypatch.delenv("PROOFIE_EVIDENCE_DIR", raising=False)
    db = config.db_path()
    repo.insert_package(db, DID, "human_edit", False, [])
    before = service.read(db, DID)
    raw_tokens = ["active_approval_receipts", "draft_messages", "outcome_appeals", "resolution_actions", "webhook_events"]
    assert all(any(token + ":" in text for text in before["limitations"]) for token in raw_tokens)
    body, digest = service.export(before)
    web = authenticated_client(create_app(), client=("127.0.0.1", 12345))
    page = web.get(f"/disputes/{DID}/ledger")
    assert page.status_code == 200
    dom = parse(page.text)
    limits = dom.find("section", id="ledger").find("ul")
    assert limits is not None and not limits.inside("details")
    assert all(token not in limits.text() for token in raw_tokens)
    assert "Individual statement reviews" in limits.text() and "Draft messages" in limits.text()
    technical = dom.find("pre", id="ledger-data")
    assert technical.inside("details") and json.loads(technical.text()) == before
    exported = web.get(f"/disputes/{DID}/ledger.json")
    assert exported.content == body and exported.headers["x-ledger-sha256"] == digest
    assert service.read(db, DID) == before
