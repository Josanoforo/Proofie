"""Tracking pages: redundant entry (the tracker form offers what the seller already typed on the package page of
the dispute), table captions and scopes, the heading level of the 'cannot be checked' list, field words.

Expected results, first: the suggestion is a display-only note with a GET link; nothing is saved or sent by it; the inputs are
filled only after 'Use these values'; a dispute id of the wrong shape or a dispute with no saved shipment fields shows nothing.
Offline (fake client, temp database), both web modes."""
import pytest
from _dom import parse
from _ux_lint import primaries

from copilot.db import repo
from copilot.features.fulfilment import store
from copilot.features.fulfilment.tracker_request import body_from_form, request_sha256
from test_routes import CAP, _web

pytestmark = [pytest.mark.usefixtures("web_mode")]

DID = "PP-R-DEMO-0000001"
URL = f"/fulfilment/{CAP}"


def _fields(db, number="1Z999AA10123456784", carrier="UPS"):
    fields = {}
    if number is not None:
        fields["tracking_number"] = {"value": number, "confidence": "read"}
    if carrier is not None:
        fields["carrier"] = {"value": carrier, "confidence": "read"}
    repo.set_manual_fields(db, DID, fields)


def _db():
    from copilot import config
    return config.db_path()


def _note(html):
    return parse(html).find("p", data_state="dispute-values")


def _control_values(html):
    dom = parse(html)
    tracking = dom.find("input", id="field-tracking_number").attrs.get("value", "")
    selected = [o.attrs["value"] for o in dom.find("select", id="field-carrier").find_all("option") if "selected" in o.attrs]
    other = dom.find("input", id="field-carrier_other_name").attrs.get("value", "")
    return tracking, selected, other


# ------------------------------------------------------------------------------------------ redundant entry
def test_the_form_offers_the_saved_shipment_values_as_a_note_with_a_link_and_prefills_nothing(monkeypatch):
    _fields(_db())
    web, fake = _web(monkeypatch)
    html = web.get(f"{URL}?from_dispute={DID}").text
    note = _note(html)
    assert note.text() == f"You saved tracking 1Z999AA10123456784 (UPS) on the package page for dispute {DID}. Use these values"
    link = note.find("a")
    assert link.text() == "Use these values" and link.attrs["href"] == f"{URL}?from_dispute={DID}&use=1"
    assert _control_values(html) == ("", ["" ], "")                                  # the empty choice, nothing typed
    dom = parse(html)
    form = dom.find("form", data_state="tracking-form")
    assert dom.find("p", data_state="dispute-values") in list(form.walk()), "the note sits inside the form"
    assert html.index('data-state="dispute-values"') < html.index('id="field-carrier"')
    assert [n.text() for n in primaries(html)] == ["Review what will be sent"]


def _freeze_clock(monkeypatch):
    """The tracking page asks PayPal for a window that ends at the current time; two page loads in one test must see one
    instant, or the recorded calls differ by a second now and then."""
    import datetime as real
    from copilot.features.fulfilment import routes

    class Frozen(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(2026, 10, 18, 12, 0, 0, tzinfo=real.timezone.utc if tz is None else tz)
    monkeypatch.setattr(routes, "datetime", Frozen)


def test_use_these_values_fills_the_inputs_and_writes_nothing_and_asks_paypal_nothing_new(monkeypatch):
    _freeze_clock(monkeypatch)
    db = _db()
    _fields(db)
    web, fake = _web(monkeypatch)
    plain_calls = (web.get(URL), list(fake.calls))[1]
    fake.calls.clear()
    manual_before, audit_before = repo.get_manual_fields(db, DID), repo.list_audit(db, DID)
    html = web.get(f"{URL}?from_dispute={DID}&use=1").text
    assert _control_values(html) == ("1Z999AA10123456784", ["UPS"], "")
    note = _note(html)
    assert note.text() == "Filled in from your shipment details. Check them, then press Review what will be sent."
    assert note.find("a") is None
    assert fake.calls == plain_calls and not fake.posts()
    assert repo.get_manual_fields(db, DID) == manual_before and repo.list_audit(db, DID) == audit_before
    assert store.list_files() == [] and store.get_file(CAP) is None


def test_a_carrier_outside_the_catalogue_fills_other_carrier_with_its_name(monkeypatch):
    _fields(_db(), carrier="Skydropx Express")
    web, _ = _web(monkeypatch)
    tracking, selected, other = _control_values(web.get(f"{URL}?from_dispute={DID}&use=1").text)
    assert selected == ["OTHER"] and other == "Skydropx Express" and tracking == "1Z999AA10123456784"


@pytest.mark.parametrize("number, carrier, sentence", [
    ("1Z999AA10123456784", None, f"You saved tracking 1Z999AA10123456784 on the package page for dispute {DID}."),
    (None, "FedEx", f"You saved the carrier FedEx on the package page for dispute {DID}.")])
def test_a_note_for_only_one_saved_value_names_just_that_value(monkeypatch, number, carrier, sentence):
    _fields(_db(), number=number, carrier=carrier)
    web, _ = _web(monkeypatch)
    assert _note(web.get(f"{URL}?from_dispute={DID}").text).text().startswith(sentence)


def test_nothing_shows_without_saved_values_or_with_a_dispute_id_of_the_wrong_shape(monkeypatch):
    """Values ARE stored under each malformed id, so only the id-shape check (validate_dispute_id) can keep them off the page."""
    from urllib.parse import unquote
    web, fake = _web(monkeypatch)
    assert _note(web.get(f"{URL}?from_dispute={DID}").text) is None                 # the dispute has no shipment fields
    _fields(_db())
    malformed = ("not-a-real-paypal-id", "..%2F..%2Fx", "PP-R-DEMO-0000001%22%3E%3Cb%3E", "PP-R-DEMO-0000001%0A",
                 "pp-r-demo-0000001", "PP-" + "A" * 60)
    for bad in malformed:
        repo.set_manual_fields(_db(), unquote(bad), {"tracking_number": {"value": "STORED-UNDER-BAD-ID", "confidence": "read"}})
    for bad in malformed:
        for query in (f"?from_dispute={bad}", f"?from_dispute={bad}&use=1"):
            html = web.get(URL + query).text
            assert _note(html) is None and "STORED-UNDER-BAD-ID" not in html, (bad, query)
            assert _control_values(html) == ("", [""], ""), (bad, query)
    assert _note(web.get(URL).text) is None
    assert _note(web.get(f"{URL}?from_dispute={DID}&use=1").text) is not None       # the valid id still works


def test_a_dispute_with_other_values_never_leaks_into_another_dispute(monkeypatch):
    repo.set_manual_fields(_db(), "PP-R-OTHER-0000002", {"tracking_number": {"value": "OTHER123456", "confidence": "read"}})
    web, _ = _web(monkeypatch)
    assert _note(web.get(f"{URL}?from_dispute={DID}").text) is None


def test_an_earlier_check_of_the_same_form_wins_over_the_suggestion(monkeypatch):
    web, fake = _web(monkeypatch)
    from test_ux_rc41_fulfilment import FORM, _preview, _token_of
    token = _token_of(_preview(web))
    _fields(_db(), number="OTHER-TRACK-9", carrier="DHL")
    html = web.get(f"{URL}?from={token}&from_dispute={DID}&use=1").text
    assert _note(html) is None and _control_values(html)[0] == FORM["tracking_number"]


def test_the_suggestion_changes_neither_the_stored_body_nor_the_request_hash(monkeypatch):
    _fields(_db())
    web, fake = _web(monkeypatch)
    from test_ux_rc41_fulfilment import FORM, _preview, _token_of
    from test_ux_rc41_fulfilment import _get
    _get(web, f"{URL}?from_dispute={DID}&use=1")
    sha = store.get_check(CAP, _token_of(_preview(web)))["request_sha256"]
    assert sha == request_sha256(body_from_form(CAP, FORM["tracking_number"], "FEDEX", "SHIPPED"))


# ------------------------------------------------------------------------------ captions, scopes, headings
def _file_page(web, not_evaluable, readback=True):
    body = body_from_form(CAP, "1Z999AA10123456784", "UPS", "SHIPPED")
    sha = request_sha256(body)
    checks = {"checks": [{"id": "tracking", "label": "Tracking number", "state": "verified", "message": "Matches.",
                          "paypal_sourced": True}],
              "not_evaluable": not_evaluable, "address_comparison": {"result": "not_declared", "basis": "x"},
              "limitations": [], "rules": {}, "protection_eligibility": None}
    back = {"tracking_number": "1Z999AA10123456784", "status": "SHIPPED", "carrier": "UPS"} if readback else None
    saved = store.save_file(CAP, body, sha, sha, back, checks, "registered" if readback else "saved_without_registration", None)
    return web.get(f"{URL}/file/{saved['id']}").text


def _levels(dom):
    return [int(n.tag[1]) for n in dom.walk() if n.tag in ("h1", "h2", "h3", "h4")]


def test_the_file_page_tables_have_hidden_captions_and_scoped_headers(monkeypatch):
    web, _ = _web(monkeypatch)
    dom = parse(_file_page(web, [{"id": "case_request", "reason": "No dispute exists yet."}]))
    expected = {"readback": "Read-back of the tracking", "checks": "Checks at the sale"}
    for state, caption in expected.items():
        table = dom.find("table", data_state=state)
        cap = table.find("caption")
        assert cap.text() == caption and "visually-hidden" in cap.attrs["class"].split()
        assert all(th.attrs.get("scope") in ("col", "row") for th in table.find_all("th")), state


def test_the_cannot_be_checked_heading_is_a_level_two_and_no_level_is_skipped(monkeypatch):
    web, _ = _web(monkeypatch)
    dom = parse(_file_page(web, [{"id": "case_request", "reason": "No dispute exists yet."}]))
    heading = [n for n in dom.walk() if n.tag in ("h2", "h3") and n.text() == "Cannot be checked until a dispute exists"]
    assert [n.tag for n in heading] == ["h2"]
    levels = _levels(dom)
    assert levels[0] == 1 and all(b - a <= 1 for a, b in zip(levels, levels[1:])), levels


def test_not_evaluable_names_come_from_the_closed_words_never_from_a_raw_key(monkeypatch):
    web, _ = _web(monkeypatch)
    dom = parse(_file_page(web, [{"id": "case_request", "reason": "No dispute exists yet."},
                                 {"id": "tracking_number", "reason": "Not asked."},
                                 {"id": "some_new_key", "reason": "Unknown."}]))
    items = [li.text() for li in dom.find("ul", data_state="not-evaluable").find_all("li")]
    assert items == ["PayPal's evidence request: No dispute exists yet.", "Tracking number: Not asked.",
                     "A field Proofie does not name: Unknown."]


def test_the_form_page_transaction_table_has_a_hidden_caption(monkeypatch):
    web, _ = _web(monkeypatch)
    table = parse(web.get(URL).text).find("table", data_state="transaction")
    assert table.find("caption").text() == "What PayPal shows for this sale"
    assert "visually-hidden" in table.find("caption").attrs["class"].split()


# ------------------------------------------------------------------------------------------------ sending cannot be undone: tracking
# Expected: the review page of the tracking says, before the send button and only when registration is on, that adding the
# tracking cannot be undone from Proofie and that Proofie refuses to add it twice; true: a repeat register makes one POST in all.
def _check_page(web):
    from test_ux_rc41_fulfilment import _get, _prepared
    token, sha = _prepared(web)
    return _get(web, f"{URL}/check/{token}").text, token, sha


def test_the_tracking_review_page_says_adding_cannot_be_undone_before_the_send_button(monkeypatch):
    web, fake = _web(monkeypatch)
    html, token, sha = _check_page(web)
    form = parse(html).find("form", data_state="confirm-form")
    note = form.find("p", data_state="cannot-undo")
    assert note.text() == "Adding the tracking cannot be undone from Proofie. Proofie refuses to add the same tracking twice."
    walk = list(form.walk())
    assert walk.index(note) < walk.index(form.find("button"))


def test_the_sentence_is_absent_when_registration_is_off_because_nothing_is_sent(monkeypatch):
    web, fake = _web(monkeypatch, register=None)
    html, token, sha = _check_page(web)
    assert parse(html).find("p", data_state="cannot-undo") is None and not fake.posts()


def test_the_tracking_sentence_is_true_a_repeat_register_posts_nothing_more(monkeypatch):
    from test_ux_rc41_fulfilment import _register
    web, fake = _web(monkeypatch)
    html, token, sha = _check_page(web)
    _register(web, token, sha)
    assert len(fake.posts()) == 1
    _register(web, token, sha)
    assert len(fake.posts()) == 1


# ------------------------------------------------------------------------------------------------ what happens next on the tracking result
# Expected: the result page has a 'What happens next' section (an h2, one sentence) for every file status.
NEXT = {"registered": "The tracking is registered. This does not answer the dispute: open the dispute to respond.",
        "registered_unconfirmed": "Press Check PayPal again to see whether PayPal now lists it. Proofie will not send it again on its own.",
        "unknown": "Press Check PayPal again to see whether PayPal now lists it. Proofie will not send it again on its own.",
        "failed": "Nothing was registered. Correct the details and prepare the tracking again. Proofie sends nothing until you approve it.",
        "saved_without_registration": "Nothing was sent to PayPal. The file stays in Proofie; you can see all tracking files from the list."}


@pytest.mark.parametrize("status", list(NEXT))
def test_the_result_page_has_a_what_happens_next_section_for_every_status(monkeypatch, status):
    from test_ux_rc41_fulfilment import _seed
    web, fake = _web(monkeypatch)
    saved, _sha = _seed(status)
    dom = parse(web.get(f"{URL}/file/{saved['id']}").text)
    section = dom.find("section", data_state="what-next")
    assert section.find("h2").text() == "What happens next" and section.find("p").text() == NEXT[status]
    assert not section.find_all("form") and not section.find_all("button")
    levels = [int(n.tag[1]) for n in dom.walk() if n.tag in ("h1", "h2", "h3")]
    assert all(b - a <= 1 for a, b in zip(levels, levels[1:])), levels


# ------------------------------------------------------------------------------------------ the dispute link
# Expected: when the seller came from a dispute (valid id), the id travels form -> review -> result and the registered result
# links that dispute; with no id, or an id of the wrong shape, the sentence has no link; a repeat check keeps the link.
def _walk_to_result(web, fake, from_dispute):
    from test_ux_rc41_fulfilment import FORM, _get, _register, _token_of
    data = {**FORM, **({"from_dispute": from_dispute} if from_dispute is not None else {})}
    response = web.post(f"{URL}/preview", data=data, follow_redirects=False)
    from _hosted import reset_csrf, set_test_csrf
    from test_routes import TOKEN
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    token = _token_of(response)
    sha = store.get_check(CAP, token)["request_sha256"]
    return _register(web, token, sha)


def test_the_registered_result_links_the_dispute_the_seller_came_from(monkeypatch):
    web, fake = _web(monkeypatch)
    registered = _walk_to_result(web, fake, DID)
    assert registered.headers["location"].endswith(f"?from_dispute={DID}")
    from test_ux_rc41_fulfilment import _get
    dom = parse(_get(web, registered.headers["location"]).text)
    anchor = dom.find("section", data_state="what-next").find("a")
    assert anchor.text() == "open the dispute" and anchor.attrs["href"] == f"/disputes/{DID}"


@pytest.mark.parametrize("sent", [None, "", "not-a-real-paypal-id", DID + "%0A", "../x"])
def test_without_a_valid_dispute_id_the_sentence_has_no_link_and_the_address_carries_nothing(monkeypatch, sent):
    web, fake = _web(monkeypatch)
    registered = _walk_to_result(web, fake, sent)
    assert "from_dispute" not in registered.headers["location"]
    from test_ux_rc41_fulfilment import _get
    dom = parse(_get(web, registered.headers["location"]).text)
    assert dom.find("section", data_state="what-next").find("a") is None
    forged = parse(_get(web, registered.headers["location"] + "?from_dispute=not-valid").text)
    assert forged.find("section", data_state="what-next").find("a") is None


# ------------------------------------------------------------------------------------------ field messages use the macro
# Expected: the shipment-status and country messages render through the shared field-message macro: the same paragraph with the
# id the select points at, the hidden-from-readers glyph and the words.
def _bad_post(web, **change):
    from test_ux_rc41_fulfilment import FORM
    return web.post(f"{URL}/preview", data={**FORM, **change})


def test_the_status_message_shows_the_glyph_and_the_words_through_the_macro(monkeypatch):
    web, _ = _web(monkeypatch)
    response = _bad_post(web, status="")
    assert response.status_code == 400
    dom = parse(response.text)
    message = dom.find("p", id="error-status")
    assert message is not None and "field-error" in message.attrs["class"].split()
    glyph = message.find("span")
    assert glyph.attrs["aria-hidden"] == "true" and glyph.text() == "\u2715"
    assert message.text().startswith("\u2715 ") and len(message.text()) > 3
    select = dom.find("select", id="field-status")
    assert "error-status" in select.attrs["aria-describedby"].split()


def test_the_country_message_shows_the_glyph_and_the_words_through_the_macro(monkeypatch):
    web, _ = _web(monkeypatch)
    response = _bad_post(web, declared_country_code="Narnia", declared_city="X")
    dom = parse(response.text)
    message = dom.find("p", id="error-declared_country_code")
    assert message is not None and message.find("span").attrs["aria-hidden"] == "true"
    assert message.text().startswith("\u2715 ") and len(message.text()) > 3
