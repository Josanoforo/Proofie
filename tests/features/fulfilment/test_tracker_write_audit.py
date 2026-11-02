"""Tracker write evidence; published with the fulfilment namespace, not baseline tests/local."""
import httpx
import pytest
import copilot.features.fulfilment.register as tracker_register
import copilot.features.fulfilment.tracker_request as tracker_request
from test_write_audit import CAPTURE, _sdk, _rows, _assert_chain

def test_tracker_approval_and_matching_readback_are_in_actual_jsonl(tmp_path):
    body = tracker_request.build_tracker_body(CAPTURE, "DEMO-TRACK-0001", "FEDEX", "FedEx")
    sha = tracker_request.request_sha256(body)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={})
        # A different tracker's status must not become the status of this approval.
        return httpx.Response(200, json={"trackers": [
            {"tracking_number": "OTHER-SYNTHETIC", "status": "CANCELLED"},
            {"tracking_number": "DEMO-TRACK-0001", "status": "SHIPPED"}]})

    client = _sdk(tmp_path, handler)
    try:
        result = tracker_register.register_tracker(client, body, sha)
        assert result.confirmed
        _assert_chain(_rows(client), "fulfilment_tracker_post", "fulfilment_tracker_get", sha, sha, "SHIPPED")
    finally:
        client._http.close()



def test_unapproved_tracker_never_reaches_sdk_or_creates_an_evidence_line(tmp_path):
    def forbidden(request):
        pytest.fail("An unapproved write reached the transport")
    client = _sdk(tmp_path, forbidden)
    try:
        body = tracker_request.build_tracker_body(CAPTURE, "DEMO-TRACK-0001", "FEDEX", "FedEx")
        with pytest.raises(tracker_register.ApprovalMismatch):
            tracker_register.register_tracker(client, body, "0" * 64)
        assert not client.log_path.exists()
    finally:
        client._http.close()
