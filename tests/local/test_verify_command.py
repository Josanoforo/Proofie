"""Read-only verification of synthetic document bytes and immutable attachment hashes."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest
from copilot.db import repo
from copilot.core.raw_storage import write_raw
ROOT = Path(__file__).resolve().parents[2]
DID = "PP-R-VERIFY-DEMO"


def dataset(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    db = data / "copilot.db"
    package = repo.insert_package(db, DID, "human_edit", True, [])
    docs = []
    for raw in [b"synthetic evidence one\n", b"synthetic evidence two\n"]:
        sha = hashlib.sha256(raw).hexdigest()
        doc = repo.insert_document(db, DID, "synthetic.txt", "text/plain", len(raw), sha, raw.decode(), True)
        write_raw(data / "uploads", sha, raw)
        repo.attach_document(db, package, doc)
        docs.append((doc, sha, raw))
    return data, db, package, docs


def snapshot(data):
    return {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in data.rglob("*") if p.is_file()}


def command(data, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COPILOT_", "PAYPAL_", "PROOFIE_", "OLLAMA_", "PYTEST_", "PYTHONPATH"))}
    env.update(PYTHONPATH=str(ROOT), PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "copilot.verify", "--data-dir", str(data)],
                          cwd=cwd or ROOT, env=env, capture_output=True, text=True, timeout=30)


def test_verify_clean_documents_readonly(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    before = snapshot(data)
    result = command(data)
    assert result.returncode == 0, result.stderr
    for index, (doc, sha, _) in enumerate(docs, 1):
        assert f"OK document={doc} packages={package}:E{index}" in result.stdout
    assert "TOTAL 2 checked: 2 OK, 0 FAIL" in result.stdout
    assert snapshot(data) == before


def test_verify_one_changed_byte_is_a_failure(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    doc, sha, raw = docs[0]
    (data / "uploads" / sha).write_bytes(b"X" + raw[1:])
    before = snapshot(data)
    result = command(data)
    assert result.returncode == 1
    assert f"FAIL document={doc} packages={package}:E1" in result.stdout
    assert "TOTAL 2 checked: 1 OK, 1 FAIL" in result.stdout
    assert snapshot(data) == before


def test_verify_attachment_snapshot_hash_is_independent(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE package_attachments SET sha256=? WHERE document_id=?", ("f" * 64, docs[0][0]))
    before = snapshot(data)
    result = command(data)
    assert result.returncode == 1
    assert "attachment hash does not match file" in result.stdout
    assert snapshot(data) == before


def test_verify_missing_file_fails_without_recreating_it(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    path = data / "uploads" / docs[0][1]; path.unlink()
    before = snapshot(data)
    result = command(data)
    assert result.returncode == 1
    assert "file is missing or cannot be read safely" in result.stdout
    assert not path.exists() and snapshot(data) == before


@pytest.mark.parametrize("bad", ["../outside", "not-a-hash", "A" * 64])
def test_verify_invalid_hash_is_not_a_filesystem_path(tmp_path, bad):
    data, db, package, docs = dataset(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE documents SET sha256=? WHERE id=?", (bad, docs[0][0]))
    before = snapshot(data)
    result = command(data)
    assert result.returncode == 1 and "invalid stored hash" in result.stdout
    assert bad not in result.stdout and snapshot(data) == before


@pytest.mark.parametrize("existing_dir", [False, True])
def test_verify_absent_data_database_exits_two_without_creation(tmp_path, existing_dir):
    data = tmp_path / "missing"
    if existing_dir: data.mkdir()
    result = command(data)
    assert result.returncode == 2
    assert not (data / "copilot.db").exists()
    assert data.is_dir() is existing_dir


def test_verify_existing_wrong_schema_is_not_initialised(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    with sqlite3.connect(data / "copilot.db") as conn: conn.execute("CREATE TABLE unrelated(id INTEGER)")
    before = snapshot(data)
    result = command(data)
    assert result.returncode == 1 and "FAIL database" in result.stdout
    assert snapshot(data) == before


def test_verify_empty_existing_store_reports_zero_without_writes(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM package_attachments"); conn.execute("DELETE FROM documents")
    before = snapshot(data)
    result = command(data)
    assert result.returncode == 0
    assert "TOTAL 0 checked: 0 OK, 0 FAIL" in result.stdout
    assert snapshot(data) == before


def test_verify_exhibit_order_includes_claim_only_document(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    repo.detach_document(db, package, docs[0][0])
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "Synthetic statement.",
                                    "source_ids": ["doc_" + str(docs[0][0])], "basis": "seller_statement"}])
    second = repo.insert_package(db, DID, "human_edit", True, [])
    repo.attach_document(db, second, docs[1][0])
    result = command(data)
    assert result.returncode == 0, result.stderr
    assert f"OK document={docs[1][0]} packages={package}:E2,{second}:E1" in result.stdout
    assert "TOTAL 2 checked: 2 OK, 0 FAIL" in result.stdout


def test_verify_orphan_document_reference_is_not_ignored(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    with sqlite3.connect(db) as conn: conn.execute("DELETE FROM documents WHERE id=?", (docs[0][0],))
    result = command(data)
    assert result.returncode == 1
    assert f"FAIL document={docs[0][0]} packages={package}:E1" in result.stdout
    assert "document metadata is missing" in result.stdout


def test_verify_distinct_cwd_uses_explicit_data_directory(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    other = tmp_path / "unrelated"; other.mkdir()
    result = command(data, cwd=other)
    assert result.returncode == 0, result.stderr
    assert "TOTAL 2 checked: 2 OK, 0 FAIL" in result.stdout
    assert list(other.iterdir()) == []


def test_verify_reads_committed_wal_snapshot_without_seeing_uncommitted_hash(tmp_path):
    data, db, package, docs = dataset(tmp_path)
    writer = sqlite3.connect(db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("BEGIN")
        writer.execute("UPDATE documents SET sha256=? WHERE id=?", ("f" * 64, docs[0][0]))
        result = command(data)
        assert result.returncode == 0, result.stderr
        assert "TOTAL 2 checked: 2 OK, 0 FAIL" in result.stdout
    finally:
        writer.rollback(); writer.close()
