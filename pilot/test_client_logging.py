"""Offline self-check: transport errors must leave an evidence line."""
import json
import time

import httpx
import pytest

from client import PayPalError, SandboxClient


def test_transport_error_is_logged(monkeypatch, tmp_path):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "x")
    c = SandboxClient("unused_logging_probe")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("t")

    c._http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler))
    c._token = "x"
    c._token_expiry = time.time() + 3600
    c.log_path = tmp_path / "probe.jsonl"

    with pytest.raises(httpx.ReadTimeout):
        c.call("POST", "/v2/x", "probe", json_body={}, request_id="rid-1")

    lines = c.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["status"] is None
    assert rec["transport_error"] == "ReadTimeout"
    assert rec["request_id"] == "rid-1"
    assert rec["outcome"] == "unknown"


def _client_with_scope_response(monkeypatch, tmp_path, scope_string: str, status: int = 200):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "x")
    c = SandboxClient("unused_logging_probe")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"scope": scope_string} if status == 200
                               else {"error": "invalid_client"})

    c._http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler))
    c.log_path = tmp_path / "probe.jsonl"
    return c


def test_scopes_logs_count_and_presence_not_the_token_or_full_list(monkeypatch, tmp_path):
    scope_string = "https://uri.paypal.com/services/disputes/read-seller " \
                   "https://uri.paypal.com/services/disputes/update-seller openid"
    c = _client_with_scope_response(monkeypatch, tmp_path, scope_string)
    result = c.scopes()
    assert result == scope_string.split()
    lines = c.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["op"] == "scopes" and rec["status"] == 200
    assert rec["scopes_count"] == 3
    assert rec["relevant_scopes_present"]["disputes/read-seller"] is True
    assert rec["relevant_scopes_present"]["disputes/update-seller"] is True
    assert rec["relevant_scopes_present"]["disputes/create"] is False
    raw = c.log_path.read_text(encoding="utf-8")
    assert scope_string not in raw and "disputes/read-seller " not in raw
    assert "x" * 1 not in json.dumps(rec)  # no raw token/secret field present at all
    assert "access_token" not in rec and "scope" not in rec


def test_scopes_error_is_logged_without_body(monkeypatch, tmp_path):
    c = _client_with_scope_response(monkeypatch, tmp_path, "", status=401)
    with pytest.raises(PayPalError):
        c.scopes()
    lines = c.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["op"] == "scopes" and rec["status"] == 401 and rec["error"] == "invalid_client"
