"""Standalone script (not collected by pytest) run as a REAL separate OS process by
test_attempt_store.py's multi-process test. Prints exactly "True" or "False" to stdout: whether this
process's claim() call succeeded. Deliberately a subprocess, not multiprocessing/threading, so the
concurrency guarantee is proven across independent interpreters, not just independent connections or
threads inside one interpreter.

Usage: python _attempt_store_claim_worker.py <db_path> <dispute_id> <package_sha>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.attempt_store import SQLiteAttemptStore

if __name__ == "__main__":
    db_path, dispute_id, package_sha = sys.argv[1], sys.argv[2], sys.argv[3]
    store = SQLiteAttemptStore(db_path)
    print(store.claim(dispute_id, package_sha))
    store.close()
