"""verification_body and verify_signature on the fake client."""
import json

import httpx
import pytest

from copilot.features.webhooks import verify
from webhooks_fakes import HEADERS, WEBHOOK_ID, FakeClient, event, paypal_error, raw


def test_all_five_headers_give_the_exact_body_with_the_parsed_event():
    ev = event()
    body = verify.verification_body(HEADERS, raw(ev), WEBHOOK_ID)
    assert body == {"auth_algo": "SHA256withRSA",
                    "cert_url": "https://api-m.sandbox.paypal.com/v1/notifications/certs/CERT-TEST-1",
                    "transmission_id": "TRANS-TEST-1", "transmission_sig": "c2lnbmF0dXJlLWZvci10ZXN0cw==",
                    "transmission_time": "2026-10-06T16:00:00Z", "webhook_id": WEBHOOK_ID, "webhook_event": ev}


def test_the_fixtures_cert_url_is_on_the_sandbox_host_only():
    assert HEADERS["PAYPAL-CERT-URL"].startswith("https://api-m.sandbox.paypal.com/")


def test_header_names_are_case_insensitive_and_a_starlette_style_mapping_works():
    lower = {k.lower(): v for k, v in HEADERS.items()}
    assert verify.verification_body(lower, raw(), WEBHOOK_ID) == verify.verification_body(HEADERS, raw(), WEBHOOK_ID)
    mixed = {k.title(): v for k, v in HEADERS.items()}
    assert verify.verification_body(mixed, raw(), WEBHOOK_ID) is not None


@pytest.mark.parametrize("missing", sorted(HEADERS))
def test_a_missing_or_empty_header_is_none_and_no_call(missing):
    client = FakeClient()
    headers = {k: v for k, v in HEADERS.items() if k != missing}
    assert verify.verification_body(headers, raw(), WEBHOOK_ID) is None
    assert verify.verification_body({**HEADERS, missing: "  "}, raw(), WEBHOOK_ID) is None
    assert verify.verify_signature(client, headers, raw(), WEBHOOK_ID).status == "rejected_headers"
    assert client.calls == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b"", b"null"])
def test_a_body_that_is_not_a_json_object_is_none_and_no_call(body):
    client = FakeClient()
    assert verify.verification_body(HEADERS, body, WEBHOOK_ID) is None
    assert verify.verify_signature(client, HEADERS, body, WEBHOOK_ID).status == "rejected_headers" and client.calls == []


def test_no_webhook_id_is_none_and_no_call():
    client = FakeClient()
    assert verify.verification_body(HEADERS, raw(), None) is None and verify.verification_body(HEADERS, raw(), "") is None
    assert verify.verify_signature(client, HEADERS, raw(), None).status == "rejected_headers" and client.calls == []


def test_success_is_verified_with_exactly_one_post_to_the_verification_path():
    client = FakeClient({"verification_status": "SUCCESS"})
    v = verify.verify_signature(client, HEADERS, raw(), WEBHOOK_ID)
    assert (v.status, v.limitation_text) == ("verified", None)
    assert len(client.calls) == 1
    method, path, op, body = client.calls[0]
    assert (method, path, op) == ("POST", "/v1/notifications/verify-webhook-signature", "webhook_verify")
    assert body["webhook_id"] == WEBHOOK_ID and body["webhook_event"]["id"] == "WH-TEST-1"


@pytest.mark.parametrize("answer", [{"verification_status": "FAILURE"}, {"verification_status": "success"},
                                    {"verification_status": ""}, {}, {"other": 1}, "SUCCESS", None])
def test_anything_but_exactly_success_is_failed(answer):
    client = FakeClient(answer if answer is not None else {"x": 1})
    client.verify_answer = answer
    assert verify.verify_signature(client, HEADERS, raw(), WEBHOOK_ID).status == "failed"


@pytest.mark.parametrize("error", [paypal_error(500), paypal_error(401), paypal_error(400), httpx.ReadTimeout("slow")])
def test_a_paypal_failure_is_a_limitation_with_text(error):
    v = verify.verify_signature(FakeClient(verify_raises=error), HEADERS, raw(), WEBHOOK_ID)
    assert v.status == "limitation" and v.limitation_text and "dbg-1" not in v.limitation_text


def test_a_programming_error_still_raises():
    with pytest.raises(KeyError):
        verify.verify_signature(FakeClient(verify_raises=KeyError("bug")), HEADERS, raw(), WEBHOOK_ID)


def test_the_module_logs_nothing_and_holds_no_host():
    import copilot.features.webhooks.verify as module
    source = open(module.__file__, encoding="utf-8").read()
    assert "logging" not in source and "print(" not in source and "://" not in source
    assert json.loads(raw())["id"] == "WH-TEST-1"
