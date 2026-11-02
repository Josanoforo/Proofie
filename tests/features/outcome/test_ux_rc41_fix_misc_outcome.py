"""The outcome page."""
import re

from outcome_fakes import CAPTURE, DECIDED, DISPUTE_ID, FakePayPal, dispute_raw
from test_outcome_routes import PAGE, _package, _web

from copilot.features.fulfilment import store as fulfil_store
from copilot.features.fulfilment import tracker_request as tr
from copilot.features.resolution import store as res_store


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _save_tracker(status, limitation=None):
    body = tr.build_tracker_body(CAPTURE, "TRK1TEST20261006", "FEDEX", "FedEx")
    sha = tr.request_sha256(body)
    fulfil_store.save_file(CAPTURE, body, sha, sha, None, {"checks": []}, status, limitation_text=limitation)


def test_sh_17_tracker_status_uses_the_tracking_table_not_the_generic_one(monkeypatch):
    _package()
    _save_tracker("unknown", "PayPal did not answer in time, so what happened is unknown.")
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    section = web.get(PAGE).text
    tracker = re.search(r'data-state="tracker">(.*?)</p>', section, re.S).group(1)
    assert "Send attempted - not confirmed by PayPal" in tracker and "Not available" not in tracker


def test_sh_17_a_legacy_failed_tracker_whose_outcome_is_unknown_reads_as_unknown(monkeypatch):
    _package()
    _save_tracker("failed", "PayPal could not register the tracking (error 503). What happened is unknown.")
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    tracker = re.search(r'data-state="tracker">(.*?)</p>', web.get(PAGE).text, re.S).group(1)
    assert "Send attempted - not confirmed by PayPal" in tracker and "Not accepted by PayPal" not in tracker


def test_sh_17_resolution_rows_use_the_message_table_and_say_when_a_message_was_released(monkeypatch):
    _package()
    released = {"message_absent": True, "released": True, "checked_at": "2026-10-16T20:00:00+0000", "minutes_after_send": 12}
    res_store.save_action(DISPUTE_ID, "message", {"message": "hello"}, "c" * 64, "c" * 64, released, True, "sent_unconfirmed")
    res_store.save_action(DISPUTE_ID, "message", {"message": "again"}, "d" * 64, "d" * 64, {"message_listed": True}, True,
                          "sent_confirmed")
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    items = re.findall(r"<li>(.*?)</li>", re.search(r'data-state="resolution-actions">(.*?)</ul>', web.get(PAGE).text, re.S).group(1),
                       re.S)
    assert len(items) == 2
    first, second = _text(items[0]), _text(items[1])
    assert "Pending PayPal confirmation" not in first and "Not available" not in first
    assert "PayPal does not list this message. It can be prepared again." in first
    assert "PayPal lists your message" in second and "does not list" not in second


def test_scope_u_5_outcome_page_shows_no_deadline_for_a_due_date_without_an_offset(monkeypatch):
    raw = dispute_raw(**DECIDED)
    raw["seller_response_due_date"] = "2026-10-17T05:59:59"
    web, _fake = _web(monkeypatch, FakePayPal(raw), appeal=None)
    row = re.search(r"Seller response due</th><td>(.*?)</td>", web.get(PAGE).text, re.S).group(1)
    assert row == "No verified deadline available" and "<time" not in row
    raw["seller_response_due_date"] = "2026-10-17T05:59:59.000Z"
    web, _fake = _web(monkeypatch, FakePayPal(raw), appeal=None)
    row = re.search(r"Seller response due</th><td>(.*?)</td>", web.get(PAGE).text, re.S).group(1)
    assert 'datetime="2026-10-16T23:59:59-06:00"' in row and "16 Oct 2026, 11:59 pm (Mexico City time)" in row
