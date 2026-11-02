"""Run the bundled habitual Proofie journey through its local ASGI app, with no provider transport."""
import json
import os
import tempfile
from pathlib import Path

from copilot import config


def run_journey():
    environment_before = dict(os.environ)
    loader_before = config.load_env_file
    db_override_before = config._db_path_override
    uploads_override_before = config._uploads_dir_override
    root = Path(__file__).resolve().parents[3]
    owned = config.data_dir() / "mejoras10" / "08"
    owned.mkdir(parents=True, exist_ok=True)
    try:
        config.load_env_file = lambda: None
        for key in list(os.environ):
            if key.startswith("COPILOT_FEATURE_"):
                os.environ.pop(key, None)
        os.environ.pop("COPILOT_HOSTED_DEMO", None)
        os.environ["COPILOT_FEATURE_JUDGE_WALKTHROUGH"] = "true"
        os.environ["COPILOT_FEATURE_EVIDENCE_DOSSIER"] = "true"
        os.environ["COPILOT_FEATURE_EVALUATION_WORKBENCH"] = "true"
        fixture = json.loads((root / "pilot" / "fixtures" / "synthetic" /
                              "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
        case_id = fixture["dispute_id"]
        from fastapi.testclient import TestClient
        from copilot.app import create_app
        from copilot.core.raw_storage import write_raw
        from copilot.db import repo
        from copilot.web.routes_disputes import get_client

        with tempfile.TemporaryDirectory(prefix="journey-", dir=owned) as temp_name:
            temp = Path(temp_name)
            config.set_db_path_override(temp / "copilot.db")
            config.set_uploads_dir_override(temp / "uploads")
            text = "Bundled synthetic walkthrough order record"
            data = text.encode("utf-8")
            import hashlib
            digest = hashlib.sha256(data).hexdigest()
            document_id = repo.insert_document(config.db_path(), case_id, "walkthrough-order.txt", "text/plain",
                                               len(data), digest, text, True)
            write_raw(config.uploads_dir(), digest, data)
            package_id = repo.insert_package(config.db_path(), case_id, "human_edit", True, [])
            repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
                "text": "The bundled synthetic order note records the seller's statement.",
                "source_ids": [f"doc_{document_id}"], "basis": "seller_statement"}])
            repo.attach_document(config.db_path(), package_id, document_id)

            class FixtureReadOnlyTransport:
                calls = []
                def call(self, method, path, operation, **kwargs):
                    self.calls.append((method, path, operation))
                    assert method == "GET", f"walkthrough refused external write: {method} {path}"
                    if operation == "list_disputes": return {"items": [fixture], "links": []}
                    if operation == "show_dispute": return fixture
                    if operation == "context_transaction_search": return {"transaction_details": []}
                    if operation == "context_tracking": return {"trackers": []}
                    raise AssertionError(f"unexpected provider operation {operation}: {path}")

            fake = FixtureReadOnlyTransport()
            app = create_app()
            app.dependency_overrides[get_client] = lambda: fake
            client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
            guide = client.get("/features/judge_walkthrough")
            assert guide.status_code == 200 and 'href="/disputes"' in guide.text
            assert "Synthetic journey provenance" in guide.text and "no buyer submitted a report" in guide.text
            from copilot.features.judge_walkthrough.service import synthetic_identity
            identity = synthetic_identity()
            assert identity and identity["dispute_id"] == case_id
            assert identity["capture_id"] in [tx.get("seller_transaction_id") for tx in fixture["disputed_transactions"]]
            assert all(identity[key] in guide.text for key in ("order_id", "capture_id", "dispute_id"))
            assert "no provider receipt or outcome is implied" in guide.text
            inbox = client.get("/", follow_redirects=False)
            assert inbox.status_code in (307, 308) and inbox.headers["location"] == "/disputes"
            inbox = client.get("/disputes")
            assert inbox.status_code == 200 and case_id in inbox.text and "Local integrated walkthrough" in inbox.text
            detail = client.get(f"/disputes/{case_id}/workspace/prepare")      # the package versions are listed in Prepare
            assert detail.status_code == 200 and f"/disputes/{case_id}/packages/{package_id}" in detail.text
            assert f"/features/judge_walkthrough?case_id={case_id}" in detail.text
            package = client.get(f"/disputes/{case_id}/packages/{package_id}")
            assert package.status_code == 200
            for visible in ("integrated-evidence-dossier", "integrated-evaluation-manifest", digest,
                            "The current case read from PayPal plus Proofie-stored records"):
                assert visible in package.text, f"visible integrated package result missing {visible}"
            assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
            return {"execution_mode": "in-process ASGI TestClient; no listening server",
                    "visited": ["/ redirect", "/disputes inbox", f"/disputes/{case_id}",
                                f"/disputes/{case_id}/packages/{package_id}"],
                    "fixture": "bundled synthetic dispute plus temporary Proofie SQLite package/document records",
                    "synthetic_identity": identity, "package_id": package_id, "document_id": document_id,
                    "visible_result": "integrated source-backed dossier and evaluation provenance manifest",
                    "guide_status": guide.status_code, "inbox_status": inbox.status_code,
                    "detail_status": detail.status_code, "package_status": package.status_code,
                    "result_status": package.status_code,
                    "visible_sections": ["existing inbox", "same-case detail", "dossier", "evaluation provenance"],
                    "transport": "injected GET-only fake; no provider call", "network_required": False,
                    "env_read": False, "provider_state": "NOT USED"}
    finally:
        os.environ.clear()
        os.environ.update(environment_before)
        config.load_env_file = loader_before
        config.set_db_path_override(db_override_before)
        config.set_uploads_dir_override(uploads_override_before)


def main():
    result = run_journey()
    print("Proofie local walkthrough completed")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
