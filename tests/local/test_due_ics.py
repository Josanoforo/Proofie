"""The downloadable calendar file for PayPal's own response due date.

Expected results are written in the comments before each group. Offline only: fake PayPal clients, a temp database per
test (conftest.py); the module under test is pure text. Both web modes run the route tests."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from _dom import parse

from copilot.web import ics

import test_ux_rc41_dispute as dispute_tests
from test_ux_rc41_dispute import DID

pytestmark = [pytest.mark.usefixtures("web_mode")]

CDMX = timezone(timedelta(hours=-6))
DUE = datetime(2026, 10, 21, 3, 0, 0, tzinfo=CDMX)          # 09:00 UTC
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
URL = f"/disputes/{DID}/due.ics"


# ----- the pure module. Expected: the exact text below, CRLF after every line, nothing computed from the clock.
EXPECTED = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Proofie//Dispute due date//EN\r\n"
    "CALSCALE:GREGORIAN\r\n"
    "METHOD:PUBLISH\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:dispute-PP-R-DEMO-0000001-due\r\n"
    "DTSTAMP:20261018T120000Z\r\n"
    "DTSTART:20261021T090000Z\r\n"
    "DURATION:PT15M\r\n"
    "SUMMARY:Respond to PayPal dispute PP-R-DEMO-0000001\r\n"
    "DESCRIPTION:PayPal's response due date for this dispute\\, as read from PayPal. Open Proofie to review it. "
    "Proofie sends nothing on its own.\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "TRIGGER:-PT24H\r\n"
    "DESCRIPTION:The response to PayPal dispute PP-R-DEMO-0000001 is due in 24 hours.\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def _unfold(text):
    return text.replace("\r\n ", "")


def test_the_exact_calendar_text_for_a_fixed_due_instant_and_a_fixed_stamp():
    body = ics.due_calendar(DID, DUE, NOW)
    assert _unfold(body) == EXPECTED                                  # the text, before folding
    assert body.count("\r\n ") == 2          # the two long DESCRIPTION lines (139 and 80 octets) are folded once each


def test_dtstart_is_paypals_instant_in_utc_whatever_the_zone_it_arrives_in():
    for zone in (timezone.utc, CDMX, timezone(timedelta(hours=5, minutes=30))):
        body = ics.due_calendar(DID, DUE.astimezone(zone), NOW.astimezone(zone))
        assert "DTSTART:20261021T090000Z\r\n" in body and "DTSTAMP:20261018T120000Z\r\n" in body


def test_a_naive_instant_is_refused_not_guessed():
    for args in ((DUE.replace(tzinfo=None), NOW), (DUE, NOW.replace(tzinfo=None)), ("2026-10-21", NOW)):
        with pytest.raises(ValueError):
            ics.due_calendar(DID, *args)


def test_text_values_escape_backslash_semicolon_comma_and_line_breaks():
    assert ics.escape("a\\b;c,d\ne\r\nf") == "a\\\\b\\;c\\,d\\ne\\nf"
    body = ics.due_calendar("A;B,C\\D", DUE, NOW)
    assert "SUMMARY:Respond to PayPal dispute A\\;B\\,C\\\\D\r\n" in body


def test_every_line_is_crlf_and_at_most_75_octets_and_unfolding_restores_the_text():
    body = ics.due_calendar("X" * 90, DUE, NOW)
    assert body.endswith("\r\n") and "\n" not in body.replace("\r\n", "")
    lines = body.split("\r\n")[:-1]
    assert all(len(line.encode("utf-8")) <= 75 for line in lines)
    assert any(line.startswith(" ") for line in lines), "a long summary must have been folded"
    assert "SUMMARY:Respond to PayPal dispute " + "X" * 90 + "\r\n" in _unfold(body)


def test_folding_never_splits_a_multibyte_character_and_continuation_lines_hold_74_octets_at_most():
    text = "UID:" + "é" * 60                                            # 124 octets
    folded = ics.fold(text)
    for line in folded.split("\r\n"):
        raw = line.encode("utf-8")
        assert len(raw) <= 75
        raw.decode("utf-8")
    assert folded.replace("\r\n ", "") == text
    assert ics.fold("x" * 75) == "x" * 75 and ics.fold("x" * 76).count("\r\n") == 1


def test_the_uid_is_stable_and_has_no_host_like_part():
    assert ics.uid(DID) == ics.uid(DID) == "dispute-PP-R-DEMO-0000001-due"
    other = ics.due_calendar(DID, DUE + timedelta(days=1), NOW + timedelta(days=1))
    assert "UID:dispute-PP-R-DEMO-0000001-due\r\n" in other
    assert "@" not in ics.uid(DID) and not re.search(r"\.[a-z]{2,}", ics.uid(DID))


def test_there_is_one_event_one_alarm_24_hours_before_and_a_15_minute_length():
    body = ics.due_calendar(DID, DUE, NOW)
    assert body.count("BEGIN:VEVENT") == body.count("END:VEVENT") == 1
    assert body.count("BEGIN:VALARM") == body.count("END:VALARM") == 1
    assert "TRIGGER:-PT24H\r\n" in body and "DURATION:PT15M\r\n" in body


# ----- the route. Expected: one GET to PayPal (the same read the page makes), zero other calls, an attachment answer.
def _download(body=None):
    web, fake = dispute_tests._web(body)
    return web, fake, web.get(URL)


def test_the_route_answers_a_calendar_attachment_from_paypals_due_date_with_one_read_and_no_write():
    web, fake, response = _download()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["content-disposition"] == f'attachment; filename="proofie-due-{DID}.ics"'
    assert response.headers["cache-control"] == "no-store"
    assert "DTSTART:20261025T021317Z\r\n" in response.content.decode("utf-8")     # the fixture's PayPal instant, in UTC
    assert fake.calls == [("GET", f"/v1/customer/disputes/{DID}", "show_dispute")]


def test_the_uid_is_the_same_on_two_downloads_and_the_due_instant_follows_what_paypal_says():
    web, _ = dispute_tests._web()
    first, second = web.get(URL).text, web.get(URL).text
    uid_line = "UID:dispute-PP-R-DEMO-0000001-due\r\n"
    assert uid_line in first and uid_line in second
    moved, _ = dispute_tests._web(dispute_tests._body(seller_response_due_date="2026-11-02T18:00:00.000Z"))
    assert "DTSTART:20261102T180000Z\r\n" in moved.get(URL).text


def test_an_invalid_dispute_id_is_refused_before_any_paypal_call():
    web, fake = dispute_tests._web()
    response = web.get("/disputes/not-a-real-paypal-id/due.ics")
    assert response.status_code == 400 and "That dispute reference is not valid." in response.text
    assert fake.calls == []


@pytest.mark.parametrize("change", ["missing", "empty", "date_only", "unreadable"])
def test_no_due_date_gives_a_404_page_without_a_calendar_and_the_page_shows_no_link(change):
    body = dispute_tests._body()
    if change == "missing":
        body.pop("seller_response_due_date")
    else:
        body["seller_response_due_date"] = {"empty": "", "date_only": "2026-10-25", "unreadable": "soon"}[change]
    web, fake = dispute_tests._web(body)
    response = web.get(URL)
    assert response.status_code == 404
    assert "BEGIN:VCALENDAR" not in response.text and "VEVENT" not in response.text
    assert "text/html" in response.headers["content-type"]
    dom = parse(response.text)
    assert dom.find("h1").text() == "No due date to add"
    assert ("PayPal gave no response due date for this dispute, so there is nothing to add to a calendar. "
            "Nothing was changed.") in dom.text()
    assert [c[0] for c in fake.calls] == ["GET"] and len(fake.calls) == 1
    page = web.get(f"/disputes/{DID}")
    assert page.status_code == 200 and "due.ics" not in page.text and ics.LINK_TEXT not in page.text


def _due_in(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_the_dispute_page_links_the_calendar_beside_respond_by_and_adds_no_primary_button():
    web, _ = dispute_tests._web(dispute_tests._body(seller_response_due_date=_due_in(5)))
    text, dom = dispute_tests._page(web)
    links = [a for a in dom.find_all("a") if a.attrs.get("href") == URL]
    assert len(links) == 1 and links[0].text() == "Add the due date to a calendar (.ics file)"
    respond_by = [d for d in dom.find("dl", class_="facts").find_all("div") if d.find("dt").text() == "Respond by"][0]
    assert links[0] in list(respond_by.walk())

    def primaries(page_dom):
        return [(n.tag, n.text(), n.attrs.get("href")) for n in dispute_tests._primaries(page_dom)]
    no_due = dispute_tests._page(dispute_tests._web(dispute_tests._body(seller_response_due_date="soon"))[0])[1]
    assert primaries(dom) == primaries(no_due) and len(primaries(dom)) == 1


def test_no_link_is_shown_once_the_due_date_has_passed_but_the_page_still_says_when():
    web, _ = dispute_tests._web(dispute_tests._body(seller_response_due_date=_due_in(-3)))
    text, dom = dispute_tests._page(web)
    assert "due.ics" not in text and ics.LINK_TEXT not in text
    assert "The due date passed" in dom.find("dl", class_="facts").text()


def test_the_file_name_is_built_only_from_letters_digits_and_hyphens_matched_whole():
    assert ics.file_name(DID) == f"proofie-due-{DID}.ics"
    for bad in (DID + "\n", DID + "\r\nSet-Cookie: x=1", 'a"b', "a b", "", "PP/../x", "ñ"):
        with pytest.raises(ValueError):
            ics.file_name(bad)


def test_an_id_with_a_trailing_line_break_gets_no_download_and_no_paypal_call():
    web, fake = dispute_tests._web()
    response = web.get(f"/disputes/{DID}%0A/due.ics")
    assert response.status_code == 400 and "content-disposition" not in response.headers
    assert "That dispute reference is not valid." in response.text and fake.calls == []


def test_the_module_imports_only_the_standard_library():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ics))
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "datetime", "re"}
