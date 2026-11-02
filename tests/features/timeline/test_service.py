"""PayPal's own timeline (W1-2): copilot/features/timeline/service.py `build(raw, read_at) -> list[Event]`.

Pure and offline: inline dispute dicts shaped like PayPal's GET /v1/customer/disputes/{id}. Expected before the code: the
dated items PayPal gives (create_time, evidences, messages, offer.history, adjudications, supporting_info, money_movements,
the contact times, update_time) come out as events sorted by INSTANT (not by text), each with PayPal's own time text kept
as given; an item with no usable instant is listed last and reads "Not available"; a message body, an evidence note, a URL
and any enum PayPal sent that the closed tables do not know never reach an event; nothing here reads a clock, so the
results do not depend on the day the suite runs.
"""
from datetime import datetime, timezone

import pytest

from copilot.features.timeline import service
from copilot.features.timeline.service import Event, build, verified_event
from copilot.web import timefmt

READ_AT = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
BODY_TEXT = "BUYER-WROTE-THIS-SECRET-BODY"
NOTE_TEXT = "EVIDENCE-NOTE-SECRET"
URL = "https://api-m.sandbox.paypal.com/v2/content/documents/X/files/1/download"


def raw_dispute(**update):
    raw = {
        "dispute_id": "PP-R-TEST-1",
        "create_time": "2026-10-05T02:13:17.163Z",
        "update_time": "2026-10-09T18:00:00.000Z",
        "evidences": [
            {"evidence_type": "CREATE", "source": "SUBMITTED_BY_BUYER", "date": "2026-10-05T02:13:17.163Z",
             "notes": NOTE_TEXT, "documents": [{"name": "buyer-photo.jpg", "url": URL}]},
            {"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER", "date": "2026-10-05T02:16:52.810Z"},
            {"evidence_type": "PROOF_OF_FULFILLMENT", "source": "SUBMITTED_BY_SELLER", "date": "2026-10-07T15:30:00.000Z",
             "documents": [{"name": "label.pdf", "url": URL}, {"name": "receipt.pdf"}],
             "evidence_info": {"tracking_info": [{"tracking_number": "T1", "carrier_name": "FEDEX"}]}},
        ],
        "messages": [{"posted_by": "BUYER", "time_posted": "2026-10-06T10:00:00.000Z", "content": BODY_TEXT,
                      "documents": [{"name": "msg-scan.png", "url": URL}]},
                     {"posted_by": "SELLER", "time_posted": "2026-10-06T11:00:00.000Z", "content": BODY_TEXT}],
        "offer": {"buyer_requested_amount": {"currency_code": "USD", "value": "25.00"},
                  "history": [{"actor": "SELLER", "event_type": "PROPOSED", "offer_time": "2026-10-08T01:00:00.000Z",
                               "offer_amount": {"currency_code": "USD", "value": "10.00"}, "notes": NOTE_TEXT},
                              {"actor": "BUYER", "event_type": "DENIED", "offer_time": "2026-10-08T05:00:00.000Z"}]},
        "adjudications": [{"type": "DENY_BUYER", "adjudication_time": "2026-10-09T17:00:00.000Z",
                           "reason": "ITEM_NOT_DELIVERED"}],
        "supporting_info": [{"source": "SUBMITTED_BY_BUYER", "provided_time": "2026-10-06T09:00:00.000Z", "notes": NOTE_TEXT,
                             "documents": [{"name": "support.pdf"}]}],
        "money_movements": [{"type": "DEBIT", "affected_party": "SELLER", "initiated_time": "2026-10-09T17:30:00.000Z",
                             "reason": "DISPUTE_FEE", "amount": {"currency_code": "USD", "value": "20.00"}}],
        "extensions": {"buyer_contacted_time": "2026-10-05T02:13:16.127Z", "merchant_contacted_time": "2026-10-04T12:00:00.000Z"},
    }
    raw.update(update)
    return raw


def summary(events):
    return [(e.kind, e.text) for e in events]


def test_every_dated_item_PayPal_gives_becomes_one_event_in_instant_order():
    events = build(raw_dispute(), READ_AT)
    assert summary(events) == [
        ("contact", "Seller contact time recorded by PayPal"),
        ("contact", "Buyer contact time recorded by PayPal"),
        ("opened", "Dispute opened"),
        ("evidence", "Buyer submitted evidence"),
        ("evidence", "PayPal asked the seller for evidence: Proof of fulfillment"),
        ("supporting_info", "The buyer provided supporting information"),
        ("message", "The buyer posted a message"),
        ("message", "The seller posted a message"),
        ("evidence", "Seller submitted evidence: Proof of fulfillment"),
        ("offer", "The seller made an offer"),
        ("offer", "The buyer declined the offer"),
        ("adjudication", "PayPal denied the buyer's claim"),
        ("money", "A debit of 20.00 USD was initiated"),
        ("updated", "PayPal last updated the dispute"),
    ]
    assert [e.instant for e in events] == sorted(e.instant for e in events)


def test_events_keep_PayPals_own_time_text_and_carry_an_aware_instant():
    events = {e.text: e for e in build(raw_dispute(), READ_AT)}
    opened = events["Dispute opened"]
    assert opened.at == "2026-10-05T02:13:17.163Z"
    assert opened.instant == datetime(2026, 10, 5, 2, 13, 17, 163000, tzinfo=timezone.utc)
    assert opened.origin == "paypal_dispute" and opened.after_read is False


def test_order_follows_the_instant_not_the_text_of_the_timestamp():
    # 23:00 at UTC-6 is 05:00 UTC on the 6th: later than 02:00 UTC on the 6th although its text sorts earlier
    raw = {"evidences": [{"source": "SUBMITTED_BY_SELLER", "date": "2026-10-05T23:00:00.000-06:00"},
                         {"source": "SUBMITTED_BY_BUYER", "date": "2026-10-06T02:00:00.000Z"}]}
    assert [e.text for e in build(raw, READ_AT)] == ["Buyer submitted evidence", "Seller submitted evidence"]


def test_equal_instants_keep_PayPals_order_and_the_opening_comes_first():
    raw = {"create_time": "2026-10-05T02:13:17.163Z",
           "evidences": [{"source": "SUBMITTED_BY_BUYER", "date": "2026-10-05T02:13:17.163Z"}]}
    assert [e.kind for e in build(raw, READ_AT)] == ["opened", "evidence"]


def test_an_item_with_no_offset_is_listed_last_and_reads_not_available():
    raw = {"create_time": "2026-10-05T02:13:17Z",
           "evidences": [{"source": "SUBMITTED_BY_BUYER", "date": "2026-10-04T10:00:00"}]}      # no offset: no known instant
    events = build(raw, READ_AT)
    assert [e.kind for e in events] == ["opened", "evidence"]
    assert events[1].instant is None and events[1].at == "2026-10-04T10:00:00"
    assert timefmt.display(events[1].at) == "Not available"


def test_an_item_with_no_date_at_all_is_not_a_dated_item_and_is_left_out():
    raw = {"evidences": [{"source": "SUBMITTED_BY_BUYER"}, {"source": "SUBMITTED_BY_SELLER", "date": ""}],
           "messages": [{"posted_by": "BUYER"}], "offer": {"history": [{"actor": "BUYER", "event_type": "PROPOSED"}]}}
    assert build(raw, READ_AT) == []


def test_no_message_body_note_or_url_reaches_any_event():
    blob = repr(build(raw_dispute(), READ_AT))
    for secret in (BODY_TEXT, NOTE_TEXT, "https://", "api-m.sandbox", "download"):
        assert secret not in blob, secret


def test_documents_are_named_by_file_name_only():
    events = {e.text: e for e in build(raw_dispute(), READ_AT)}
    assert events["Seller submitted evidence: Proof of fulfillment"].detail == (
        "Files: label.pdf, receipt.pdf", "Tracking information for 1 shipment")
    assert events["The buyer posted a message"].detail == ("Files: msg-scan.png",)
    assert events["Buyer submitted evidence"].detail == ("Files: buyer-photo.jpg",)


def test_a_document_name_that_is_a_link_is_long_or_odd_is_not_shown_as_it_came():
    raw = {"messages": [{"posted_by": "BUYER", "time_posted": "2026-10-06T10:00:00Z",
                         "documents": [{"name": URL}, {"name": "x" * 400}, {"name": "ok.pdf"}, {"name": "  a\n\tb.pdf "},
                                       {"name": 7}, {"name": "d.pdf"}, {"name": "e.pdf"}, {"name": "f.pdf"}]}]}
    (event,) = build(raw, READ_AT)
    (line,) = event.detail
    assert URL not in line and "x" * 81 not in line and "\n" not in line and "\t" not in line
    assert line == "Files: a file, " + "x" * 79 + "…, ok.pdf, a b.pdf, d.pdf and 2 more"      # five names, then a count


def test_the_offer_history_names_the_actor_the_event_and_the_amount_by_integer_cents():
    events = [e for e in build(raw_dispute(), READ_AT) if e.kind == "offer"]
    assert events[0].detail == ("Amount: 10.00 USD",) and events[1].detail == ()


def test_money_movement_names_party_and_reason_from_closed_tables():
    (event,) = [e for e in build(raw_dispute(), READ_AT) if e.kind == "money"]
    assert event.detail == ("Affects: the seller", "Reason: dispute fee")


@pytest.mark.parametrize("value", ["abc", "10.999", "", None, 5, {"x": 1}])
def test_an_unreadable_amount_is_left_out_and_nothing_is_raised(value):
    raw = {"money_movements": [{"type": "CREDIT", "initiated_time": "2026-10-09T17:30:00Z",
                                "amount": {"currency_code": "USD", "value": value}}]}
    (event,) = build(raw, READ_AT)
    assert event.text == "A credit was initiated" and event.detail == ()


def test_an_enum_the_tables_do_not_know_is_never_echoed():
    hostile = "<script>alert(1)</script>"
    raw = {"evidences": [{"source": hostile, "evidence_type": hostile, "date": "2026-10-05T00:00:00Z"}],
           "messages": [{"posted_by": hostile, "time_posted": "2026-10-05T00:00:01Z"}],
           "offer": {"history": [{"actor": hostile, "event_type": hostile, "offer_time": "2026-10-05T00:00:02Z"}]},
           "adjudications": [{"type": hostile, "adjudication_time": "2026-10-05T00:00:03Z", "reason": hostile}],
           "supporting_info": [{"source": hostile, "provided_time": "2026-10-05T00:00:04Z"}],
           "money_movements": [{"type": hostile, "affected_party": hostile, "reason": hostile,
                                "initiated_time": "2026-10-05T00:00:05Z"}]}
    events = build(raw, READ_AT)
    assert [e.text for e in events] == ["PayPal listed evidence", "A message was posted", "A party recorded an offer event",
                                        "PayPal recorded an adjudication", "Supporting information was provided",
                                        "A money movement was initiated"]
    assert "script" not in repr(events)


@pytest.mark.parametrize("raw", [None, [], "x", 5, {}, {"evidences": "x", "messages": {"a": 1}, "offer": [], "adjudications": 3,
                                                       "supporting_info": None, "money_movements": "z", "extensions": []},
                                 {"evidences": [None, 5, "s", [], {"date": 7}], "messages": [None, {"time_posted": []}],
                                  "offer": {"history": [None, "x", {"offer_time": {}}]}, "extensions": {"buyer_contacted_time": 5}}])
def test_malformed_input_never_raises(raw):
    assert isinstance(build(raw, READ_AT), list)


def test_an_event_dated_after_the_read_is_flagged():
    raw = {"create_time": "2026-10-05T02:13:17Z", "update_time": "2026-10-21T00:00:00Z"}
    opened, updated = build(raw, READ_AT)
    assert opened.after_read is False and updated.after_read is True


def test_a_naive_read_time_is_taken_as_utc():
    raw = {"update_time": "2026-10-20T12:00:01Z"}
    assert build(raw, datetime(2026, 10, 20, 12, 0))[0].after_read is True


def test_the_result_does_not_depend_on_the_day_it_is_built(monkeypatch):
    first = build(raw_dispute(), READ_AT)
    import time
    monkeypatch.setattr(time, "time", lambda: 4102444800.0)         # 2100-01-01: build() reads no clock
    assert build(raw_dispute(), READ_AT) == first


def test_the_seam_for_verified_events_merges_by_instant_and_marks_its_origin():
    extra = verified_event("2026-10-07T00:00:00Z", "PayPal sent a notice that evidence was received", detail=("Notice 1",))
    events = build(raw_dispute(), READ_AT, verified=[extra])
    index = [e.origin for e in events].index("verified_event")
    assert events[index].kind == "verified" and events[index].text.startswith("PayPal sent a notice")
    assert events[index - 1].instant <= events[index].instant <= events[index + 1].instant
    assert [e for e in events if e.origin != "verified_event"] == build(raw_dispute(), READ_AT)


def test_a_verified_event_with_no_usable_time_goes_last_and_a_wrong_type_is_ignored():
    odd = verified_event("not a time", "A notice")
    events = build(raw_dispute(), READ_AT, verified=[odd, "x", None, 3])
    assert events[-1] == odd
    assert len([e for e in events if e.origin == "verified_event"]) == 1


def test_event_is_a_frozen_value():
    event = build({"create_time": "2026-10-05T02:13:17Z"}, READ_AT)[0]
    assert isinstance(event, Event)
    with pytest.raises(Exception):
        event.text = "changed"


def test_instant_reads_only_what_the_page_formatter_reads():
    assert timefmt.instant("2026-10-05T02:13:17.163Z") == datetime(2026, 10, 5, 2, 13, 17, 163000, tzinfo=timezone.utc)
    assert timefmt.instant("2026-10-05T23:00:00-06:00") == datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
    for value in (None, "", "2026-10-05T10:00:00", "2026-10-05", "garbage", True, 3.5, [], {}):
        assert timefmt.instant(value) is None


def test_the_module_has_no_model_no_network_and_no_clock_read():
    import inspect
    source = inspect.getsource(service)
    for banned in ("ollama", "httpx", "requests", "datetime.now", "utcnow", "time.time", "random", "float("):
        assert banned not in source, banned
