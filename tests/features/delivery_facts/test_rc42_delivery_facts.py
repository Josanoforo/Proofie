"""Shipment-facts page: the lone 'Carrier comparison: Verified' line is gone and the field names
come from the closed table of field words. Offline: temp database, no client, both web modes.

Expected: the page never carries a carrier 'Verified' tag; the comparison list still says 'Carrier: same in both' or
'Carrier: different - check both'; the record rows read Tracking number, Carrier, Status, Ship date; the discrepancy
sentence names the fields in those same words, lower-case at the start."""
import hashlib
import json
import os
import re
import sqlite3

import pytest
from _dom import parse
from _hosted import authenticated_client

from copilot import config
from copilot.app import create_app
from copilot.web.routes_disputes import get_client

CAP = "DEMO1234567890123"
NUMBER = "DEMO1234567890"
DDL = """CREATE TABLE fulfilment_files(id INTEGER PRIMARY KEY,capture_id TEXT,created_at TEXT,request_json TEXT,
request_sha256 TEXT,approval_sha256 TEXT,tracker_readback_json TEXT,checks_json TEXT,file_sha256 TEXT,status TEXT)"""


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def store(db, request, readback, checks=None, status="registered"):
    checks = checks if checks is not None else {"address_comparison": {"result": "match"}}
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO fulfilment_files VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (None, CAP, "2026-10-08T12:00:00+0000", json.dumps(request), _digest(request), _digest(request),
                   json.dumps(readback), json.dumps(checks),
                   _digest({"request": request, "tracker_readback": readback, "checks": checks}), status))


def url():
    return "/delivery-facts/" + CAP


@pytest.fixture
def prepared(web_mode, monkeypatch):
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_FEATURE_DELIVERY_FACTS", "true")
    db = config.db_path()
    with sqlite3.connect(db) as c:
        c.execute(DDL)
    app = create_app()

    def no_client():
        raise AssertionError("the facts page never obtains a client")
    app.dependency_overrides[get_client] = no_client
    return authenticated_client(app), db


def _request(carrier="FEDEX", number=NUMBER):
    return {"trackers": [{"transaction_id": CAP, "tracking_number": number, "carrier": carrier, "status": "SHIPPED"}]}


def test_the_carrier_verified_line_is_gone_and_the_comparison_list_keeps_the_carrier_result(prepared):
    web, db = prepared
    store(db, request=_request(), readback={"tracking_number": NUMBER, "carrier": "FEDEX", "status": "SHIPPED"})
    text = web.get(url()).text
    dom = parse(text)
    assert "Carrier comparison" not in text and "The carrier values match in the saved records" not in text
    assert dom.find(None, data_state="carrier-comparison") is None
    items = [li.text() for li in dom.find(None, data_state="comparison").find_all("li")]
    assert any(i.endswith("Carrier: same in both") for i in items), items


def test_a_carrier_that_differs_shows_the_list_line_and_no_verified_tag(prepared):
    web, db = prepared
    store(db, request=_request("FEDEX"), readback={"tracking_number": NUMBER, "carrier": "DHL", "status": "SHIPPED"})
    dom = parse(web.get(url()).text)
    items = [li.text() for li in dom.find(None, data_state="comparison").find_all("li")]
    assert any(i.endswith("Carrier: different - check both") for i in items), items
    assert "Verified" not in dom.text()


def test_the_record_rows_and_the_discrepancy_sentence_use_the_field_words(prepared):
    web, db = prepared
    store(db, request=_request("FEDEX", "AAA111"), readback={"tracking_number": "BBB222", "carrier": "DHL",
                                                              "status": "SHIPPED", "shipment_date": "2026-10-07"})
    dom = parse(web.get(url()).text)
    for heading in ("What you entered", "What PayPal returned"):
        section = [s for s in dom.find_all("section") if s.find("h2") and s.find("h2").text() == heading][0]
        assert [dt.text() for dt in section.find_all("dt")] == ["Tracking number", "Carrier", "Status", "Ship date"]
    sentence = dom.find("p", class_="field-error").text()
    assert "differ on: tracking number, carrier." in sentence
    assert not re.search(r"_", sentence)


def test_the_paste_texts_show_display_dates_never_iso_and_keep_digits_in_the_same_order(prepared):
    """English '7 Oct 2026', Spanish '7 oct 2026'; the textareas on the page carry the same words."""
    from copilot.features.delivery_facts import service
    web, db = prepared
    store(db, request={"trackers": [{"transaction_id": CAP, "tracking_number": NUMBER, "carrier": "FEDEX", "status": "SHIPPED",
                                     "shipment_date": "2026-10-06"}]},
          readback={"tracking_number": NUMBER, "carrier": "FEDEX", "status": "SHIPPED", "shipment_date": "2026-10-12"})
    result = service.read(db, CAP)
    assert "ship date 6 Oct 2026" in result["english"] and "ship date 12 Oct 2026" in result["english"]
    assert "fecha de envío 6 oct 2026" in result["spanish"] and "fecha de envío 12 oct 2026" in result["spanish"]
    assert not re.search(r"\d{4}-\d{2}-\d{2}", result["english"] + result["spanish"])
    assert service.DIGIT_RUN.findall(result["english"]) == service.DIGIT_RUN.findall(result["spanish"])
    dom = parse(web.get(url()).text)
    assert "12 Oct 2026" in dom.find("textarea", id="english").text() and "12 oct 2026" in dom.find("textarea", id="spanish").text()
    assert not re.search(r"\d{4}-\d{2}-\d{2}", dom.find("textarea", id="english").text() + dom.find("textarea", id="spanish").text())


def test_a_tracking_number_or_carrier_that_looks_like_a_date_is_never_rewritten(prepared):
    """Only the ship-date value is shown in display form; free text that merely looks like a date stays as saved."""
    from copilot.features.delivery_facts import service
    web, db = prepared
    tricky = "2026-10-07-AB"
    store(db, request={"trackers": [{"transaction_id": CAP, "tracking_number": tricky, "carrier": "FEDEX", "status": "SHIPPED",
                                     "shipment_date": "2026-10-12"}]},
          readback={"tracking_number": tricky, "carrier": "FEDEX", "status": "SHIPPED", "shipment_date": "2026-10-12"})
    result = service.read(db, CAP)
    for text, day in ((result["english"], "12 Oct 2026"), (result["spanish"], "12 oct 2026")):
        assert tricky in text and day in text and "7 Oct 2026" not in text and "7 oct 2026" not in text
    assert tricky in web.get(url()).text


def test_the_ship_date_has_one_format_in_the_record_rows_and_in_the_paste_texts(prepared):
    """One date format on the page: the rows read '12 Oct 2026', the same words as the paste texts (not '12 October 2026')."""
    web, db = prepared
    store(db, request=_request(), readback={"tracking_number": NUMBER, "carrier": "FEDEX", "status": "SHIPPED",
                                            "shipment_date": "2026-10-12"})
    dom = parse(web.get(url()).text)
    section = [s for s in dom.find_all("section") if s.find("h2") and s.find("h2").text() == "What PayPal returned"][0]
    cells = {dt.text(): dt.parent.find_all("dd") for dt in section.find_all("dt")}
    dds = [dd.text() for dd in section.find_all("dd")]
    assert "12 Oct 2026" in dds and "12 October 2026" not in dom.text()
