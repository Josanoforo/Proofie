"""Body shape (sandbox-accepted shape), validation and the sha256 canon on a pinned fixture."""
import hashlib
import json

import pytest

from copilot.features.fulfilment import tracker_request as tr

SAMPLE_BODY = {"trackers": [{"transaction_id": "5S647034CC1979815", "tracking_number": "TRK1TEST20261006",
                           "status": "DELIVERED", "carrier": "FEDEX", "carrier_name_other": "FedEx"}]}
SAMPLE_SHA = "f608469c4b9d67974a7c788e073c09d0c2a603446c0f94762bae6ad2d64fcda3"
CAP = "5S647034CC1979815"


def test_body_equals_the_trk1_shape():
    assert tr.build_tracker_body(CAP, "TRK1TEST20261006", "FEDEX", "FedEx", status="DELIVERED") == SAMPLE_BODY


def test_default_status_is_shipped_and_name_is_omitted_when_not_supplied():
    body = tr.build_tracker_body(CAP, "N1", "FEDEX", None)
    assert body == {"trackers": [{"transaction_id": CAP, "tracking_number": "N1", "status": "SHIPPED",
                                  "carrier": "FEDEX"}]}


def test_form_helper_maps_a_free_text_catalogue_carrier():
    body = tr.body_from_form(CAP, "N1", "fedex")
    t = body["trackers"][0]
    assert (t["carrier"], t["status"], t["carrier_name_other"]) == ("FEDEX", "SHIPPED", "fedex")


def test_unmapped_carrier_goes_as_other_with_its_name():
    t = tr.body_from_form(CAP, "N1", "Transportes Locales SA")["trackers"][0]
    assert (t["carrier"], t["carrier_name_other"]) == ("OTHER", "Transportes Locales SA")


def test_other_without_a_name_is_refused():
    with pytest.raises(ValueError):
        tr.build_tracker_body(CAP, "N1", "OTHER", None)
    with pytest.raises(ValueError):
        tr.body_from_form(CAP, "N1", "   ")


@pytest.mark.parametrize("status", ["delivered", "LOST", "", "IN_TRANSIT"])
def test_invalid_status_is_refused(status):
    with pytest.raises(ValueError):
        tr.build_tracker_body(CAP, "N1", "FEDEX", None, status=status)


@pytest.mark.parametrize("status", tr.STATUSES)
def test_every_documented_status_is_accepted(status):
    assert tr.build_tracker_body(CAP, "N1", "UPS", None, status=status)["trackers"][0]["status"] == status


@pytest.mark.parametrize("cap", ["", "SHORT", "5S647034CC1979815X", "5S647034CC19798/5", "../etc/passwd0000"])
def test_bad_capture_ids_are_refused(cap):
    assert tr.validate_capture_id(cap)
    with pytest.raises(ValueError):
        tr.build_tracker_body(cap, "N1", "FEDEX", None)


@pytest.mark.parametrize("number", ["", "   ", "has space", "x" * 65])
def test_bad_tracking_numbers_are_refused(number):
    with pytest.raises(ValueError):
        tr.build_tracker_body(CAP, number, "FEDEX", None)


def test_unknown_carrier_code_is_refused():
    with pytest.raises(ValueError):
        tr.build_tracker_body(CAP, "N1", "NOT_A_CARRIER", None)


def test_sha256_equals_the_pinned_fixture_value():
    assert tr.request_sha256(SAMPLE_BODY) == SAMPLE_SHA


def test_sha256_is_the_send_path_canon_written_out_independently():
    canon = json.dumps(SAMPLE_BODY, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert tr.request_sha256(SAMPLE_BODY) == hashlib.sha256(canon).hexdigest()


def test_sha256_is_stable_across_key_order_and_changes_with_content():
    t = SAMPLE_BODY["trackers"][0]
    reordered = {"trackers": [dict(reversed(list(t.items())))]}
    assert tr.request_sha256(reordered) == SAMPLE_SHA
    changed = {"trackers": [{**t, "tracking_number": "TRK1TEST20261007"}]}
    assert tr.request_sha256(changed) != SAMPLE_SHA
