"""Test-only stdio entrypoint for the production MCP server, with a closed fake provider."""
import json
import os
from pathlib import Path

from copilot import config
def forbid_env():
    raise AssertionError(".env load forbidden")
config.load_env_file = forbid_env
from copilot import mcp_server

fixture = json.loads(Path('pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json').read_text(encoding='utf-8'))['body']

class FakeProvider:
    def call(self, method, path, operation, **kwargs):
        if method != 'GET':
            raise AssertionError('provider write forbidden')
        if operation == 'mcp_show_dispute' and path == '/v1/customer/disputes/' + fixture['dispute_id']:
            return fixture
        if operation == 'context_transaction_search':
            return {'transaction_details': []}
        if operation == 'context_tracking':
            return {'trackers': []}
        raise AssertionError('unexpected provider operation: ' + operation)

mcp_server._make_client = lambda: FakeProvider()
# A real HTTP transport, including a model call, is a hard failure.
import httpx
httpx.Client.send = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
httpx.AsyncClient.send = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
mcp_server.main()
