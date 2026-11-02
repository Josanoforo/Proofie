"""Offer/message evidence; published with resolution, not the baseline namespace selection."""
import json
from datetime import datetime, timezone

import httpx
import pytest
import copilot.features.resolution.actions as actions
import copilot.features.resolution.service as service
import copilot.features.resolution.store as store
from test_write_audit import DID, _sdk, _rows, _assert_chain

@pytest.mark.parametrize("action", ["offer", "message"])
def test_resolution_approval_and_deferred_readback_are_in_actual_jsonl(tmp_path, action):
    posted = False
    sent = {}
    payload = actions.offer_body(1000, "USD", "Synthetic test offer") if action == "offer" else {
        "message": "Synthetic test message"}
    sha = actions.request_sha256(payload)

    def handler(request):
        nonlocal posted
        if request.method == "POST":
            posted = True
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={})
        # PayPal's read-back lists the seller message it accepted (a message is confirmed only then).
        listed = {"messages": [{"posted_by": "SELLER", "content": sent["message"],
                                "time_posted": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}]}             if posted and "message" in sent else {}
        rels = [] if posted else ["make_offer", "send_message"]
        return httpx.Response(200, json={**listed, "dispute_id": DID, "status": (
            "WAITING_FOR_BUYER_RESPONSE" if posted else "WAITING_FOR_SELLER_RESPONSE"),
            "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"currency_code": "USD", "value": "40.00"},
            "links": [{"rel": rel, "method": "POST", "href": (
                f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{DID}/{rel.replace('_', '-')}")}
                for rel in rels]})

    client = _sdk(tmp_path, handler)
    try:
        outcome = service.register(client, DID, action, payload, sha)
        assert outcome.status == store.STATUS_PENDING
        assert service.settle_pending(client, DID) == 1
        assert store.list_actions(DID)[0]["status"] == "sent_confirmed"
        rows = _rows(client)
        _assert_chain(rows, f"resolution_{action}", "resolution_poll", sha, sha, "WAITING_FOR_BUYER_RESPONSE")
        _assert_chain(rows, f"resolution_{action}", "resolution_readback", sha, sha, "WAITING_FOR_BUYER_RESPONSE")
    finally:
        client._http.close()


def test_resolution_pending_get_logs_observed_word_without_promoting_the_row(tmp_path):
    store.save_action(DID, "message", {}, "a" * 64, "b" * 64, None, False, store.STATUS_PENDING)
    client = _sdk(tmp_path, lambda request: httpx.Response(200, json={
        "dispute_id": DID, "status": "UNDER_REVIEW", "links": []}))
    try:
        assert service.settle_pending(client, DID) == 0
        assert store.list_actions(DID)[0]["status"] == store.STATUS_PENDING
        rows = _rows(client)
        assert len(rows) == 1 and rows[0]["method"] == "GET"
        assert rows[0]["request_sha256"] == "a" * 64 and rows[0]["approval_sha256"] == "b" * 64
        assert rows[0]["settled_status"] == "UNDER_REVIEW"
    finally:
        client._http.close()



@pytest.mark.parametrize("operation", ["offer", "message"])
def test_unapproved_resolution_never_reaches_sdk_or_creates_an_evidence_line(tmp_path, operation):
    def forbidden(request):
        pytest.fail("An unapproved write reached the transport")
    client = _sdk(tmp_path, forbidden)
    try:
        body = actions.offer_body(1000, "USD", "Synthetic test offer") if operation == "offer" else {
            "message": "Synthetic test message"}
        assert service.register(client, DID, operation, body, "0" * 64).status == "rejected_hash"
        assert not client.log_path.exists()
    finally:
        client._http.close()
