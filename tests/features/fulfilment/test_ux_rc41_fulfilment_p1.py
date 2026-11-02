"""Fulfilment flow: shell rollout (lint, one primary, breadcrumbs) and the smaller items. Fake client and
temp database only."""
import re
from pathlib import Path

import httpx
import pytest

from _dom import parse
from _hosted import reset_csrf, set_test_csrf
from _ux_lint import assert_single_primary, back_links, breadcrumb_items, primaries, template_lint
from copilot.features.fulfilment import store
from copilot.features.fulfilment.tracker_request import body_from_form, request_sha256
from test_routes import CAP, FakeClient, TOKEN, _web
from test_ux_rc41_fulfilment import FORM, _get, _prepared, _register, _seed


def _post(web, path, data, **kw):
    """POST, then put the fixed test csrf token back (an error page rotates the cookie)."""
    response = web.post(path, data=data, **kw)
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    return response


pytestmark = [pytest.mark.usefixtures("web_mode")]

ROOT = Path(__file__).resolve().parents[3]
OWNED_TEMPLATES = sorted((ROOT / "copilot/web/templates/features/fulfilment").glob("*.html")) + \
    sorted((ROOT / "copilot/web/templates/features/delivery_facts").glob("*.html"))


def test_owned_templates_pass_the_shell_lint_and_have_no_maxlength():
    assert template_lint(OWNED_TEMPLATES) == []
    assert not [p.name for p in OWNED_TEMPLATES if "maxlength" in p.read_text(encoding="utf-8")]


def _file_of(status):
    body = body_from_form(CAP, FORM["tracking_number"], "FEDEX", "SHIPPED")
    sha = request_sha256(body)
    checks = {"checks": [], "not_evaluable": [], "limitations": [],
              "address_comparison": {"result": "not_declared", "basis": "x"}}
    return store.save_file(CAP, body, sha, sha, None, checks, status, None)


def test_every_page_has_one_primary_no_back_link_and_a_breadcrumb(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch, FakeClient(list_after_post=False))
    pages = {"list-empty": _get(web, "/fulfilment").text, "form": _get(web, f"/fulfilment/{CAP}").text,
             "form-error": _post(web, f"/fulfilment/{CAP}/preview", {**FORM, "tracking_number": ""}).text}
    token, sha = _prepared(web)
    pages["check"] = _get(web, f"/fulfilment/{CAP}/check/{token}").text
    r = _register(web, token, sha)                                    # accepted, not listed
    assert store.get_file(CAP)["status"] == "registered_unconfirmed"
    pages["result-unconfirmed"] = _get(web, r.headers["location"]).text
    pages["list-files"] = _get(web, "/fulfilment").text
    pages["form-existing"] = _get(web, f"/fulfilment/{CAP}").text
    for status in ("unknown", "failed", "saved_without_registration"):
        saved = _file_of(status)
        pages["result-" + status] = _get(web, f"/fulfilment/{CAP}/file/{saved['id']}").text
    for name, html in pages.items():
        assert back_links(html) == [], name
        assert_single_primary(html)
        assert breadcrumb_items(html), name


def test_registered_result_has_the_success_panel_before_the_checks_and_one_primary(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    token, sha = _prepared(web)
    r = _register(web, token, sha)
    page = _get(web, r.headers["location"]).text
    dom = parse(page)
    assert dom.find("h1").text() == "Tracking registered in PayPal"
    assert_single_primary(page)
    assert [n.text() for n in primaries(page)] == ["View shipment facts"]
    assert page.index('data-state="file-status"') < page.index('data-state="checks"')
    assert "tag--bad" not in "".join(
        str(n.attrs.get("class", "")) for row in dom.find_all("tr") if "for information only" in row.text()
        for n in row.walk())
    for hook in ("file-status", "readback", "checks", "file-sha256"):
        assert dom.find(None, data_state=hook) is not None, hook


def test_an_empty_form_reports_carrier_tracking_and_status_in_one_response(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    r = _post(web, f"/fulfilment/{CAP}/preview", {"csrf_token": TOKEN})
    assert r.status_code == 400
    summary = parse(r.text).find("section", class_="error-summary")
    assert [a.attrs["href"] for a in summary.find_all("a")] == ["#field-carrier", "#field-tracking_number",
                                                                "#field-status"]
    assert "Choose the shipping company" in summary.text() and "Choose the shipment status" in summary.text()
    assert not fake.calls


def test_the_status_select_has_no_preselected_option_and_is_required_on_the_server(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    select = parse(_get(web, f"/fulfilment/{CAP}").text).find("select", id="field-status")
    options = select.find_all("option")
    assert options[0].attrs["value"] == "" and options[0].text() == "Choose the shipment status"
    assert not [o for o in options if "selected" in o.attrs and o.attrs["value"]]
    r = _post(web, f"/fulfilment/{CAP}/preview", {k: v for k, v in FORM.items() if k != "status"})
    assert r.status_code == 400 and "Choose the shipment status" in r.text


def test_a_delivered_status_shows_a_non_blocking_warning(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    token, _sha = _prepared(web, {**FORM, "status": "DELIVERED"})
    page = _get(web, f"/fulfilment/{CAP}/check/{token}").text
    assert "You are telling PayPal the parcel was delivered. Proofie cannot check this; PayPal shows what you send." in page
    assert 'data-state="confirm-form"' in page


def test_the_check_page_shows_what_was_sent_as_and_its_sha_matches_the_register_body(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    token, sha = _prepared(web, {**FORM, "tracking_number": " 7712 3456 7890 "})
    page = _get(web, f"/fulfilment/{CAP}/check/{token}").text
    assert "Sent as:" in page and "771234567890" in page
    assert re.search(r'data-state="request-sha256">([0-9a-f]{64})<', page).group(1) == sha
    assert sha == request_sha256(store.get_check(CAP, token)["body"])


def test_a_number_paypal_already_lists_is_refused_at_the_preview_without_a_post(web_mode, monkeypatch):
    fake = FakeClient()
    fake.trackers.append({"transaction_id": CAP, "tracking_number": "771234567890", "status": "SHIPPED",
                          "carrier": "FEDEX", "links": []})
    web, _ = _web(monkeypatch, fake)
    r = web.post(f"/fulfilment/{CAP}/preview", data=FORM, follow_redirects=False)
    assert r.status_code == 400
    assert "PayPal already lists tracking 771234567890 for this sale. Nothing was sent." in r.text
    assert parse(r.text).find("input", id="field-tracking_number").attrs["value"] == "771234567890"
    assert not fake.posts()


def test_a_failed_tracker_read_does_not_block_and_the_check_page_says_so(web_mode, monkeypatch):
    class Down(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "fulfilment_tracker_get":
                raise httpx.ConnectError("down")
            return super().call(method, path, op, **kw)
    web, _fake = _web(monkeypatch, Down())
    token, _sha = _prepared(web)
    page = _get(web, f"/fulfilment/{CAP}/check/{token}").text
    assert "Proofie could not check PayPal&#39;s trackers at" in page or "Proofie could not check PayPal's trackers at" in page
    assert 'data-state="confirm-form"' in page


def test_the_error_page_does_not_claim_paypal_returned_nothing_and_keeps_the_file_notice(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    _seed("registered")
    r = _post(web, f"/fulfilment/{CAP}/preview", {**FORM, "tracking_number": "Fedex"})
    assert r.status_code == 400
    assert "Not returned by PayPal" not in r.text
    assert "This page did not ask PayPal again. Your entries are kept." in r.text
    assert 'data-state="existing-file"' in r.text and 'data-state="transaction"' not in r.text


def test_change_the_details_refills_the_form_from_the_stored_check(web_mode, monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit
    # The check token is random: force one that contains "AB" (the typed tracking starts with it) so the test cannot depend on luck.
    import itertools
    counter = itertools.count(1)
    monkeypatch.setattr(store, "secrets", SimpleNamespace(token_urlsafe=lambda size=18: f"ABcdABcdABcd{next(counter):08d}"))
    web, _fake = _web(monkeypatch)
    token, _sha = _prepared(web, {**FORM, "carrier": "OTHER", "carrier_other_name": "Transportes Locales SA",
                                  "tracking_number": "AB 123 456"})
    assert token.startswith("AB")
    check_page = _get(web, f"/fulfilment/{CAP}/check/{token}").text
    link = [a.attrs["href"] for a in parse(check_page).find_all("a") if a.text().startswith("Change")][0]
    parts = urlsplit(link)
    # What it means: the link carries only the opaque token, never what the seller typed.
    assert parts.path == f"/fulfilment/{CAP}" and parse_qs(parts.query) == {"from": [token]}
    dom = parse(_get(web, link.split("#")[0]).text)
    assert dom.find("input", id="field-tracking_number").attrs["value"] == "AB 123 456"
    assert dom.find("input", id="field-carrier_other_name").attrs["value"] == "Transportes Locales SA"
    assert dom.find("option", value="OTHER").attrs.get("selected") is not None
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    again = web.post(f"/fulfilment/{CAP}/preview", data={**FORM, "from_token": token}, follow_redirects=False)
    assert again.status_code == 303
    assert _get(web, f"/fulfilment/{CAP}/check/{token}").status_code == 404      # the old token is spent


def test_country_names_map_to_a_code_and_a_long_tracking_number_keeps_its_value(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    for typed, expected in (("Mexico", "MX"), ("MX", "MX"), ("United States", "US")):
        token, _ = _prepared(web, {**FORM, "declared_country_code": typed, "declared_city": "X"})
        assert store.get_check(CAP, token)["declared"]["country_code"] == expected
    token, _ = _prepared(web, {**FORM, "declared_country_code": "OTHER", "declared_country_other": "ca"})
    assert store.get_check(CAP, token)["declared"]["country_code"] == "CA"
    bad = _post(web, f"/fulfilment/{CAP}/preview", {**FORM, "declared_country_code": "Narnia"})
    assert bad.status_code == 400 and parse(bad.text).find("p", id="error-declared_country_code") is not None
    long_number = "A1" * 33
    r = _post(web, f"/fulfilment/{CAP}/preview", {**FORM, "tracking_number": long_number})
    assert r.status_code == 400 and "Tracking number must be 64 characters or fewer. Check the receipt." in r.text
    assert parse(r.text).find("input", id="field-tracking_number").attrs["value"] == long_number


def test_list_page_markup_stacks_on_small_screens_and_links_to_the_latest_file(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    saved = _seed("registered")[0]
    html = _get(web, "/fulfilment").text
    dom = parse(html)
    table = dom.find("table", class_="fulfilment-files")
    assert "stack" in table.attrs["class"].split()
    assert all("data-label" in td.attrs for td in table.find_all("td"))
    assert "Fields marked Required" not in html and "File sha256" not in html
    assert table.find("caption").text() == "Your tracking files, newest first"
    link = table.find_all("a")[0]
    assert link.attrs["href"] == f"/fulfilment/{CAP}/file/{saved['id']}" and link.text().startswith("View tracking file:")
    assert [l.text() for l in dom.find_all("label") if l.attrs.get("for") == "field-capture_id"][0].startswith("Transaction ID")
    assert [n.text() for n in primaries(html)] == ["Open the sale"]


def test_a_malformed_transaction_id_gets_the_exact_message(web_mode, monkeypatch):
    web, fake = _web(monkeypatch)
    r = web.get("/fulfilment/open?capture_id=abc")
    assert r.status_code == 400
    assert "Enter the transaction ID: 17 letters and numbers, from the payment details in PayPal" in r.text
    assert not fake.calls


def test_ups_advisory_never_blocks(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    token, _ = _prepared(web, {**FORM, "carrier": "UPS"})
    page = _get(web, f"/fulfilment/{CAP}/check/{token}").text
    assert "UPS numbers usually start with 1Z and have 18 characters." in page
    assert [n.text() for n in primaries(page)] == ["Add tracking in PayPal"]


def test_payment_not_found_and_read_failure_are_worded_apart(web_mode, monkeypatch):
    class Empty(FakeClient):
        def call(self, method, path, op, **kw):
            if path.startswith("/v1/reporting/transactions"):
                self.calls.append((method, path, op))
                return {"transaction_details": []}
            return super().call(method, path, op, **kw)

    class Broken(FakeClient):
        def call(self, method, path, op, **kw):
            if path.startswith("/v1/reporting/transactions"):
                raise httpx.ConnectError("down")
            return super().call(method, path, op, **kw)
    web, _ = _web(monkeypatch, Empty())
    page = _get(web, f"/fulfilment/{CAP}").text
    assert "PayPal found no payment with transaction ID" in page and "Check the ID in your PayPal activity." in page
    web, _ = _web(monkeypatch, Broken())
    page = _get(web, f"/fulfilment/{CAP}").text
    assert "Proofie could not read this payment from PayPal. Try again in a minute." in page
    assert "PayPal found no payment" not in page


def test_delivery_facts_page_title_comparison_and_display_words(web_mode, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_DELIVERY_FACTS", "true")
    web, _fake = _web(monkeypatch)
    token, sha = _prepared(web)
    _register(web, token, sha)
    html = _get(web, f"/delivery-facts/{CAP}").text
    dom = parse(html)
    assert dom.find("h1").text() == f"Shipment facts for sale {CAP}"
    assert "Last updated" not in html
    assert "Your entry compared with PayPal" in html and "Tracking number: same in both" in html
    assert "Carrier: same in both" in html and "Status: same in both" in html
    assert "FEDEX; SHIPPED" not in html and "Stored tracker readback" not in html
    assert 'lang="es"' in html and "Detalles del envío." in html
    assert back_links(html) == []


def test_us_spelling_in_rendered_pages(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    token, _sha = _prepared(web)
    for html in (_get(web, f"/fulfilment/{CAP}").text, _get(web, f"/fulfilment/{CAP}/check/{token}").text,
                 _get(web, "/fulfilment").text):
        assert "programme" not in html and "Cancelled" not in html
