"""Approved writes/readbacks evaluated from the real SDK JSONL, using mock transport only."""
import copy
import json

import httpx
import pytest

from copilot import config
from copilot.core.disputes import PayPalError, SandboxClient, readback_audit, write_audit
from copilot.db import repo


DID = "PP-R-TST-0000001"
CAPTURE = "5S647034CC1979815"


def _sdk(tmp_path, handler):
    # Deliberately do not call __init__: no dotenv, credential, OAuth or real transport.
    client = SandboxClient.__new__(SandboxClient)
    client._http = httpx.Client(base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(handler))
    client._token, client._token_expiry = "synthetic-test-token", float("inf")
    client.log_path = tmp_path / "write-audit.jsonl"
    return client


def _rows(client):
    return [json.loads(line) for line in client.log_path.read_text(encoding="utf-8").splitlines()]


def _assert_chain(rows, post_op, read_op, request_sha, approval_sha, status):
    writes = [row for row in rows if row["op"] == post_op]
    reads = [row for row in rows if row["op"] == read_op]
    assert len(writes) == 1
    assert reads
    assert writes[0]["request_sha256"] == request_sha
    assert writes[0]["approval_sha256"] == approval_sha
    for row in reads:
        assert row["request_sha256"] == request_sha
        assert row["settled_status"] == status
    assert rows.index(writes[0]) < rows.index(reads[0])






@pytest.mark.usefixtures("legacy_approval_scenario")
def test_app_evidence_approval_and_readback_are_in_actual_jsonl(tmp_path, monkeypatch):
    # Reuse the existing UI approval setup, then run its real send service and SDK.
    from test_routes_send import _client, _approved_package_with_tracking, WAITING, DID as send_did
    from copilot.web.routes_disputes import get_client
    from copilot.core.send import sha256_json

    web, db_path, token, _fake = _client(tmp_path, monkeypatch)
    package_id = _approved_package_with_tracking(web, db_path, token)
    approval = repo.get_latest_valid_approval(db_path, package_id)
    response_body = copy.deepcopy(WAITING)
    sent_input = None

    def handler(request):
        nonlocal sent_input
        if request.method == "POST":
            raw = request.content.decode("utf-8")
            sent_input = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            ev = sent_input["evidences"][0]
            response_body["status"] = "UNDER_REVIEW"
            response_body["evidences"] = [{**ev, "source": "SUBMITTED_BY_SELLER"}]
            return httpx.Response(200, json={})
        return httpx.Response(200, json=response_body)

    client = _sdk(tmp_path, handler)
    web.app.dependency_overrides[get_client] = lambda: client
    try:
        result = web.post(f"/disputes/{send_did}/packages/{package_id}/send", data={"csrf_token": token})
        assert result.status_code == 200 and "Evidence sent to PayPal" in result.text
        _assert_chain(_rows(client), "provide_evidence", "post_send_get", sha256_json(sent_input),
                      approval["approval_sha256"], "UNDER_REVIEW")
    finally:
        client._http.close()
        web.close()


@pytest.mark.parametrize("failure", ["http", "transport"])
def test_audit_error_lines_keep_hashes_without_inventing_a_settled_status(tmp_path, failure):
    def handler(request):
        if failure == "transport":
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(500, json={"status": "RESOLVED", "name": "SYNTHETIC_FAILURE"})
    client = _sdk(tmp_path, handler)
    try:
        for method, metadata in [("POST", write_audit("a" * 64, "b" * 64)),
                                 ("GET", readback_audit("a" * 64, "b" * 64))]:
            with pytest.raises((PayPalError, httpx.TransportError)):
                client.call(method, "/v1/test", "audit_failure", log_extra=metadata)
        rows = _rows(client)
        assert len(rows) == 2
        assert all(row["request_sha256"] == "a" * 64 and row["approval_sha256"] == "b" * 64 for row in rows)
        assert "settled_status" not in rows[0]
        assert rows[1]["settled_status"] is None
    finally:
        client._http.close()


@pytest.mark.parametrize("remote_status", ["UNDER_REVIEW", "SYNTHETIC-PRIVATE-REVIEWER", None])
def test_recovery_jsonl_status_is_remote_word_not_a_confirmation(tmp_path, remote_status):
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send_recovery import recover_dispute
    req_input = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "Synthetic test",
                               "evidence_info": {"tracking_info": [{"tracking_number": "DEMO-TRACK-0001"}]}}]}
    attempt_store = SQLiteAttemptStore(config.db_path())
    attempt_store.write(DID, "a" * 64, {"state": "sent_unconfirmed", "request_input": req_input,
                                      "request_id": "synthetic-request-id"})
    client = _sdk(tmp_path, lambda request: httpx.Response(200, json={"status": remote_status, "evidences": []}))
    try:
        pending = recover_dispute(client, attempt_store, DID, config.db_path())
        assert len(pending) == 1 and pending[0]["state"] == "sent_unconfirmed"
        rows = _rows(client)
        assert len(rows) == 1 and rows[0]["method"] == "GET"
        assert rows[0]["request_sha256"] == "a" * 64
        assert rows[0]["settled_status"] == ("UNDER_REVIEW" if remote_status == "UNDER_REVIEW" else None)
    finally:
        client._http.close()
        attempt_store.close()


def test_new_audit_values_are_only_hashes_and_status_and_survive_sanitizer(tmp_path):
    from client import sanitize
    response_body = {"status": "UNDER_REVIEW", "email": "synthetic@example.com", "given_name": "Synthetic"}
    client = _sdk(tmp_path, lambda request: httpx.Response(200, json=response_body))
    try:
        client.call("GET", "/v1/test", "audited", log_extra=readback_audit("a" * 64, "b" * 64))
        client.call("GET", "/v1/test", "control")
        audited, control = _rows(client)
        assert audited["body"] == control["body"] == sanitize(response_body)
        assert audited["body"]["email"] == "[email]"
        assert audited["body"]["given_name"] == "[redacted]"
        added = set(audited) - set(control)
        assert added == {"request_sha256", "approval_sha256", "settled_status"}
        assert {key: audited[key] for key in added} == {
            "request_sha256": "a" * 64, "approval_sha256": "b" * 64, "settled_status": "UNDER_REVIEW"}
    finally:
        client._http.close()






def test_pilot_send_and_verify_use_same_hash_schema_in_actual_jsonl(tmp_path, monkeypatch):
    import step5_send_evidence as s5
    from test_step5_send import FakePayPal, WAITING, make_pkg, CAPTURE as pilot_capture
    monkeypatch.setattr(s5, "ATTEMPTS_DIR", tmp_path / "send_attempts")
    monkeypatch.setattr(s5, "_expected_capture", lambda: pilot_capture)
    pkg = make_pkg()
    sha = pkg["paypal_request_sha256"]
    approval_sha = s5.approval_fingerprint(pkg["dispute_id"], "provide_evidence", pkg["paypal_request_input"])
    fake = FakePayPal(WAITING)
    client = _sdk(tmp_path, fake)
    try:
        code, text = s5.send(client, pkg, "Synthetic explicit approval")
        assert code == 0 and "VERIFIED" in text
        _assert_chain(_rows(client), "provide_evidence", "post_send_get", sha, approval_sha, WAITING["status"])
        assert s5.verify_only(client, pkg)[0] == 0
        verified = _rows(client)[-1]
        assert verified["op"] == "verify_get" and verified["request_sha256"] == sha
        assert verified["settled_status"] == WAITING["status"]
        assert len(fake.posts()) == 1
    finally:
        client._http.close()


def test_unapproved_evidence_never_reaches_sdk_or_creates_an_evidence_line(tmp_path, monkeypatch):
    from test_routes_send import _client, DID as send_did
    from copilot.core.send_service import send_package
    def forbidden(request):
        pytest.fail("An unapproved write reached the transport")
    client = _sdk(tmp_path, forbidden)
    web, db_path, _token, _fake = _client(tmp_path, monkeypatch)
    try:
        package_id = repo.insert_package(db_path, send_did, source="human_edit", validation_ok=False,
                                         review_flags=[])
        assert send_package(client, db_path, send_did, package_id).title == "Not approved"
        assert not client.log_path.exists()
    finally:
        web.close()
        client._http.close()
