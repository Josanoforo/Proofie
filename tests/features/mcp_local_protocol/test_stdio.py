"""Production server via the SDK stdio client, with a synthetic case and no network."""
import asyncio
import os
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_production_server_stdio(tmp_path, monkeypatch):
    from copilot import config
    from copilot.db import repo
    case = 'PP-R-DEMO-0000001'
    monkeypatch.setenv('COPILOT_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(config, '_db_path_override', None)
    package = repo.insert_package(config.db_path(), case, 'human_edit', True, [])
    repo.insert_claims(config.db_path(), package, [{'evidence_index': 0, 'claim_index': 0,
        'text': 'Synthetic seller record', 'source_ids': [], 'basis': 'seller_statement'}])
    env = {k: v for k, v in os.environ.items() if not k.startswith('COPILOT_FEATURE_') and k != 'COPILOT_MCP_ALLOW_SEND'}
    env['COPILOT_DATA_DIR'] = str(tmp_path)
    async def flow():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'tests.features.mcp_local_protocol.fake_server'], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == 'proofie'
                catalog = {t.name for t in (await session.list_tools()).tools}
                assert 'get_dispute_file' in catalog and 'send_approved_package' not in catalog
                assert not any('approve' in name for name in catalog)
                result = await session.call_tool('get_dispute_file', {'dispute_id': case, 'include_text': True})
                assert not result.is_error
                text = str(result.content)
                assert case in text and f'/packages/{package}' in text and 'Synthetic seller record' in text
                assert 'not_approved' in text
                denied = await session.call_tool('send_approved_package', {'dispute_id': case})
                assert denied.is_error
                invalid = await session.call_tool('get_dispute_file', {'dispute_id': '../invalid'})
                assert invalid.is_error
    asyncio.run(flow())

