"""Tests against a synthetic fixture that has the exact JSON shape of a real PayPal sandbox response.

Fixture: fixtures/synthetic/dispute_PP-R-DEMO-0000001_under_review.json (invented ids, DEMO DATA; its shape was taken
from a real sandbox response). Expected values below were read by hand from that fixture, not produced by the code
under test. Variants are derived from the fixture by editing one field; each test says which.
"""
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from client import SANDBOX, SandboxClient
from step3_dispute import fetch_all_disputes, normalize

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic" / "dispute_PP-R-DEMO-0000001_under_review.json"
NOW = datetime(2026, 10, 5, 2, 20, 0, tzinfo=timezone.utc)  # 20:20 CDMX on 2026-10-04
CAPTURE = "DEMOTXN0000000001"


@pytest.fixture
def real():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]


def test_real_response_is_sanitized_in_the_fixture(real):
    text = json.dumps(real)
    assert "@" not in text.replace("[email]", "")      # no raw emails
    assert real["disputed_transactions"][0]["buyer"]["name"] == "[redacted]"


def test_real_under_review_response_without_deadline(real):
    n = normalize(real, NOW, CAPTURE)
    assert n["dispute_id"] == "PP-R-DEMO-0000001"
    assert n["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert n["status"] == "UNDER_REVIEW" and n["stage"] == "INQUIRY"
    assert (n["amount_cents"], n["amount_display"], n["currency"]) == (2500, "25.00", "USD")
    assert n["seller_transaction_ids"] == [CAPTURE] and n["capture_matches_expected"] is True
    assert n["buyer_transaction_ids"] == ["DEMOTXN0000000002"]
    assert n["create_time"] == "2026-10-05T02:13:17.163Z"
    # PayPal sent no deadline and no actions in this state: code must say so, not invent them
    assert n["seller_response_due_date"] is None and n["deadline_parse_ok"] is None
    assert n["remaining_hours"] is None and n["remaining_text"] == "no deadline in response"
    assert n["available_actions"] == [] and n["can_provide_evidence"] is False
    assert n["evidence_requested_from_seller"] == [] and n["evidence_submitted_by_seller"] == []


WAITING = Path(__file__).parent / "fixtures" / "synthetic" / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json"


@pytest.fixture
def real_waiting():
    return json.loads(WAITING.read_text(encoding="utf-8"))["body"]


def test_real_waiting_response_uses_underscore_rels_and_is_recognised(real_waiting):
    """Regression: the API sends 'provide_evidence' (underscore) while the docs say 'provide-evidence'.
    The first version of normalize() looked for the hyphen and wrongly reported can_provide_evidence=False."""
    assert {l["rel"] for l in real_waiting["links"]} == {
        "self", "accept_claim", "provide_evidence", "send_message", "escalate", "make_offer"}   # read by hand
    n = normalize(real_waiting, NOW, CAPTURE)
    assert n["status"] == "WAITING_FOR_SELLER_RESPONSE" and n["stage"] == "INQUIRY"
    assert n["can_provide_evidence"] is True
    assert n["available_actions"] == ["accept-claim", "escalate", "make-offer", "provide-evidence", "send-message"]
    # the send step must use PayPal's own link, not a URL we build
    assert n["action_links"]["provide-evidence"] == {
        "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-DEMO-0000001/provide-evidence",
        "method": "POST"}
    assert n["evidence_requested_from_seller"] == ["PROOF_OF_FULFILLMENT"]
    assert n["evidence_submitted_by_seller"] == []
    assert n["seller_transaction_ids"] == [CAPTURE] and n["capture_matches_expected"] is True


def test_real_waiting_response_deadline_computed_by_code(real_waiting):
    n = normalize(real_waiting, NOW, CAPTURE)
    # due 2026-10-25T02:13:17.163Z, NOW 2026-10-05T02:20:00Z -> 19 d 23 h 53 min 17 s -> 479 h = 19 d 23 h
    assert (n["remaining_days"], n["remaining_hours"], n["remaining_text"]) == (19, 479, "19d 23h")
    assert n["deadline_cdmx"].startswith("2026-10-24 20:13") and n["overdue"] is False
    assert n["deadline_parse_ok"] is True


def test_real_response_wrong_expected_capture_is_flagged(real):
    assert normalize(real, NOW, "SOMEOTHERCAPTURE")["capture_matches_expected"] is False


def test_derived_other_currency_from_real_response(real):
    d = copy.deepcopy(real)                                  # DERIVED: only dispute_amount edited
    d["dispute_amount"] = {"currency_code": "MXN", "value": "435.77"}
    n = normalize(d, NOW, CAPTURE)
    assert (n["amount_cents"], n["amount_display"], n["currency"]) == (43577, "435.77", "MXN")


def test_derived_waiting_for_seller_with_deadline_and_evidence_link(real):
    d = copy.deepcopy(real)                                  # DERIVED: status/links/deadline edited by hand
    d["status"] = "WAITING_FOR_SELLER_RESPONSE"
    d["seller_response_due_date"] = "2026-10-15T02:13:17.000Z"
    d["links"].append({"href": "https://x/provide-evidence", "rel": "provide-evidence", "method": "POST"})
    d["evidences"].append({"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"})
    n = normalize(d, NOW, CAPTURE)
    assert n["can_provide_evidence"] is True and n["available_actions"] == ["provide-evidence"]
    assert n["evidence_requested_from_seller"] == ["PROOF_OF_FULFILLMENT"]
    # 2026-10-15T02:13:17Z minus 2026-10-05T02:20:00Z = 9 days 23 h 53 min 17 s -> 9 d 23 h
    assert (n["remaining_days"], n["remaining_hours"], n["remaining_text"]) == (9, 239, "9d 23h")
    assert n["deadline_cdmx"].startswith("2026-10-14 20:13")   # 02:13Z is 20:13 of the previous day in CDMX


# ---- pagination follows PayPal's own `next` link, not a URL we build -------------------------
FIX = Path(__file__).parent / "fixtures" / "synthetic"
LIST_PAGE1 = json.loads((FIX / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE1_NEXT = json.loads((FIX / "disputes_list_page1_with_next.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE2 = json.loads((FIX / "disputes_list_page2.json").read_text(encoding="utf-8"))["body"]


def _client_for_pages(tmp_path: Path, pages_by_path: dict) -> SandboxClient:
    """pages_by_path: {path-or-query-suffix: body}. Routes GETs by the request path+query."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        key = str(req.url)[len(SANDBOX):]
        for k, body in pages_by_path.items():
            if key == k or key.endswith(k):
                return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected request: {key}")

    c = SandboxClient.__new__(SandboxClient)
    c._http = httpx.Client(base_url=SANDBOX, transport=httpx.MockTransport(handler))
    c._token, c._token_expiry = "x", time.time() + 3600
    c.log_path = tmp_path / "pagination_probe.jsonl"
    c.calls = calls
    return c


def test_fetch_all_disputes_real_single_page_no_next_link(tmp_path):
    c = _client_for_pages(tmp_path, {"/v1/customer/disputes": LIST_PAGE1})
    items, pages = fetch_all_disputes(c)
    assert pages == 1 and len(items) == 1 and items[0]["dispute_id"] == "PP-R-DEMO-0000001"


def test_fetch_all_disputes_follows_next_link_across_two_pages(tmp_path):
    c = _client_for_pages(tmp_path, {
        "/v1/customer/disputes": LIST_PAGE1_NEXT,
        "/v1/customer/disputes?next_page_token=SYNTHETIC-TOKEN-1&page_size=1": LIST_PAGE2,
    })
    items, pages = fetch_all_disputes(c)
    assert pages == 2 and len(items) == 2
    assert {it["dispute_id"] for it in items} == {"PP-R-DEMO-0000001", "PP-R-DEMO-SECONDPAGE"}
    # it used PayPal's own next href verbatim, not a URL it constructed
    assert c.calls[1].endswith("next_page_token=SYNTHETIC-TOKEN-1&page_size=1")


def test_fetch_all_disputes_stops_and_raises_on_a_pagination_loop(tmp_path):
    looping_page = copy.deepcopy(LIST_PAGE1_NEXT)
    c = _client_for_pages(tmp_path, {
        "/v1/customer/disputes": looping_page,
        "/v1/customer/disputes?next_page_token=SYNTHETIC-TOKEN-1&page_size=1": looping_page,
    })
    with pytest.raises(RuntimeError, match="pagination loop"):
        fetch_all_disputes(c, max_pages=3)
    assert len(c.calls) == 3   # stopped at the cap, did not loop forever
