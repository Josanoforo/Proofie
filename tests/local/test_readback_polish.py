"""Scoped private readback projection; real SDK, MockTransport, no constructor/OAuth/environment."""
import copy
import hashlib
import json

import httpx
import pytest

from client import PayPalError, readback_audit, sanitize, write_audit
from test_write_audit import _rows, _sdk

RAW = {"dispute_id": "PP-R-TST-0000001", "status": "UNDER_REVIEW",
       "dispute_life_cycle_stage": "CHARGEBACK", "seller_response_due_date": "2026-11-01T00:00:00Z",
       "dispute_outcome": {"outcome_code": "CANCELED_BY_BUYER", "messages": ["DEMO DATA"]},
       "links": [{"rel": "self", "href": "DEMO DATA"}, {"rel": "provide_evidence", "href": "DEMO DATA"}],
       "trackers": [{"tracking_number": "DEMO-TRACK-01", "status": "SHIPPED", "carrier": "FEDEX", "given_name": "DEMO DATA"}],
       "messages": ["DEMO DATA"], "evidences": [{"given_name": "DEMO DATA"}], "fund_movements": ["DEMO DATA"]}


@pytest.mark.parametrize("op", ["resolution_readback", "resolution_poll", "fulfilment_tracker_get"])
def test_polish_scoped_readback_projects_only_keys_and_canonical_sanitized_hash(tmp_path, op):
    body = copy.deepcopy(RAW)
    audit = readback_audit("a" * 64, "b" * 64, tracking_number="DEMO-TRACK-01" if op == "fulfilment_tracker_get" else None)
    client = _sdk(tmp_path, lambda request: httpx.Response(200, json=body))
    try:
        returned = client.call("GET", "/v1/test", op, log_extra=audit)
        assert returned == body and returned["trackers"][0]["given_name"] == "DEMO DATA"
        line, = _rows(client)
        assert "body" not in line
        assert line["body_sha256"] == hashlib.sha256(json.dumps(sanitize(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        keys = line["body_keys"]
        assert not ({"messages", "evidences", "fund_movements"} & keys.keys())
        assert keys["links"] == [{"rel": "provide_evidence"}, {"rel": "self"}]
        if op == "fulfilment_tracker_get":
            assert keys["trackers"] == [{"tracking_number": "DEMO-TRACK-01", "status": "SHIPPED", "carrier": "FEDEX"}]
            assert line["settled_status"] == "SHIPPED"
        else:
            assert keys["dispute_id"] == body["dispute_id"] and keys["status"] == "UNDER_REVIEW"
            assert keys["dispute_life_cycle_stage"] == "CHARGEBACK"
            assert keys["seller_response_due_date"] == body["seller_response_due_date"]
            assert keys["dispute_outcome"] == {"outcome_code": "CANCELED_BY_BUYER"}
            assert line["settled_status"] == "UNDER_REVIEW"
        assert line["request_sha256"] == "a" * 64 and line["approval_sha256"] == "b" * 64
    finally:
        client._http.close()


@pytest.mark.parametrize("method,op,kind,status", [
    ("GET", "post_send_get", "audit", 200), ("GET", "verify_get", "audit", 200),
    ("GET", "audited", "audit", 200), ("POST", "resolution_readback", "audit", 200),
    ("GET", "resolution_readback", "plain", 200), ("GET", "resolution_readback", "write", 200),
    ("GET", "resolution_readback", "callable", 200), ("GET", "resolution_readback", "audit", 400),
])
def test_polish_scope_negative_preserves_sanitized_body_and_audit_contract(tmp_path, method, op, kind, status):
    body = copy.deepcopy(RAW)
    client = _sdk(tmp_path, lambda request: httpx.Response(status, json=body))
    extra = readback_audit("a" * 64, "b" * 64) if kind == "audit" else write_audit("a" * 64, "b" * 64) if kind == "write" else {"settled_status": lambda b: "UNDER_REVIEW"} if kind == "callable" else {}
    try:
        if status >= 400:
            with pytest.raises(PayPalError):client.call(method, "/v1/test", op, log_extra=extra)
        else:
            assert client.call(method, "/v1/test", op, log_extra=extra) == body
        line, = _rows(client)
        assert line["body"] == sanitize(body)
        assert "body_keys" not in line and "body_sha256" not in line
        if kind == "audit":
            assert line["request_sha256"] == "a" * 64 and line["approval_sha256"] == "b" * 64
            assert line["settled_status"] == ("UNDER_REVIEW" if method == "GET" and status < 400 else None)
    finally:
        client._http.close()


def test_polish_projected_sdk_line_preserves_index_citation_contract(tmp_path):
    from tools import export_public as ep
    client = _sdk(tmp_path, lambda request: httpx.Response(200, json=RAW))
    try:
        client.call("GET", "/v1/test", "resolution_readback", log_extra=readback_audit("a" * 64, "b" * 64))
        row, = _rows(client)
        assert "body_keys" in row and "body" not in row
        data = client.log_path.read_bytes()
        claim = {"id": "EV-DEMO", "file": "demo.jsonl", "lines": [1, 1], "recorded_utc": "2026-10-13",
                 "proves": "Synthetic audit schema verification.", "used_by": []}
        index = ep.build_evidence_index("c" * 40, {"demo.jsonl": data}, [claim], True).decode()
        assert hashlib.sha256(data).hexdigest() in index
        assert hashlib.sha256(data.rstrip(b"\n")).hexdigest() in index
        assert data.decode().strip() not in index and '"body_keys"' not in index
        assert '| EV-DEMO |' in index
    finally:
        client._http.close()
