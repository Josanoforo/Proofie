"""register_tracker on a fake client: order, ops, paths, the approval guard and every failure as a Limitation."""
import httpx
import pytest

from copilot.core.disputes import PayPalError
from copilot.features.fulfilment import register as reg
from copilot.features.fulfilment import tracker_request as tr

CAP = "5S647034CC1979815"
BODY = tr.build_tracker_body(CAP, "TRK1TEST20261006", "FEDEX", "FedEx", status="DELIVERED")
SHA = tr.request_sha256(BODY)
LISTED = {"trackers": [{"transaction_id": CAP, "tracking_number": "TRK1TEST20261006", "status": "DELIVERED",
                        "carrier": "FEDEX", "shipment_date": "2026-10-06", "last_updated_time": 1791299995286,
                        "links": [{"rel": "edit"}, {"rel": "replace"}, {"rel": "self"}]}], "links": []}


class Fake:
    def __init__(self, post=None, get=None, post_raises=None, get_raises=None):
        self.post, self.get = post if post is not None else {"trackers": [{"links": []}]}, get
        self.post_raises, self.get_raises = post_raises, get_raises
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op, kw))
        if method == "POST":
            if self.post_raises:
                raise self.post_raises
            return self.post
        if self.get_raises:
            raise self.get_raises
        return self.get if self.get is not None else {"trackers": []}


def test_201_then_a_listing_confirms_and_the_two_calls_are_in_order():
    fake = Fake(get=LISTED)
    r = reg.register_tracker(fake, BODY, SHA)
    assert (r.status, r.confirmed, r.post_accepted, r.limitations) == ("registered", True, True, ())
    assert [(c[0], c[2]) for c in fake.calls] == [("POST", "fulfilment_tracker_post"),
                                                  ("GET", "fulfilment_tracker_get")]
    assert fake.calls[0][1] == "/v1/shipping/trackers"
    assert fake.calls[1][1] == f"/v1/shipping/trackers?transaction_id={CAP}"
    assert fake.calls[0][3]["json_body"] == BODY
    assert r.tracker == {"tracking_number": "TRK1TEST20261006", "status": "DELIVERED", "carrier": "FEDEX",
                         "shipment_date": "2026-10-06", "last_updated_time": 1791299995286,
                         "link_rels": ["edit", "replace", "self"]}
    assert r.request_sha256 == SHA


def test_every_call_stays_under_the_tracker_path_prefix_and_never_names_a_host():
    fake = Fake(get=LISTED)
    reg.register_tracker(fake, BODY, SHA)
    for _m, path, _op, _kw in fake.calls:
        assert path.startswith("/v1/shipping/trackers") and "://" not in path


def test_the_same_body_always_gets_the_same_paypal_request_id():
    a, b = Fake(get=LISTED), Fake(get=LISTED)
    reg.register_tracker(a, BODY, SHA)
    reg.register_tracker(b, BODY, SHA)
    assert a.calls[0][3]["request_id"] == b.calls[0][3]["request_id"]


def test_an_empty_listing_is_registered_unconfirmed():
    fake = Fake(get={"trackers": []})
    r = reg.register_tracker(fake, BODY, SHA)
    assert (r.status, r.confirmed, r.post_accepted, r.tracker) == ("registered_unconfirmed", False, True, None)
    assert len(r.limitations) == 1 and len(fake.calls) == 2


def test_a_listing_with_another_number_is_not_a_confirmation():
    other = {"trackers": [{"tracking_number": "SOMETHING-ELSE", "status": "SHIPPED"}]}
    r = reg.register_tracker(Fake(get=other), BODY, SHA)
    assert (r.status, r.confirmed) == ("registered_unconfirmed", False)


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_a_definite_4xx_is_failed_with_a_limitation_and_there_is_no_get(status):
    fake = Fake(post_raises=PayPalError(status, "ISSUE", "dbg-123", {}))
    r = reg.register_tracker(fake, BODY, SHA)
    assert (r.status, r.confirmed, r.post_status, r.post_accepted) == ("failed", False, status, False)
    assert r.reason == "ISSUE"                                   # PayPal's own reason, never its debug id
    assert [c[0] for c in fake.calls] == ["POST"]
    text = r.limitations[0].message
    assert "dbg-123" not in text and "ISSUE" not in text


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_5xx_or_429_is_unknown_not_failed_and_there_is_no_get(status):
    # PayPal may have saved the tracker; "failed" would say it did not.
    fake = Fake(post_raises=PayPalError(status, "ISSUE", "dbg-123", {}))
    r = reg.register_tracker(fake, BODY, SHA)
    assert (r.status, r.confirmed, r.post_status, r.post_accepted) == ("unknown", False, status, False)
    assert [c[0] for c in fake.calls] == ["POST"]
    assert "unknown" in r.limitations[0].message and "dbg-123" not in r.limitations[0].message


def test_a_timeout_on_the_post_is_unknown_and_the_limitation_says_so():
    fake = Fake(post_raises=httpx.ReadTimeout("slow"))
    r = reg.register_tracker(fake, BODY, SHA)
    assert r.status == "unknown" and "unknown" in r.limitations[0].message
    assert [c[0] for c in fake.calls] == ["POST"]


def test_a_failing_get_after_an_accepted_post_is_unconfirmed_with_a_limitation():
    r = reg.register_tracker(Fake(get_raises=PayPalError(500, None, None, {})), BODY, SHA)
    assert (r.status, r.confirmed, r.post_accepted) == ("registered_unconfirmed", False, True)
    assert "read the tracker back" in r.limitations[0].message


def test_errors_in_the_post_answer_are_reported_even_when_the_get_lists_the_tracker():
    r = reg.register_tracker(Fake(post={"errors": [{"name": "X"}]}, get=LISTED), BODY, SHA)
    assert r.confirmed and len(r.limitations) == 1


def test_a_programming_error_still_raises():
    with pytest.raises(KeyError):
        reg.register_tracker(Fake(post_raises=KeyError("bug")), BODY, SHA)


@pytest.mark.parametrize("wrong", ["", "0" * 64, SHA.upper(), "approved-something-else"])
def test_a_wrong_approval_sha256_is_refused_before_any_client_call(wrong):
    fake = Fake(get=LISTED)
    with pytest.raises(reg.ApprovalMismatch):
        reg.register_tracker(fake, BODY, wrong)
    assert fake.calls == []


def test_an_approval_for_another_body_does_not_cover_this_one():
    other = tr.build_tracker_body(CAP, "ANOTHER-NUMBER", "FEDEX", "FedEx")
    fake = Fake(get=LISTED)
    with pytest.raises(reg.ApprovalMismatch):
        reg.register_tracker(fake, BODY, tr.request_sha256(other))
    assert fake.calls == []
