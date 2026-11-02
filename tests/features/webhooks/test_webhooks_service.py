"""handle_event: the ordered outcomes, one by one, on the fake client and a throwaway database."""
import json

import httpx
import pytest

from copilot import config
from copilot.core.send import sha256_json
from copilot.features.webhooks import service, store
from webhooks_fakes import DISPUTE_ID, HEADERS, WEBHOOK_ID, FakeClient, dispute_get, event, paypal_error, raw

DB = lambda: config.db_path()  # noqa: E731


def _handle(client, body=None, webhook_id=WEBHOOK_ID, headers=HEADERS):
    return service.handle_event(client, DB(), headers, raw() if body is None else body, webhook_id)


def test_503_when_the_webhook_id_is_unset_nothing_stored_no_call():
    client = FakeClient()
    h = _handle(client, webhook_id=None)
    assert h.http_status == 503 and client.calls == [] and store.list_events() == []


def test_413_when_the_body_is_over_65536_bytes_nothing_stored_no_call():
    client = FakeClient()
    big = json.dumps(event(padding="x" * 70000)).encode("utf-8")
    assert len(big) > 65536
    assert _handle(client, big).http_status == 413 and client.calls == [] and store.list_events() == []
    exactly = json.dumps(event(padding="")).encode("utf-8")
    assert len(exactly) <= 65536 and _handle(FakeClient(), exactly).http_status == 200


@pytest.mark.parametrize("body", [b"not json", b"[1]", b'"x"', b"", b"{}", b'{"id": "WH-1"}', b'{"event_type": "X"}',
                                  b'{"id": 5, "event_type": "X"}', b'{"id": "", "event_type": "X"}',
                                  json.dumps({"id": "x" * 200, "event_type": "X"}).encode()])
def test_400_when_the_body_is_not_a_json_object_or_has_no_usable_id_and_type(body):
    client = FakeClient()
    assert _handle(client, body).http_status == 400 and client.calls == [] and store.list_events() == []


def test_a_verified_event_already_stored_is_a_200_duplicate_without_any_call():
    _handle(FakeClient())
    client = FakeClient()
    h = _handle(client)
    assert (h.http_status, h.verification) == (200, "duplicate") and client.calls == []
    assert len(store.list_events()) == 1


def test_a_stored_event_that_never_verified_is_verified_again_on_the_retry():
    _handle(FakeClient({"verification_status": "FAILURE"}))
    assert store.get_event("WH-TEST-1")["verification"] == "failed"
    client = FakeClient()
    h = _handle(client)
    assert h.verification == "verified" and len(client.posts()) == 1 and len(client.gets()) == 1
    assert store.get_event("WH-TEST-1")["verification"] == "verified" and len(store.list_events()) == 1


def test_a_missing_header_is_400_stored_as_rejected_with_no_call():
    client = FakeClient()
    headers = {k: v for k, v in HEADERS.items() if k != "PAYPAL-CERT-URL"}
    h = _handle(client, headers=headers)
    assert (h.http_status, h.verification) == (400, "rejected_headers") and client.calls == []
    assert store.get_event("WH-TEST-1")["verification"] == "rejected_headers"


def test_a_failed_verification_is_400_stored_with_no_get():
    client = FakeClient({"verification_status": "FAILURE"})
    h = _handle(client)
    assert (h.http_status, h.verification) == (400, "failed") and client.gets() == [] and len(client.posts()) == 1
    row = store.get_event("WH-TEST-1")
    assert row["verification"] == "failed" and row["readback_status"] is None


@pytest.mark.parametrize("error", [paypal_error(500), httpx.ReadTimeout("slow")])
def test_a_verification_limitation_is_503_stored_with_its_text_and_no_get(error):
    client = FakeClient(verify_raises=error)
    h = _handle(client)
    assert (h.http_status, h.verification) == (503, "limitation") and client.gets() == []
    row = store.get_event("WH-TEST-1")
    assert row["verification"] == "limitation" and row["limitation_text"] and "dbg-1" not in row["limitation_text"]


@pytest.mark.parametrize("event_type", service.DISPUTE_EVENTS)
def test_verified_dispute_event_is_one_verify_post_then_one_get_and_the_status_is_the_gets(event_type):
    client = FakeClient(get_answer=dispute_get("WAITING_FOR_BUYER_RESPONSE"))
    ev = event(event_type=event_type, status="OPEN")                      # the event's own status differs on purpose
    h = _handle(client, raw(ev))
    assert (h.http_status, h.verification, h.dispute_id, h.readback_status) == (
        200, "verified", DISPUTE_ID, "WAITING_FOR_BUYER_RESPONSE")
    assert [(c[0], c[1], c[2]) for c in client.calls] == [
        ("POST", "/v1/notifications/verify-webhook-signature", "webhook_verify"),
        ("GET", f"/v1/customer/disputes/{DISPUTE_ID}", "webhook_readback")]
    row = store.get_event("WH-TEST-1")
    assert row["readback_status"] == "WAITING_FOR_BUYER_RESPONSE" != "OPEN"
    assert row["readback_sha256"] == sha256_json(dispute_get("WAITING_FOR_BUYER_RESPONSE"))
    assert row["dispute_id"] == DISPUTE_ID and row["body_sha256"] and row["transmission_id"] == "TRANS-TEST-1"


def test_the_event_bodys_status_is_never_stored_as_a_state_when_the_get_fails():
    client = FakeClient(get_raises=paypal_error(500))
    h = _handle(client, raw(event(status="RESOLVED")))
    assert (h.http_status, h.verification, h.readback_status) == (200, "verified", None)
    row = store.get_event("WH-TEST-1")
    assert row["readback_status"] is None and row["readback_sha256"] is None and row["limitation_text"]
    assert "RESOLVED" not in json.dumps(row)


def test_a_get_failure_by_timeout_is_a_limitation_and_a_programming_error_still_raises():
    assert _handle(FakeClient(get_raises=httpx.ReadTimeout("slow"))).http_status == 200
    with pytest.raises(KeyError):
        _handle(FakeClient(get_raises=KeyError("bug")), raw(event("WH-TEST-9")))


def test_a_verified_event_of_another_type_is_stored_with_no_get():
    client = FakeClient()
    ev = event(event_type="PAYMENT.CAPTURE.COMPLETED", dispute_id=None)
    h = _handle(client, raw(ev))
    assert h.http_status == 200 and h.readback_status is None and client.gets() == [] and len(client.posts()) == 1
    row = store.get_event("WH-TEST-1")
    assert row["dispute_id"] is None and row["event_type"] == "PAYMENT.CAPTURE.COMPLETED" and row["readback_status"] is None


def test_other_type_with_a_dispute_id_in_the_resource_still_gets_no_get():
    client = FakeClient()
    _handle(client, raw(event(event_type="PAYMENT.SALE.COMPLETED")))
    assert client.gets() == []


@pytest.mark.parametrize("bad", ["../../etc/passwd", "PP-R", "pp-r-test-0000001", "x" * 80, "PP-R-TEST/0000001", 12345, ""])
def test_an_invalid_dispute_id_in_the_event_gets_no_get_and_is_stored_with_dispute_id_none(bad):
    client = FakeClient()
    h = _handle(client, raw(event(dispute_id=bad)))
    assert h.http_status == 200 and h.dispute_id is None and client.gets() == [] and len(client.posts()) == 1
    assert store.get_event("WH-TEST-1")["dispute_id"] is None


def test_a_dispute_event_without_a_dispute_id_gets_no_get():
    client = FakeClient()
    h = _handle(client, raw(event(dispute_id=None)))
    assert h.http_status == 200 and client.gets() == []


def test_at_most_two_paypal_calls_per_event():
    client = FakeClient()
    _handle(client)
    assert len(client.calls) == 2


def test_the_stored_row_holds_hashes_never_the_body_or_the_signature():
    _handle(FakeClient())
    row = store.get_event("WH-TEST-1")
    blob = json.dumps(row)
    assert HEADERS["PAYPAL-TRANSMISSION-SIG"] not in blob and "\"status\": \"OPEN\"" not in blob and HEADERS["PAYPAL-CERT-URL"] not in blob


def test_the_whole_feature_never_reads_the_state_from_the_event_body():
    import pathlib
    source = (pathlib.Path(service.__file__)).read_text(encoding="utf-8")
    assert 'resource.get("status")' not in source and 'event.get("status")' not in source


@pytest.fixture(autouse=True)
def enable_existing_verification_contract(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", "true")
