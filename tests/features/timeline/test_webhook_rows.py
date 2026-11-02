"""W2-2: PayPal's webhook notices on the timeline, verified ones only (hosted copy: WEBHOOKS and WEBHOOKS_VERIFY on).

Offline. A fake client answers the signature check, the read-back GET and the dispute page's reads; no PayPal call is made.
Expected: a signed simulator event posted to /webhooks/paypal is verified (one POST to the signature check, then one GET) and
the dispute page shows "PayPal notified Proofie at <time> (verified)" with the status Proofie itself read afterwards; an unsigned
or badly signed event is refused, logged by the audit row `webhook_event` and never shown; the event's own status never
appears; a notice of another dispute, or any row that is not verified, never appears; with WEBHOOKS off nothing is read.
"""
import json
import re

import pytest

from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.features.timeline import service
from copilot.features.timeline.service import Event, webhook_events
from copilot.features.webhooks import store
from copilot.web.routes_disputes import get_client
from _dom import parse
from _hosted import authenticated_client

import test_ux_rc41_dispute as dt
from test_ux_rc41_dispute import DID

pytestmark = [pytest.mark.usefixtures("web_mode")]

WEBHOOK_ID = "WH-REGISTERED-1"
HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api-m.sandbox.paypal.com/v1/notifications/certs/CERT-TEST-1",
    "PAYPAL-TRANSMISSION-ID": "TRANS-TEST-1",
    "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJlLWZvci10ZXN0cw==",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-06T16:00:00Z",
}
LYING_STATUS = "RESOLVED"
READBACK = "UNDER_REVIEW"
SENTENCE = re.compile(r"PayPal notified Proofie at\s*\d{1,2} \w{3} \d{4}, \d{1,2}:\d{2} (am|pm) \(Mexico City time\)\s*\(verified\)")


def simulator_event(event_id="WH-SIM-1", dispute_id=DID, event_type="CUSTOMER.DISPUTE.UPDATED"):
    resource = {"status": LYING_STATUS}          # the notice says something else on purpose: it must never be shown
    if dispute_id is not None:
        resource["dispute_id"] = dispute_id
    return json.dumps({"id": event_id, "event_type": event_type, "resource_type": "dispute", "resource": resource}).encode()


class Hub:
    """The one fake the app is given: signature check, read-back GET and the dispute page's own reads."""

    def __init__(self, body=None, verify="SUCCESS"):
        self.body, self.verify, self.calls = body if body is not None else dt._body(), verify, []

    def call(self, method, path, op, **kw):
        self.calls.append((method, op))
        if op == "webhook_verify":
            assert method == "POST"
            return {"verification_status": self.verify}
        if op == "webhook_readback":
            return {**self.body, "status": READBACK}
        if op == "show_dispute":
            return self.body
        if op == "recovery_get":
            return {"evidences": []}
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


@pytest.fixture
def flags(monkeypatch):
    for name in ("TIMELINE", "WEBHOOKS", "WEBHOOKS_VERIFY"):
        monkeypatch.setenv("COPILOT_FEATURE_" + name, "true")
    monkeypatch.setenv("COPILOT_WEBHOOK_ID", WEBHOOK_ID)
    return monkeypatch


def make(hub):
    app = create_app()
    app.dependency_overrides[get_client] = lambda: hub
    return authenticated_client(app, client=("127.0.0.1", 12345))


def section(web):
    response = web.get(f"/disputes/{DID}/workspace/records")
    assert response.status_code == 200
    return response.text, parse(response.text).find("section", data_state="paypal-timeline")


def notified(sec):
    return [li for li in sec.find_all("li") if "PayPal notified Proofie" in li.text()]


# ------------------------------------------------------------------------------------------------ exit test 1: verified and shown
def test_a_simulator_event_is_verified_and_shown_with_the_status_proofie_read(flags):
    hub = Hub()
    web = make(hub)
    posted = web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    assert posted.status_code == 200 and posted.json() == {"verification": "verified", "readback_status": READBACK}
    assert hub.calls == [("POST", "webhook_verify"), ("GET", "webhook_readback")]
    text, sec = section(web)
    (row,) = notified(sec)
    assert SENTENCE.search(row.text()), row.text()
    assert "Proofie's own read of the dispute afterwards: Under review by PayPal" in row.text()
    assert row.attrs["data-origin"] == "verified_event" and row.attrs["data-kind"] == "verified"
    assert sec.find("p", data_state="paypal-timeline-notices") is not None


def test_the_notices_sentence_is_there_only_when_a_notice_is(flags):
    web = make(Hub())
    assert section(web)[1].find("p", data_state="paypal-timeline-notices") is None


def test_the_events_own_status_is_never_shown(flags):
    web = make(Hub())
    web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    text, sec = section(web)
    assert "Resolved" not in sec.text() and LYING_STATUS not in text[text.index('data-state="paypal-timeline"'):]
    assert store.get_event("WH-SIM-1")["readback_status"] == READBACK


def test_the_notice_sits_among_PayPals_dated_items_by_time(flags):
    web = make(Hub())
    web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    items = [li.text() for li in section(web)[1].find_all("li")]
    assert "PayPal notified Proofie" in items[-1], items          # received now: later than every 2026-10-05 item PayPal dated


# ------------------------------------------------------------------------------------------------ exit test 2: refused, logged, not shown
def test_an_unsigned_event_is_refused_logged_and_not_shown(flags):
    hub = Hub()
    web = make(hub)
    refused = web.post("/webhooks/paypal", content=simulator_event(), headers={})
    assert refused.status_code == 400 and refused.json()["verification"] == "rejected_headers"
    assert hub.calls == []                                          # no signature check, no GET: nothing to trust
    assert store.get_event("WH-SIM-1")["verification"] == "rejected_headers"
    logged = [e for e in repo.list_audit(config.db_path(), DID) if e["action"] == "webhook_event"]
    assert len(logged) == 1
    assert notified(section(web)[1]) == []


def test_a_badly_signed_event_is_refused_logged_and_not_shown(flags):
    hub = Hub(verify="FAILURE")
    web = make(hub)
    refused = web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    assert refused.status_code == 400 and refused.json()["verification"] == "failed"
    assert hub.calls == [("POST", "webhook_verify")]                # the signature check only: no read-back from a forged event
    assert [e for e in repo.list_audit(config.db_path(), DID) if e["action"] == "webhook_event"]
    assert notified(section(web)[1]) == []


def test_an_unverified_row_is_never_shown(flags):
    store.save_event("WH-U", "CUSTOMER.DISPUTE.UPDATED", "dispute", DID, None, "0" * 64, "unverified")
    store.save_event("WH-L", "CUSTOMER.DISPUTE.UPDATED", "dispute", DID, None, "1" * 64, "limitation", limitation_text="x")
    assert notified(section(make(Hub()))[1]) == []


# ------------------------------------------------------------------------------------------------ scope
def test_another_disputes_notice_is_not_shown(flags):
    web = make(Hub())
    web.post("/webhooks/paypal", content=simulator_event("WH-OTHER", dispute_id="PP-R-OTHER-0000009"), headers=HEADERS)
    web.post("/webhooks/paypal", content=simulator_event("WH-NONE", dispute_id=None, event_type="PAYMENT.CAPTURE.COMPLETED"),
             headers=HEADERS)
    assert notified(section(web)[1]) == []


def test_with_the_webhooks_flag_off_the_stored_rows_are_not_read(flags, monkeypatch):
    web = make(Hub())
    web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    assert len(notified(section(web)[1])) == 1
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS", "false")
    assert notified(section(web)[1]) == []


def test_with_the_timeline_flag_off_the_page_has_no_section_even_with_notices(flags, monkeypatch):
    web = make(Hub())
    web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    monkeypatch.setenv("COPILOT_FEATURE_TIMELINE", "false")
    text = web.get(f"/disputes/{DID}").text
    assert "paypal-timeline" not in text and "PayPal notified Proofie" not in text


def test_a_duplicate_delivery_shows_one_row(flags):
    web = make(Hub())
    for _ in range(3):
        web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    assert len(notified(section(web)[1])) == 1


def test_the_dispute_page_adds_no_write_and_no_call_for_the_notices(flags):
    hub = Hub()
    web = make(hub)
    web.post("/webhooks/paypal", content=simulator_event(), headers=HEADERS)
    before = len(hub.calls)
    section(web)
    new = hub.calls[before:]
    assert all(method == "GET" for method, _ in new) and ("GET", "webhook_readback") not in new


# ------------------------------------------------------------------------------------------------ the pure mapping
def row(**update):
    base = {"id": 1, "received_at": "2026-10-19T22:45:00-0600", "event_id": "WH-1", "event_type": "CUSTOMER.DISPUTE.UPDATED",
            "resource_type": "dispute", "dispute_id": DID, "transmission_id": "T", "body_sha256": "0" * 64,
            "verification": "verified", "readback_status": "WAITING_FOR_SELLER_RESPONSE", "readback_sha256": "1" * 64,
            "limitation_text": None}
    base.update(update)
    return base


def test_only_verified_rows_become_events_with_the_read_back_status_in_words():
    events = webhook_events([row(), row(event_id="WH-2", verification="failed"), row(event_id="WH-3", verification="unverified"),
                             row(event_id="WH-4", verification="limitation"), row(event_id="WH-5", verification="rejected_headers")])
    assert [(e.kind, e.origin, e.text, e.detail) for e in events] == [
        ("verified", "verified_event", "PayPal notified Proofie",
         ("Proofie's own read of the dispute afterwards: Waiting for your response",))]
    assert events[0].at == "2026-10-19T22:45:00-0600"
    assert events[0].instant is not None


@pytest.mark.parametrize("status", [None, "", "<script>", "x" * 90, "lower case", 5])
def test_a_read_back_status_that_is_not_a_plain_code_is_not_echoed(status):
    (event,) = webhook_events([row(readback_status=status)])
    assert event.detail == ("Proofie has no read of the dispute stored for this notice",)
    assert "script" not in repr(event)


@pytest.mark.parametrize("rows", [None, [], "x", 5, [None, 3, "s", [], {}], [{"verification": "verified"}]])
def test_malformed_rows_never_raise(rows):
    assert isinstance(webhook_events(rows), list)


def test_a_received_time_that_cannot_be_read_goes_last_and_reads_not_available():
    raw = {"create_time": "2026-10-05T02:13:17Z"}
    events = service.build(raw, None, verified=webhook_events([row(received_at="not a time")]))
    assert [e.kind for e in events] == ["opened", "verified"] and events[1].instant is None


def test_the_row_time_is_read_in_the_offset_the_store_writes():
    (event,) = webhook_events([row(received_at="2026-10-19T22:45:00-0600")])
    assert event.instant.isoformat() == "2026-10-20T04:45:00+00:00"
    assert isinstance(event, Event)
