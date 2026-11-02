"""Real protocol path from Proofie local DB artifacts to the habitual case detail."""
import asyncio
import json
import os
import sys
import uuid

from _hosted import authenticated_client
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_uses_proofie_artifacts_and_prepared_note_appears_in_case_detail(monkeypatch):
    """Expected before run: protocol discovery exposes only read/prepare tools, an authorized actor reads
    actual Proofie DB metadata and prepares a bounded note; foreign/extra/unknown calls refuse; the normal
    GET-only dispute detail renders that same persisted note and audit proves no provider write happened.
    """
    from copilot import config
    from copilot.db import repo
    from copilot.features.mcp_local_contracts import service
    from copilot.web.routes_disputes import get_client

    work = service.OWNED_ROOT / ("r3-protocol-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    case_id, other_id = "PP-R-M10-0000012", "PP-R-M10-0000013"
    owned = work / "mejoras10" / "06"                      # COPILOT_DATA_DIR=work, so the server's owned folder is work/mejoras10/06
    owned.mkdir(parents=True)
    registry_path, audit_path = owned / "registry.json", owned / "audit.jsonl"
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)
    monkeypatch.setenv("COPILOT_DATA_DIR", str(work))
    monkeypatch.setenv("PROOFIE_MCP_LOCAL_AUDIT", str(audit_path))
    monkeypatch.setenv("PROOFIE_MCP_LOCAL_REGISTRY", str(registry_path))
    monkeypatch.setenv("PROOFIE_MCP_LOCAL_ACTOR", "alice")
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_MCP_LOCAL_CONTRACTS", "true")

    doc_id = repo.insert_document(config.db_path(), case_id, "order-note.txt", "text/plain", 17,
                                 "a" * 64, "Seller supplied order record", True)
    package_id = repo.insert_package(config.db_path(), case_id, "human_edit", True, [])
    repo.insert_claims(config.db_path(), package_id, [{"evidence_index": 0, "claim_index": 0,
        "text": "Order information recorded.", "source_ids": [f"doc_{doc_id}"], "basis": "seller_statement"}])
    registry_path.write_text(json.dumps({"actor_cases": {"alice": [case_id], "bob": [other_id]}}), encoding="utf-8")

    async def protocol_flow():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "copilot.features.mcp_local_contracts.server"], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                assert {tool.name for tool in catalog.tools} == {"list_local_cases", "read_local_case", "prepare_local_note"}
                assert all(tool.input_schema.get("additionalProperties") is False for tool in catalog.tools)
                listed = await session.call_tool("list_local_cases", {})
                assert case_id in str(listed.content) and other_id not in str(listed.content)
                artifact = await session.call_tool("read_local_case", {"case_id": case_id})
                assert f"{doc_id}" in str(artifact.content) and "order-note.txt" in str(artifact.content)
                prepared = await session.call_tool("prepare_local_note", {"case_id": case_id, "note": "Check seller order record"})
                assert "not sent" in str(prepared.content)
                assert (await session.call_tool("read_local_case", {"case_id": other_id})).is_error
                assert (await session.call_tool("read_local_case", {"case_id": case_id, "extra": "reject"})).is_error
                assert (await session.call_tool("accept-claim", {})).is_error

    try:
        asyncio.run(protocol_flow())
        from copilot.app import create_app

        class ReadOnlyFake:
            def __init__(self): self.calls = []
            def call(self, method, path, op, **kwargs):
                self.calls.append((method, path, op))
                assert method == "GET"
                assert path == f"/v1/customer/disputes/{case_id}"
                return {"dispute_id": case_id, "status": "WAITING_FOR_SELLER_RESPONSE",
                        "dispute_life_cycle_stage": "INQUIRY", "reason": "UNAUTHORISED",
                        "dispute_amount": {"value": "25.00", "currency_code": "USD"},
                        "links": [], "disputed_transactions": []}

        fake = ReadOnlyFake()
        app = create_app()
        app.dependency_overrides[get_client] = lambda: fake
        client = authenticated_client(app, client=("127.0.0.1", 12345))
        detail = client.get(f"/disputes/{case_id}/workspace/hackathon")
        assert detail.status_code == 200, detail.text
        panel = detail.text.split('data-state="mcp-local-case-notes"', 1)[1].split("</section>", 1)[0]
        assert "Check seller order record" in panel and "prepared locally; not sent" in panel
        assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        assert [row["result"] for row in rows].count("handled") == 3
        assert [row["result"] for row in rows].count("refused") == 3
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)       # the whole per-test data folder (db, registry, audit, notes)
