"""The package view supplies real local provenance to the evaluation instrument."""
import hashlib
import json
import os
from pathlib import Path

from _hosted import authenticated_client
from fastapi.testclient import TestClient


def test_package_manifest_uses_actual_ids_hashes_and_unknown_provenance(monkeypatch, tmp_path):
    """Expected before run: package review auto-derives a manifest from same-case DB records and original bytes,
    reports missing bytes/source IDs as UNKNOWN, keeps historic CUT72 SHA fixed, and records no model/scorer run.
    Only injected GET transport is permitted; no package writer is invoked.
    """
    from copilot import config
    from copilot.app import create_app
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "proofie.sqlite3")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_EVALUATION_WORKBENCH", "true")
    case_id = "PP-R-DEMO-0000001"
    fixture_path = Path(__file__).resolve().parents[3] / "pilot" / "fixtures" / "synthetic" / \
        "dispute_PP-R-DEMO-0000001_waiting_for_seller.json"
    dispute_raw = json.loads(fixture_path.read_text(encoding="utf-8"))["body"]

    class ReadOnlyFake:
        def __init__(self): self.calls = []
        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path, op))
            assert method == "GET"
            if op == "show_dispute": return dispute_raw
            if op == "context_transaction_search": return {"transaction_details": []}
            if op == "context_tracking": return {"trackers": []}
            raise AssertionError(f"unexpected read {op}: {path}")

    good = b"synthetic package source bytes"
    good_sha = hashlib.sha256(good).hexdigest()
    good_id = repo.insert_document(config.db_path(), case_id, "order-source.txt", "text/plain", len(good),
                                   good_sha, "Seller recorded order source", True)
    write_raw(config.uploads_dir(), good_sha, good)
    missing_id = repo.insert_document(config.db_path(), case_id, "unavailable-scan.pdf", "application/pdf", 50,
                                      "b" * 64, None, False)
    package_id = repo.insert_package(config.db_path(), case_id, "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [
        {"evidence_index": 0, "claim_index": 0, "text": "Order recorded from source.",
         "source_ids": [f"doc_{good_id}"], "basis": "seller_statement"},
        {"evidence_index": 0, "claim_index": 1, "text": "An unavailable scan and missing reference are noted.",
         "source_ids": [f"doc_{missing_id}", "record-absent-77"], "basis": "seller_statement"},
    ])
    repo.attach_document(config.db_path(), package_id, good_id)
    repo.attach_document(config.db_path(), package_id, missing_id)
    fake = ReadOnlyFake()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = authenticated_client(app, client=("127.0.0.1", 12345))

    response = client.get(f"/disputes/{case_id}/packages/{package_id}")
    assert response.status_code == 200, response.text
    panel = response.text.split('data-state="integrated-evaluation-manifest"', 1)[1].split("</details>", 1)[0]
    assert "Technical detail: evidence provenance and evaluation manifest" in panel
    assert f"Case <span translate=\"no\">{case_id}</span>, package {package_id}" in panel
    assert good_sha in panel and f"doc_{good_id}" in panel
    assert "UNKNOWN" in panel and "record-absent-77" in panel
    assert "no scorer, benchmark, model, or quality measurement ran" in panel
    assert "5a12f56e136b1f7e98b1e5b9f0387191f068be1e" in panel
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
