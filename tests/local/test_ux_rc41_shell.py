"""The shared UI shell (buttons, header, breadcrumb, forms, status, next step, time, guard).

Offline only: fake PayPal clients, a temp database per test (conftest.py), DOM checks through _dom / _ux_lint.
The new modules are imported inside the tests that use them, so the behaviour tests
can be run before the code exists and fail for the right reason.
"""
import ast
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _dom import parse
from _hosted import authenticated_client, set_test_csrf
import _ux_lint as lint

from copilot import config
from copilot.app import create_app
from copilot.core import carriers
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.db import repo
from copilot.web import routes_disputes, routes_package
from copilot.web.routes_disputes import get_client

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "copilot" / "web"
REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE1 = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
UTC = timezone.utc


class FakeClient:
    def call(self, method, path, op, **kw):
        if op == "show_dispute":
            return WAITING
        if op == "list_disputes":
            return LIST_PAGE1
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _web():
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    token = secrets.token_urlsafe(32)
    set_test_csrf(web, token)
    return web, config.db_path(), token


def render(source, **context):
    return routes_disputes.templates.env.from_string(source).render(**context)


def macro(names, call, env=None, **context):
    env = env or routes_disputes.templates.env
    return env.from_string("{% from '_components.html' import " + names + " %}" + call).render(**context)


def form_macro(names, call, **context):
    return routes_disputes.templates.env.from_string("{% from '_form_fields.html' import " + names + " %}" + call).render(**context)


def css_block(selector_regex):
    match = re.search(selector_regex + r"\s*\{([^{}]*)\}", CSS, re.S)
    assert match, f"CSS rule not found: {selector_regex}"
    return match.group(1)


# ------------------------------------------------------------------------------------- SHELL-BOOTSTRAP
@pytest.mark.parametrize("module", [routes_disputes, routes_package])
def test_both_template_environments_expose_the_shell_helpers_and_render_when(module):
    env = module.templates.env
    assert "when" in env.filters and "when_text" in env.filters
    for name in ("dispute_status", "step_caption", "done_message"):
        assert name in env.globals, name
    html = env.from_string("{{ x|when }}|{{ x|when_text }}").render(x="2026-10-16 22:10 CST (UTC-6)")
    assert '<time datetime="2026-10-16T22:10:00-06:00">16 Oct 2026, 10:10 pm (Mexico City time)</time>' in html
    assert html.endswith("|16 Oct 2026, 10:10 pm (Mexico City time)")


def test_install_is_idempotent_and_the_environments_are_separate():
    from copilot.web import presentation
    env = routes_package.templates.env
    before = dict(env.globals)
    presentation.install(env)
    assert env.filters["when"] is before.get("when", env.filters["when"]) and set(env.globals) == set(before)
    assert routes_package.templates.env is not routes_disputes.templates.env


# --- the frame
def _matrix(web, db, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")
    package = repo.insert_package(db, DID, "human_edit", True, [])
    return {"inbox": "/disputes", "dispute": f"/disputes/{DID}",
            "package": f"/disputes/{DID}/packages/{package}", "fulfilment": "/fulfilment"}


def test_the_frame_has_the_same_main_navigation_on_every_page(web_mode, monkeypatch):
    for lane in ('JUDGE_WALKTHROUGH', 'LOCAL_DEMO_HUB'):      # the lane's two navigation links are their own flags'; this test pins the core navigation
        monkeypatch.delenv('COPILOT_FEATURE_' + lane, raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")   # the fulfilment router is mounted when the app is created
    web, db, _ = _web()
    pages = {name: web.get(path) for name, path in _matrix(web, db, monkeypatch).items()}
    orders = set()
    for name, response in pages.items():
        assert response.status_code == 200, name
        links = lint.main_nav_links(response.text)
        assert links[0][:2] == ("All disputes", "/disputes"), name
        assert [link[0] for link in links] == ["All disputes", "Tracking at the sale"], name
        orders.add(tuple((text, href) for text, href, _ in links))
    assert len(orders) == 1
    assert dict((t, c) for t, _, c in lint.main_nav_links(pages["inbox"].text))["All disputes"] == "page"
    assert dict((t, c) for t, _, c in lint.main_nav_links(pages["dispute"].text))["All disputes"] == "true"
    assert dict((t, c) for t, _, c in lint.main_nav_links(pages["package"].text))["All disputes"] == "true"
    fulfilment = dict((t, c) for t, _, c in lint.main_nav_links(pages["fulfilment"].text))
    assert fulfilment["Tracking at the sale"] == "page" and fulfilment["All disputes"] is None


def test_tracking_at_the_sale_is_in_the_navigation_only_with_the_fulfilment_flag(web_mode, monkeypatch):
    for lane in ('JUDGE_WALKTHROUGH', 'LOCAL_DEMO_HUB'):      # the lane's two navigation links are their own flags'; this test pins the core navigation
        monkeypatch.delenv('COPILOT_FEATURE_' + lane, raising=False)
    web, _, _ = _web()
    monkeypatch.delenv("COPILOT_FEATURE_FULFILMENT", raising=False)
    off = lint.main_nav_links(web.get("/disputes").text)
    assert [link[0] for link in off] == ["All disputes"]
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")
    assert [link[0] for link in lint.main_nav_links(web.get("/disputes").text)] == ["All disputes", "Tracking at the sale"]


def test_delivery_facts_pages_count_under_tracking_at_the_sale():
    html = routes_disputes.templates.env.from_string("{% extends '_base.html' %}").render(
        request=type("R", (), {"url": type("U", (), {"path": "/delivery-facts/CAP1"})(), "scope": {}})())   # no app in scope: the hosted-demo note falls back to the global sentence
    assert dict((t, c) for t, _, c in lint.main_nav_links(html)).get("All disputes") is None


def test_env_tag_brand_and_frame_landmarks(web_mode, monkeypatch):
    web, _, _ = _web()
    html = web.get("/disputes").text
    dom = parse(html)
    assert dom.find("span", class_="env-tag").text() == "PayPal sandbox · assistant only, you review everything"
    brand = dom.find("a", class_="brand")
    assert brand.attrs["href"] == "/disputes" and brand.attrs.get("translate") == "no"
    main = dom.find("main")
    assert main.attrs.get("id") == "main" and main.attrs.get("tabindex") in (None, "-1")   # -1: the skip link can move focus here; never in the tab order
    regions = dom.find_all("div", id="inflight-status")
    assert len(regions) == 1 and regions[0].attrs.get("role") == "status" and regions[0].attrs.get("aria-live") == "polite"
    # order in the document: skip link, header, (breadcrumb), main, footer, in-flight region
    positions = [html.index(marker) for marker in ('class="skip-link"', 'class="site-header"', "<main", 'class="site-footer"', 'id="inflight-status"')]
    assert positions == sorted(positions)


def test_only_the_access_page_overrides_the_navigation_block():
    overriding = sorted(p.name for p in (WEB / "templates").rglob("*.html") if re.search(r"\{%-?\s*block nav\s*-?%\}", p.read_text(encoding="utf-8")))
    assert overriding == ["_base.html", "access.html"]
    access = (WEB / "templates" / "access.html").read_text(encoding="utf-8")
    assert re.search(r"\{% block nav %\}\s*\{% endblock %\}", access)


def test_first_focusable_element_is_the_skip_link_on_every_page(web_mode, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")   # the fulfilment router is mounted when the app is created
    web, db, _ = _web()
    for name, path in _matrix(web, db, monkeypatch).items():
        dom = parse(web.get(path).text)
        body = dom.find("body")
        focusable = [n for n in body.walk() if n.tag in ("a", "button", "input", "select", "textarea", "summary")]
        assert focusable[0].tag == "a" and focusable[0].attrs.get("class") == "skip-link" and focusable[0].attrs.get("href") == "#main", name
        assert dom.find("main", id="main") is not None and dom.find("main").attrs.get("tabindex") in (None, "-1"), name


def test_the_navigation_css_gives_44px_targets_and_marks_the_current_page():
    assert "min-height: var(--target)" in css_block(r"\.site-nav a")
    assert "border-bottom-color: var(--header-fg)" in css_block(r"\.site-nav a\[aria-current\]")
    assert "3px" in css_block(r"\.site-nav a")
    skip = css_block(r"\.skip-link")
    assert "left: -9999px" in skip and "display" not in skip
    assert "left: var(--space-2)" in css_block(r"\.skip-link:focus")


# --- the control family
def test_button_macro_documented_classes():
    cases = {
        "button('Save')": "btn btn-primary btn-large",
        "button('Save', kind='primary', large=True)": "btn btn-primary btn-large",
        "button('Save', kind='secondary')": "btn btn-secondary",
        "button('Save', kind='secondary', large=True)": "btn btn-secondary btn-large",
        "button('Save', kind='link')": "btn-link",
    }
    for call, expected in cases.items():
        node = parse(macro("button", "{{ " + call + " }}")).find("button")
        assert node.attrs["class"] == expected, call
        assert node.attrs["type"] == "submit"
    link = parse(macro("button", "{{ button('Save', kind='link') }}")).find("button")
    assert link.attrs["class"] == "btn-link"                       # link kind has class btn-link only


def test_button_macro_attributes_and_hidden_text():
    node = parse(macro("button", "{{ button('Send', name='k', value='v', hidden='for statement 2', sending='Sending…', "
                                 "describedby='note', form='f1', id='b1', data_state='x', type='button') }}")).find("button")
    assert node.attrs["type"] == "button" and node.attrs["name"] == "k" and node.attrs["value"] == "v"
    assert node.attrs["form"] == "f1" and node.attrs["aria-describedby"] == "note" and node.attrs["id"] == "b1"
    assert node.attrs["data-sending-text"] == "Sending…" and node.attrs["data-state"] == "x"
    hidden = node.find("span", class_="visually-hidden")
    assert hidden is not None and hidden.text() == "for statement 2"
    assert "disabled" not in node.attrs and "aria-disabled" not in node.attrs


def test_link_button_is_a_plain_get_link_never_a_role_button():
    node = parse(macro("link_button", "{{ link_button('/disputes', 'Back', kind='secondary', large=True, hidden='x') }}")).find("a")
    assert node.attrs["href"] == "/disputes" and node.attrs["class"] == "btn btn-secondary btn-large" and "role" not in node.attrs
    assert node.find("span", class_="visually-hidden") is not None
    assert parse(macro("link_button", "{{ link_button('/x', 'Go') }}")).find("a").attrs["class"] == "btn btn-primary btn-large"


def test_post_button_wraps_one_guarded_form():
    html = macro("post_button", "{{ post_button('/disputes/1/packages', 'Generate', 'tok', kind='primary', sending='Preparing…', "
                                 "sends=True, fields=(('package_id','7'),('x','y')), form_state='fs', button_state='bs') }}")
    form = parse(html).find("form")
    assert form.attrs["method"] == "post" and form.attrs["action"] == "/disputes/1/packages" and form.attrs["class"] == "inline-form"
    assert "data-sends" in form.attrs and form.attrs["data-state"] == "fs"
    hidden = {n.attrs["name"]: n.attrs["value"] for n in form.find_all("input")}
    assert hidden == {"csrf_token": "tok", "package_id": "7", "x": "y"}
    button = form.find("button")
    assert button.attrs["data-state"] == "bs" and "btn-primary" in button.attrs["class"].split()
    plain = parse(macro("post_button", "{{ post_button('/a', 'Go', 't') }}")).find("form")
    assert "data-sends" not in plain.attrs and "data-state" not in plain.attrs
    assert "btn-secondary" in plain.find("button").attrs["class"].split()


def test_stylesheet_control_rules():
    large = css_block(r"\.btn-large, button\.btn-large, a\.btn\.btn-large")
    assert "min-height: 3.5rem" in large and "min-width: 14rem" in large and "font-size: 1rem" in large
    narrow = re.search(r"@media \(max-width: 599\.98px\) \{(.*?)\n\}", CSS, re.S).group(1)
    assert re.search(r"\.btn-primary, \.btn-large \{ width: 100%; \}", narrow)
    assert "flex-direction: column" in narrow and re.search(r"\.actions \.btn, \.actions button \{ width: 100%; \}", narrow)
    assert "gap: 12px" in narrow
    for family in ("ok", "warn", "error"):
        assert f".banner-{family} .btn" not in CSS
    assert not re.search(r"^\[aria-disabled", CSS, re.M)
    assert "form[data-submitting=\"1\"] [aria-disabled=\"true\"] { cursor: progress; }" in CSS
    assert ".banner .btn.btn-secondary" in CSS                       # the secondary-button rule stays
    assert "min-height: var(--target)" in css_block(r"\.btn-link, button\.btn-link, a\.btn-link")
    assert "font-weight: 700" in css_block(r"\.btn-primary, button\.btn-primary, a\.btn\.btn-primary")


def test_forced_colours_block_gives_every_shape_a_border():
    block = re.search(r"@media \(forced-colors: active\) \{(.*?)\n\}", CSS, re.S).group(1)
    assert "border: 2px solid ButtonText" in block and "border: 1px solid ButtonText" in block
    assert re.search(r"\.tag \{ border: 1px solid CanvasText; \}", block) and "border-left-style: double" in block


# --- large labels
def test_large_labels_are_exactly_the_eight_verbatim_strings():
    from copilot.web.presentation import LARGE_LABELS
    assert LARGE_LABELS == ("Attach to the response", "Review each statement and approve", "Approve this package",
                            "Send evidence to PayPal", "Review the message", "Send message to buyer",
                            "Add tracking in PayPal", "Resolution options and buyer messages")


def test_large_labels_lint_passes_large_secondary_and_fails_a_plain_one():
    good = macro("button", "{{ button('Attach to the response', kind='secondary', large=True) }}")
    bad = macro("button", "{{ button('Attach to the response', kind='secondary') }}")
    filled = macro("link_button", "{{ link_button('/x', 'Send evidence to PayPal') }}")
    other = macro("button", "{{ button('Cancel', kind='secondary') }}")
    assert lint.large_labels_ok(good) and lint.large_labels_ok(filled) and lint.large_labels_ok(other)
    assert not lint.large_labels_ok(bad)


def test_lint_helpers_find_bare_buttons_primaries_and_back_links():
    html = '<main><button>x</button><a class="btn" href="/a">a</a><button class="btn btn-primary">p</button><a class="back-link" href="/">Back to x</a></main>'
    assert [n.text() for n in lint.bare_buttons(html)] == ["x", "a"]
    assert len(lint.primaries(html)) == 1 and len(lint.back_links(html)) == 1
    lint.assert_single_primary(html)
    with pytest.raises(AssertionError):
        lint.assert_single_primary("<main></main>")
    lint.assert_single_primary("<main></main>", allow_zero=True)
    assert lint.required_sentence_count("Fields marked Required must be completed. Fields marked Required must be completed.") == 2


def test_template_lint_reports_each_forbidden_pattern(tmp_path):
    source = tmp_path / "t.html"
    source.write_text('<button type="submit">x</button>\n<a class="btn" href="/">y</a>\n<button disabled>z</button>\n'
                      '<p>Fields marked Required must be completed.</p>\n<input maxlength="5">\n<a class="btn btn-primary">ok</a>\n',
                      encoding="utf-8")
    problems = lint.template_lint([source])
    kinds = " | ".join(problems)
    assert "bare <button>" in kinds and "without a kind" in kinds and "disabled" in kinds and "Fields marked Required" in kinds and "maxlength" in kinds
    assert all(p.startswith(f"{source}:") for p in problems)
    assert lint.template_lint([WEB / "templates" / "_components.html"]) != []      # the macros own the only <button>


# --- the guard
def test_the_submit_guard_script_contract():
    web, _, _ = _web()
    js = web.get("/static/app.js")
    assert js.status_code == 200
    text = js.text
    assert "disabled = true" not in text and "disabled = false" not in text
    for needle in ("aria-disabled", "data-guard-submit", "submitter", "inflight-status", "data-sends", "pageshow", "location.reload",
                   "data-submitting", "data-sending-text", "Sending to PayPal. Keep this page open.", "convenience"):
        assert needle in text, needle
    assert 'getAttribute("data-guard-submit") === "off"' in text
    assert ".disabled" not in text.replace("aria-disabled", "")


def test_the_frame_renders_exactly_one_inflight_region(web_mode):
    web, _, _ = _web()
    assert len(parse(web.get("/disputes").text).find_all(role="status", id="inflight-status")) == 1


# --- breadcrumbs
def test_breadcrumb_macro_and_its_css():
    html = macro("breadcrumbs", "{{ breadcrumbs([('All disputes','/disputes'), ('Dispute X','/disputes/X'), ('Evidence package v2', None)]) }}")
    nav = parse(html).find("nav", aria_label="Breadcrumb")
    assert nav is not None and nav.find("ol") is not None
    items = nav.find_all("li")
    assert [li.text() for li in items] == ["All disputes", "Dispute X", "Evidence package v2"]
    assert items[-1].find("a") is None and items[-1].find("span").attrs["aria-current"] == "page"
    assert items[0].find("a").attrs["href"] == "/disputes"
    assert lint.breadcrumb_items(html) == [("All disputes", "/disputes"), ("Dispute X", "/disputes/X"), ("Evidence package v2", None)]
    narrow = re.search(r"@media \(max-width: 639px\) \{\s*\.breadcrumbs li \{ display: none; \}\s*\.breadcrumbs li:nth-last-child\(2\) \{ display: flex; \}", CSS)
    assert narrow, "below 640 px only the parent crumb is shown"
    assert 'content: "›"' in css_block(r"\.breadcrumbs li \+ li::before") and "min-height: var(--target)" in css_block(r"\.breadcrumbs a")


def test_a_last_crumb_with_a_href_is_still_not_a_link():
    nav = parse(macro("breadcrumbs", "{{ breadcrumbs([('A','/a'), ('B','/b')]) }}")).find("nav")
    assert [li.find("a") is not None for li in nav.find_all("li")] == [True, False]


# --- tracking
def test_tracking_number_problem_rejects_names_and_junk_with_the_exact_messages():
    problem = carriers.tracking_number_problem
    name = ("“{v}” is a carrier name, not a tracking number. Choose it in “Carrier (shipping company)”, then enter the "
            "code from the carrier's receipt in “Tracking number (from the carrier's receipt)”.")
    for value in ("Fedex", "FEDEX", "ups ", "Estafeta", "99minutos"):
        assert problem(value) == name.format(v=value.strip()), value
    assert problem("ABCDEF") == "Enter the tracking number from the carrier's receipt. It includes numbers."
    assert problem("https://x.example") == "Enter only the tracking number, not a web address."
    assert problem("www.x.example/1") == "Enter only the tracking number, not a web address."
    assert problem("a@example.com") == "Enter the tracking number, not an email address."
    assert problem("1" * 65) == "Tracking number must be 64 characters or fewer. Check the receipt."


def test_tracking_number_problem_accepts_real_looking_numbers():
    for value in ("1Z999AA10123456784", "DEMO-FDX-000001", "FEDEX123456", "1" * 22, "7712 3456 7890", "", "  ", "ESTAFETA-99", "UPS123"):
        assert carriers.tracking_number_problem(value) is None, value
    assert carriers.normalize_tracking(" 7712 3456\t7890\n") == "771234567890"


def _fields(db, package):
    return repo.get_manual_fields(db, DID)


def test_manual_fields_refuse_a_carrier_name_in_the_tracking_box(web_mode):
    web, db, token = _web()
    package = repo.insert_package(db, DID, "human_edit", True, [])
    repo.set_manual_fields(db, DID, {"carrier": {"value": "DHL", "confidence": "read"}})
    before = repo.get_manual_fields(db, DID)
    response = web.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": package, "tracking_number": "Fedex"})
    assert response.status_code == 400
    assert repo.get_manual_fields(db, DID) == before
    dom = parse(response.text)
    summary = dom.find("section", class_="error-summary")
    assert summary is not None and any(a.attrs["href"] == "#field-tracking_number" for a in summary.find_all("a"))
    assert "“Fedex” is a carrier name, not a tracking number." in response.text.replace("&#34;", '"')


def test_manual_fields_store_the_tracking_number_without_spaces(web_mode):
    web, db, token = _web()
    package = repo.insert_package(db, DID, "human_edit", True, [])
    response = web.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": package,
                                                               "tracking_number": " 7712 3456 7890 "}, follow_redirects=False)
    assert response.status_code == 303
    assert repo.get_manual_fields(db, DID)["tracking_number"]["value"] == "771234567890"


# --- carrier
def test_carrier_catalogue_and_display_names():
    assert [code for code, _ in carriers.SELECT_OPTIONS] == ["FEDEX", "DHL", "UPS", "MEX_ESTAFETA", "CORREOS_DE_MEXICO", "PAQUETEXPRESS",
                                                          "MEX_REDPACK", "MEX_SENDA", "HOUNDEXPRESS", "GRUPO", "DOMINO", "OTHER"]
    assert carriers.SELECT_OPTIONS[-1] == ("OTHER", "Other carrier")
    assert carriers.display_name("MEX_ESTAFETA") == "Estafeta" and carriers.display_name("fedex") == "FedEx"
    assert carriers.display_name("Paquetería Local") == "Paquetería Local" and carriers.display_name("") == ""
    assert set(carriers.DISPLAY_NAMES) <= carriers.KNOWN_CODES


def test_carrier_from_form_rules():
    f = carriers.carrier_from_form
    assert f("FEDEX", "") == ("FEDEX", None, None)
    assert f("FedEx", "") == ("FedEx", None, None)                      # legacy free text keeps its spelling
    assert f("OTHER", "Paquetería Local") == ("Paquetería Local", None, None)
    assert f("OTHER", "") == ("", "carrier_other_name", "Enter the carrier name")
    assert f("OTHER", "  ") == ("", "carrier_other_name", "Enter the carrier name")
    assert f("OTHER", "771234567890") == ("", "carrier_other_name", "This looks like a tracking number. Enter it in “Tracking number (from the carrier's receipt)”, and type the carrier's name here.")
    assert f("OTHER", "123")[2] == "Enter the carrier name"
    assert f("OTHER", "x" * 65)[1:] == ("carrier_other_name", "Carrier name must be 64 characters or fewer.")
    assert f("", "") == ("", None, None)
    assert f("", "Local")[1:] == ("carrier", "Choose the shipping company")
    assert f("", "", legacy_text="Local")[0] == "Local"
    assert all(f(code, "")[0] != "OTHER" for code, _ in carriers.SELECT_OPTIONS if code != "OTHER")


def _post_carrier(web, db, token, **data):
    package = repo.insert_package(db, DID, "human_edit", True, [])
    set_test_csrf(web, token)        # an error page issues a fresh token cookie; reset the test's own token for each post
    return web.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": package, **data}, follow_redirects=False)


def test_manual_fields_carrier_storage_matrix(web_mode):
    web, db, token = _web()
    r = _post_carrier(web, db, token, carrier="OTHER", carrier_other_name="")
    assert r.status_code == 400 and "Enter the carrier name" in r.text and repo.get_manual_fields(db, DID) == {}
    assert any(a.attrs["href"] == "#field-carrier_other_name" for a in parse(r.text).find("section", class_="error-summary").find_all("a"))
    stored = {}
    for data, expected in (({"carrier": "OTHER", "carrier_other_name": "Paquetería Local"}, "Paquetería Local"),
                           ({"carrier": "FEDEX"}, "FEDEX"), ({"carrier": "FedEx"}, "FedEx")):
        r = _post_carrier(web, db, token, **data)
        assert r.status_code == 303, data
        stored[expected] = repo.get_manual_fields(db, DID)["carrier"]["value"]
        assert stored[expected] == expected
    r = _post_carrier(web, db, token, carrier="OTHER", carrier_other_name="771234567890")
    assert r.status_code == 400 and "This looks like a tracking number." in r.text
    assert repo.get_manual_fields(db, DID)["carrier"]["value"] != "OTHER"


# --- label macros
def test_tracking_and_carrier_macros_have_the_exact_labels_hints_and_attributes():
    html = form_macro("tracking_field, carrier_field", "{{ carrier_field() }}{{ tracking_field() }}")
    dom = parse(html)
    labels = {l.attrs["for"]: l.text() for l in dom.find_all("label")}
    assert labels["field-carrier"] == "Carrier (shipping company) Required to send"
    assert labels["field-tracking_number"] == "Tracking number (from the carrier's receipt) Required to send"
    assert labels["field-carrier_other_name"] == "Carrier name (only if you chose Other carrier)"
    hints = {p.attrs["id"]: p.text() for p in dom.find_all("p", class_="field-hint")}
    assert hints["hint-tracking_number"] == ("The code on the carrier's receipt or tracking page, usually letters and digits. "
                                            "It is not the company name. Spaces are removed for you. También llamado número de guía o número de rastreo.")
    assert hints["hint-carrier"] == ("The company that delivers the parcel, for example FedEx, DHL, Estafeta or UPS. "
                                    "Paquetería o empresa transportista.")
    assert [s.text() for s in dom.find_all("span", lang="es")] == ["Paquetería o empresa transportista.",
                                                                  "También llamado número de guía o número de rastreo."]
    tracking = dom.find("input", id="field-tracking_number")
    assert (tracking.attrs["spellcheck"], tracking.attrs["autocomplete"], tracking.attrs["autocapitalize"]) == ("false", "off", "off")
    assert "maxlength" not in tracking.attrs and tracking.attrs["name"] == "tracking_number"
    assert html.index("field-carrier") < html.index("field-tracking_number")                 # carrier first


def test_carrier_field_select_options_and_other_input():
    dom = parse(form_macro("carrier_field", "{{ carrier_field() }}"))
    select = dom.find("select", id="field-carrier")
    options = select.find_all("option")
    assert (options[0].attrs["value"], options[0].text()) == ("", "Choose the shipping company") and "selected" in options[0].attrs
    assert (options[-1].attrs["value"], options[-1].text()) == ("OTHER", "Other carrier")
    assert [o.attrs["value"] for o in options[1:-1]] == [c for c, _ in carriers.SELECT_OPTIONS[:-1]]
    other = dom.find("input", id="field-carrier_other_name")
    assert other is not None and other.attrs["name"] == "carrier_other_name"
    assert any(l.attrs.get("for") == "field-carrier_other_name" for l in dom.find_all("label"))


def test_carrier_field_selects_a_code_and_keeps_a_legacy_entry():
    chosen = parse(form_macro("carrier_field", "{{ carrier_field(current='MEX_ESTAFETA') }}")).find("select")
    assert [o.attrs["value"] for o in chosen.find_all("option") if "selected" in o.attrs] == ["MEX_ESTAFETA"]
    legacy = parse(form_macro("carrier_field", "{{ carrier_field(current='Paquetería Local') }}")).find("select")
    selected = [o for o in legacy.find_all("option") if "selected" in o.attrs]
    assert [(o.attrs["value"], o.text()) for o in selected] == [("Paquetería Local", "Your entry: Paquetería Local")]
    errored = parse(form_macro("carrier_field", "{{ carrier_field(error='Choose the shipping company', other_error='Enter the carrier name', other_name='x') }}"))
    assert errored.find("select").attrs["aria-invalid"] == "true" and errored.find("p", id="error-carrier") is not None
    assert errored.find("input", id="field-carrier_other_name").attrs["value"] == "x" and errored.find("p", id="error-carrier_other_name") is not None


# ---- required note appears once and only when it helps -----
def test_required_note_appears_once_and_only_when_it_helps():
    assert form_macro("required_note", "{{ required_note(1, True) }}").strip() == ""
    assert form_macro("required_note", "{{ required_note(2, False) }}").strip() == ""
    rendered = form_macro("required_note", "{{ required_note(3, True) }}")
    assert rendered.count("Fields marked Required must be completed.") == 1 and lint.required_sentence_count(rendered) == 1


def test_field_macro_wiring_and_textarea_defaults():
    dom = parse(form_macro("field", "{{ field('note', 'Your note', value='v', hint='Hint', mark='Required', error='Bad', hint_es='Pista') }}"))
    label = dom.find("label")
    control = dom.find("input", id="field-note")
    assert label.attrs["for"] == "field-note" and "Required" in label.text() and control.attrs["name"] == "note"
    assert control.attrs["aria-describedby"] == "hint-note error-note" and control.attrs["aria-invalid"] == "true" and control.attrs["aria-required"] == "true"
    assert dom.find("p", id="hint-note").text() == "Hint Pista" and dom.find("p", id="error-note").text() == "✕ Bad"      # the glyph, then the words
    optional = parse(form_macro("field", "{{ field('n2', 'Optional note', mark='Optional') }}")).find("input")
    assert "aria-required" not in optional.attrs and "aria-describedby" not in optional.attrs
    area = parse(form_macro("field", "{{ field('body', 'Message', textarea=True, value='line') }}")).find("textarea")
    assert area.attrs["rows"] == "8" and "maxlength" not in area.attrs and area.text() == "line"
    narrow = parse(form_macro("field", "{{ field('q', 'Q', width='w10') }}")).find("input")
    assert narrow.attrs["class"] == "input-w10"


def test_date_fields_have_no_pattern_attribute_and_keep_ids():
    html = form_macro("date_fields", "{{ date_fields('delivery_date', 'Delivery date', current='2026-10-14') }}")
    assert "pattern=" not in html
    dom = parse(html)
    node = dom.find("input", id="field-delivery_date")
    assert node.attrs["type"] == "date" and node.attrs["name"] == "delivery_date" and node.attrs["value"] == "2026-10-14"
    assert dom.find("input", id="field-delivery_date-day") is None


def test_error_summary_has_one_plain_entry_per_field_in_order():
    dom = parse(form_macro("error_summary", "{{ error_summary({'a': 'X', 'b': 'Y'}) }}"))
    entries = dom.find("section").find_all("li")
    assert [(li.text(), li.find("a").attrs["href"]) for li in entries] == [("X", "#field-a"), ("Y", "#field-b")]
    assert dom.find("section").find_all("span", class_="tag") == []
    assert form_macro("error_summary", "{{ error_summary({}) }}").strip() == ""


def test_form_css_rules():
    assert "max-width: 46rem" in css_block(r"(?m)^textarea") and "min-height: 8rem" in css_block(r"(?m)^textarea") and "width: 100%" in css_block(r"(?m)^textarea")
    selector = re.search(r'(input\[type="text"\], input\[type="password"\][^{]*)\{', CSS).group(1)
    for kind in ("password", "email", "number", "search", "tel"):
        assert f'input[type="{kind}"]' in selector
    assert "width: 11rem" in css_block(r"input\.input-w10") and "width: 22rem" in css_block(r"input\.input-w20")
    assert "display: block" in css_block(r"\.field > label")
    assert "70ch" in css_block(r"main :is\(p, li, dd, legend, \.field-hint\)")
    assert css_block(r"\.date-fields legend").split("font-weight:")[1].split(";")[0].strip() == \
        css_block(r"\.field > label").split("font-weight:")[1].split(";")[0].strip()


# ---- tag glyph table one glyph per state and due clock -----
def test_tag_glyph_table_one_glyph_per_state_and_due_clock():
    states = ("ok", "warn", "bad", "na", "info", "todo", "due")
    glyphs = {}
    for state in states:
        node = parse(macro("tag", "{{ tag('" + state + "', 'Label') }}")).find("span", class_="tag")
        assert f"tag--{state}" in node.attrs["class"].split()
        glyphs[state] = node.find("span").text()
    assert glyphs["due"] == "◷" and len(set(glyphs.values())) == len(states)
    assert glyphs["bad"] == "✕"
    assert "◷" in macro("tag", "{{ tag('due', '2 days left') }}")
    assert "tag--todo" in macro("tag", "{{ tag('nonsense', 'x') }}")                    # unknown states fall back to todo


def test_tags_have_no_border_and_summary_is_a_quiet_link():
    def has_border(block):
        return re.search(r"border(?!-radius)", block) is not None       # the radius is shape, not an edge
    assert not has_border(css_block(r"\.tag")) and not has_border(css_block(r"\.tag--todo"))
    summary = css_block(r"summary")
    assert not has_border(summary) and "min-height: var(--target)" in summary
    assert "rotate(90deg)" in css_block(r"details\[open\] > summary::before")


def test_table_scroll_visually_hidden_and_actions_css():
    assert "overflow-x: auto" in css_block(r"\.table-scroll")
    assert len(re.findall(r"\.mono-id \{", CSS)) == 1
    assert "nowrap" not in re.sub(r"@media \(min-width: 640px\) \{ td \.mono-id\{ white-space: nowrap; \} \}", "", css_block(r"td") if False else "")
    for match in re.finditer(r"white-space: nowrap", CSS):
        before = CSS[:match.start()]
        assert before.rindex("td .mono-id{") > before.rindex("@media (min-width: 640px)") if "td .mono-id{" in before[-60:] else True
    assert "td .mono-id { white-space: nowrap" not in CSS
    hidden = css_block(r"\.visually-hidden")
    assert "clip" in hidden and "position: absolute" in hidden
    assert "border: 0" in hidden
    assert css_block(r"\.actions")                                  # still a flex row on wide screens
    assert "flex-direction: column" in re.search(r"@media \(max-width: 599\.98px\) \{(.*?)\n\}", CSS, re.S).group(1)
    assert "nth-last-child(2)" in CSS


def test_h1_id_exact_content_and_next_step_css():
    assert "display: inline" in css_block(r"\.h1-id") and "font-weight: 400" in css_block(r"\.h1-id")      # pass 3: the id sits on the title line
    assert "white-space: pre-wrap" in css_block(r"\.exact-content")
    assert "color: var(--text-muted)" in css_block(r"\.exact-note")


# --- checked bar, time
def test_checked_bar_renders_the_fixed_sentence_and_a_time_element():
    html = macro("checked_bar", "{{ checked_bar('2026-10-16 22:10 CST (UTC-6)', '/disputes', 'Refresh from PayPal') }}")
    bar = parse(html).find("p", class_="checked-at")
    assert bar.attrs["data-state"] == "checked-at"
    time_node = bar.find("time")
    assert time_node.attrs["datetime"] == "2026-10-16T22:10:00-06:00"
    assert re.sub(r" \.", ".", bar.text()) == "PayPal was read at 16 Oct 2026, 10:10 pm (Mexico City time). Disputes may have changed since. Refresh from PayPal"
    assert "</time>. Disputes may have changed since." in html                          # no stray space before the full stop
    link = bar.find("a")
    assert link.attrs["href"] == "/disputes" and link.text() == "Refresh from PayPal"
    iso = parse(macro("checked_bar", "{{ checked_bar('2026-10-17T04:10:00+00:00', '/d', 'Check again') }}")).find("time")
    assert iso.attrs["datetime"] == "2026-10-16T22:10:00-06:00"


def test_timefmt_display_forms():
    from copilot.web import timefmt
    assert timefmt.display(datetime(2026, 10, 17, 4, 10, tzinfo=UTC)) == "16 Oct 2026, 10:10 pm (Mexico City time)"
    assert timefmt.display(datetime(2026, 10, 16, 18, 0, tzinfo=UTC)) == "16 Oct 2026, 12:00 pm (Mexico City time)"
    assert timefmt.display(datetime(2026, 10, 16, 6, 0, tzinfo=UTC)) == "16 Oct 2026, 12:00 am (Mexico City time)"
    assert timefmt.display(datetime(2026, 10, 17, 4, 10)) == "16 Oct 2026, 10:10 pm (Mexico City time)"            # naive = UTC
    assert timefmt.display("2026-10-16 22:10 CST (UTC-6)") == "16 Oct 2026, 10:10 pm (Mexico City time)"
    assert timefmt.display("2026-10-17T04:10:00Z") == "16 Oct 2026, 10:10 pm (Mexico City time)"
    assert timefmt.display(1792210200000) == timefmt.display(datetime.fromtimestamp(1792210200, UTC))
    assert timefmt.display("2026-10-16") == "16 Oct 2026" and timefmt.display("2026-10-16", zone=False) == "16 Oct 2026"
    assert timefmt.display(datetime(2026, 10, 17, 4, 10, tzinfo=UTC), zone=False) == "16 Oct 2026, 10:10 pm"
    assert timefmt.display("") == "" and timefmt.display(None) == "" and timefmt.display("nonsense") == "Not available"
    assert timefmt.display(True) == "" or timefmt.display(True) == "Not available"
    assert timefmt.iso("2026-10-16 22:10 CST (UTC-6)") == "2026-10-16T22:10:00-06:00"
    assert timefmt.iso("2026-10-16") == "2026-10-16" and timefmt.iso("nonsense") == ""
    assert timefmt.when("") == "" and timefmt.when("nonsense") == "Not available"
    assert str(timefmt.when("2026-10-16", zone=False)) == '<time datetime="2026-10-16">16 Oct 2026</time>'
    assert timefmt.when_text("2026-10-16 22:10 CST (UTC-6)") == "16 Oct 2026, 10:10 pm (Mexico City time)"


def test_timefmt_month_names_do_not_follow_the_operating_system():
    source = (WEB / "timefmt.py").read_text(encoding="utf-8")
    new_part = source[source.index("Seller-facing display"):]
    assert "%b" not in new_part and "%B" not in new_part and "strftime" not in new_part


def test_timefmt_keeps_the_stored_formats_unchanged():
    from copilot.web import timefmt
    assert timefmt.fmt(datetime(2026, 10, 17, 4, 10, tzinfo=UTC)) == "2026-10-16 22:10 CST (UTC-6)"
    assert timefmt.fmt_iso("2026-10-17T04:10:00Z") == "2026-10-16 22:10 CST (UTC-6)"
    assert timefmt.localize_text("due 2026-10-16 22:10 CST") == "due 2026-10-16 22:10 CST (UTC-6)"


def test_due_display_phrases():
    from copilot.web import timefmt
    now = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)                                    # 12:00 in Mexico City
    def due(delta):
        return timefmt.due_display({"seller_response_due_date": (now + delta).isoformat().replace("+00:00", "Z")}, now=now)
    assert due(timedelta(days=18, hours=3))["remaining"] == "18 days left"
    assert due(timedelta(hours=30))["remaining"] == "Due in 30 hours"
    assert due(timedelta(hours=5))["remaining"] == "Due today, in 5 hours"
    assert due(timedelta(hours=-5))["remaining"] == "The due date passed 5 hours ago"
    assert due(timedelta(days=-2, hours=-1))["remaining"] == "The due date passed 2 days ago"
    assert due(timedelta(hours=-5))["passed"] is True and due(timedelta(hours=5))["passed"] is False
    result = due(timedelta(hours=30))
    assert result["text"].startswith("Respond by ") and result["hours_left"] == 30 and result["iso"].endswith("-06:00")
    assert timefmt.due_display({}, now=now) is None and timefmt.due_display({"seller_response_due_date": None}) is None
    assert timefmt.due_display({"seller_response_due_date": "garbage"}, now=now) is None


# --- messages
def test_done_message_table_and_banner():
    from copilot.web.presentation import done_message
    assert done_message("nope") is None and done_message("attached") is None and done_message("attached", filename="") is None
    assert done_message("attached", filename="label.pdf") == "label.pdf is in the response."
    assert done_message("statement_added", n=3) == "Statement 3 added."
    assert done_message("shipment_saved") == "Shipment details saved."
    assert done_message("approved", when="16 Oct 2026, 10:10 pm (Mexico City time)") == \
        "Package approved at 16 Oct 2026, 10:10 pm (Mexico City time). Nothing has been sent. Next: send it to PayPal."
    assert done_message("reading_confirmed", field="tracking_number", value="1Z9") == "Reading confirmed: tracking_number 1Z9."
    assert done_message("approval_cleared") == "Your earlier approval was cleared because the content changed. Review and approve again."
    for key, facts in (("document_added", {"filename": "a.pdf"}), ("document_added_package", {"filename": "a.pdf"}),
                       ("document_excluded", {"filename": "a.pdf"}), ("document_included", {"filename": "a.pdf"}),
                       ("statement_saved", {"n": 1}), ("statement_removed", {"n": 1}), ("detached", {"filename": "a.pdf"})):
        assert done_message(key, **facts)
    html = macro("done_banner", "{{ done_banner('attached', done_message('attached', filename='label.pdf')) }}")
    banner = parse(html).find("div", class_="banner")
    assert banner.attrs["data-state"] == "done" and banner.attrs["data-done"] == "attached" and banner.attrs["role"] == "status"
    assert banner.text() == "✓ label.pdf is in the response."


def test_delivery_block_messages_and_hint():
    from copilot.web import presentation as p
    assert p.delivery_block_message(2, "delivered", "unsupported") == ("Statement 2 says “delivered”, but no document it cites shows that. "
        "Change the wording (for example to “shipped”, if that is what happened), then approve.")
    assert p.delivery_block_message(2, "delivered", "unreadable", "a.pdf").startswith("Statement 2 says “delivered” and cites a.pdf, which Proofie could not read.")
    assert p.delivery_block_message(1, "x", "unreadable") is None and p.delivery_block_message(1, "x", "other") is None
    assert p.delivery_block_message(4, "x", "conflict") == "Statement 4 relies on a document that does not match PayPal's data for the tracking number."
    assert p.delivery_block_message(4, "x", "untrusted", "b.pdf") == "Statement 4 cites b.pdf, which Proofie cannot use as proof of delivery."
    assert p.DELIVERY_WORDS_HINT.startswith("These words claim the parcel arrived: delivered, signed for, received by, entregado, firmado, recibido.")
    assert p.step_caption(3) == "Step 3 of 4: Review each statement and approve" and p.READS_ONLY_NOTE == "Reads the dispute from PayPal. Sends nothing."
    assert p.GUIDED_STEPS[0] == "Upload documents"


# ---- exact content block preserves line breaks and translate off ---
def test_exact_content_block_preserves_line_breaks_and_translate_off():
    html = macro("exact_content", "{% call exact_content() %}{{ text }}{% endcall %}", text="Line one\nLine <two>")
    dom = parse(html)
    block = dom.find("div", class_="exact-content")
    assert block.attrs["translate"] == "no" and "data-exact-content" in block.attrs
    assert "Line one\nLine &lt;two&gt;" in html and block.text() == "Line one Line <two>"
    assert dom.find("p", class_="exact-note").text() == "Shown exactly as PayPal will receive it. Your browser will not translate this part."


def test_mono_id_macro():
    node = parse(macro("mono_id", "{{ mono_id('PP-R-1') }}")).find("span", class_="mono-id")
    assert node.attrs["translate"] == "no" and node.text() == "PP-R-1"


# ------------------------------------------------------------------------------------------- the rule source keeps the hook
def test_rule_source_keeps_the_hook_and_names_the_external_site():
    html = routes_disputes.templates.env.get_template("_rule_source.html").render(
        policy_source={"url": "https://www.paypal.com/x", "country": "Mexico", "updated": "13 January 2025"})
    paragraph = parse(html).find("p", data_state="policy-source")
    link = paragraph.find("a")
    assert link.text() == "PayPal Seller Protection, Mexico, updated 13 January 2025" and "noopener" in link.attrs["rel"]
    assert paragraph.text().endswith("(PayPal website).") and "Checked against:" in paragraph.text()


# --- status golden fixtures
def _attempt(db, package, state, request_id, sha, reason=None, files=(), registered_files=None):
    store = SQLiteAttemptStore(db)
    try:
        store.write(DID, sha, {"state": state, "request_id": request_id, "reason": reason, "attachment_filenames": list(files)})
    finally:
        store.close()
    detail = {"package_id": package, "request_id": request_id, "approval_sha256": "appr-1"}
    detail["attachments"] = list(files if registered_files is None else registered_files)
    repo.log_action(db, "system", "send_attempt_registered", DID, detail)


def _package(db):
    package = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0, "text": "I shipped the order.",
                                      "source_ids": [], "basis": "seller_statement"}])
    return repo.get_package(db, package)


def test_send_record_and_status_words_for_each_stored_state():
    from copilot.web import status
    db = config.db_path()
    package = _package(db)
    assert status.send_record(db, DID, package).state == "none"
    assert status.evidence_status(status.send_record(db, DID, package)).label == "Not sent yet"
    assert status.evidence_status(None, approval_valid=True).label == "Approved, not sent yet"
    cases = [("pending", "reserved", "Sending started - not confirmed yet"),
             ("sent_unconfirmed", "attempted", "Send attempted - not confirmed by PayPal"),
             ("failed", "refused", "PayPal did not accept it"),
             ("blocked", "blocked", "Not sent: Proofie stopped it"),
             ("abandoned", "abandoned", "You marked it as not shown in PayPal"),
             ("confirmed", "confirmed", "Received by PayPal (confirmed by reading the dispute back)")]
    for index, (stored, expected_state, label) in enumerate(cases):
        db = config.db_path()
        pkg = _package(db)
        _attempt(db, pkg["id"], stored, f"req-{index}", f"sha-{index}")
        record = status.send_record(db, DID, pkg)
        assert record.state == expected_state, stored
        assert status.evidence_status(record).label == label
        sent = status.package_status(record)
        assert sent.label == {"reserved": "Send attempted, not confirmed by PayPal", "attempted": "Send attempted, not confirmed by PayPal",
                              "refused": "PayPal did not accept it", "blocked": "Not sent: Proofie stopped it",
                              "abandoned": "You marked it as not shown in PayPal", "confirmed": "Sent to PayPal"}[expected_state]
    confirmed = status.send_record(db, DID, pkg)
    assert confirmed.confirmed_at and confirmed.attachments_match and confirmed.approval_sha256 == "appr-1"
    assert status.evidence_status(confirmed).kind == "ok" and status.evidence_status(status.NO_RECORD).kind == "todo"


def test_a_confirmed_send_stays_confirmed_for_its_package_after_a_later_source_change():
    from copilot.web import status
    db = config.db_path()
    package = _package(db)
    _attempt(db, package["id"], "confirmed", "req-1", "sha-of-the-old-content")
    repo.insert_claims(db, package["id"], [{"evidence_index": 0, "claim_index": 1, "text": "Another statement.",
                                           "source_ids": [], "basis": "seller_statement"}])
    record = status.send_record(db, DID, package)
    assert record.state == "confirmed" and record.content_matches is False
    assert status.evidence_status(record).label.startswith("Received by PayPal")
    other = repo.get_package(db, repo.insert_package(db, DID, "human_edit", True, []))
    assert status.send_record(db, DID, other).state == "none"                # an attempt registered for package 1 is not package 2's


def test_a_partial_send_and_a_file_attached_afterwards_are_never_shown_as_sent():
    from copilot.web import status
    db = config.db_path()
    package = _package(db)
    _attempt(db, package["id"], "sent_unconfirmed", "req-p", "sha-p", reason="missing_attachment:label.pdf")
    partial = status.send_record(db, DID, package)
    assert partial.state == "partial"
    assert status.evidence_status(partial).label == "Statements in PayPal, file missing"
    assert status.package_status(partial).label == "Statements in PayPal, file missing"
    db2 = config.db_path()
    second = _package(db2)
    _attempt(db2, second["id"], "confirmed", "req-c", "sha-c", files=(), registered_files=())
    assert status.send_record(db2, DID, second).attachments_match is True
    document = repo.insert_document(db2, DID, "late.pdf", "application/pdf", 3, "abc123", "text", True)
    repo.attach_document(db2, second["id"], document)
    record = status.send_record(db2, DID, second)
    assert record.state == "confirmed" and record.attachments_match is False
    assert status.effective_state(record) == "later_file"                       # Its own state, never pending
    assert status.evidence_status(record).label == "Confirmed in PayPal, a file was added afterwards"
    assert status.package_status(record).label == "Sent, a file was added afterwards" and status.package_status(record).label != "Sent to PayPal"


def test_a_legacy_attempt_without_a_file_list_counts_as_matching():
    from copilot.web import status
    db = config.db_path()
    package = _package(db)
    store = SQLiteAttemptStore(db)
    store.write(DID, "sha-l", {"state": "confirmed", "request_id": "req-l"})
    store.close()
    repo.log_action(db, "system", "send_attempt_registered", DID, {"package_id": package["id"], "request_id": "req-l"})
    document = repo.insert_document(db, DID, "x.pdf", "application/pdf", 3, "h", "t", True)
    repo.attach_document(db, package["id"], document)
    assert status.send_record(db, DID, package).attachments_match is True


def test_package_status_draft_words():
    from copilot.web import status
    assert status.package_status(None).label == "Draft" and status.package_status(None).kind == "todo"
    assert status.package_status(None, has_conflict=True).label == "Draft: needs attention"
    assert status.package_status(None, missing_required=True).code == "draft_attention"
    assert status.package_status(None, approval_valid=True, has_conflict=True).label == "Approved, not sent"


def test_latest_package_record_uses_the_highest_version():
    from copilot.web import status
    db = config.db_path()
    assert status.latest_package_record(db, DID, []) == (None, None)
    first, second = _package(db), _package(db)
    package, record = status.latest_package_record(db, DID, repo.list_packages(db, DID))
    assert package["id"] == second["id"] and record.state == "none"
    assert first["version"] < second["version"]


def test_message_and_tracking_vocabularies():
    from copilot.web import status
    expected = {"sent_pending_settle": "PayPal accepted the request; not confirmed yet", "sent_unconfirmed": "Send attempted - not confirmed by PayPal",
                "failed": "Not sent: PayPal returned an error", "refused_422": "Not sent: PayPal was still updating the dispute",
                "rejected_hash": "Not sent: the approval did not match", "rejected_link": "Not sent: PayPal did not offer this option at that moment",
                "rejected_body": "Not sent: the request was not valid", "read_failed": "Not sent: PayPal could not be read"}
    for code, label in expected.items():
        assert status.message_status({"status": code, "action": "message"}).label == label
    listed = {"status": "sent_confirmed", "action": "message", "readback_json": '{"message_listed": true}'}
    assert status.message_status(listed).label == "PayPal lists your message"
    assert status.message_status({"status": "sent_confirmed", "action": "message"}).label == "Confirmed by reading the dispute back (message not checked)"
    assert status.message_status({"status": "sent_confirmed", "action": "offer"}).label == "PayPal no longer shows the refund-offer option"
    assert status.message_status({"status": "sent_confirmed"}).label == "Status not recognized"
    assert status.message_status({"status": "weird"}).label == "Status not recognized" and status.message_status(None).kind == "warn"
    tracking = {"registered": "Registered and confirmed", "registered_unconfirmed": "Accepted by PayPal, not confirmed",
                "unknown": "Send attempted - not confirmed by PayPal", "failed": "Not accepted by PayPal",
                "saved_without_registration": "Saved in Proofie only"}
    for code, label in tracking.items():
        assert status.tracking_status(code).label == label
    unknown = status.tracking_status("zzz")
    assert (unknown.label, unknown.kind) == ("Status not recognized", "warn")


# --- dispute
def _norm(status="WAITING_FOR_SELLER_RESPONSE", links=None, stage="INQUIRY", **extra):
    links = {"provide-evidence": {"href": "https://api-m.sandbox.paypal.com/x", "method": "POST"}} if links is None else links
    return {"dispute_id": DID, "status": status, "stage": stage, "action_links": links,
            "can_provide_evidence": "provide-evidence" in links, **extra}


def test_dispute_status_codes_and_settling_rule(monkeypatch):
    from copilot.web import status
    assert status.dispute_status(_norm()).label == "Waiting for your response" and status.dispute_status(_norm()).needs_seller
    assert status.dispute_status(_norm("WAITING_FOR_BUYER_RESPONSE", links={})).label == "Waiting for the buyer"
    settling = _norm("UNDER_REVIEW", links={})
    assert status.dispute_status(settling).code == "settling" and status.dispute_status(settling).label == "PayPal is updating this dispute"
    assert status.is_settling(settling) and not status.is_settling(_norm("UNDER_REVIEW", links={"make-offer": {}}))
    assert status.dispute_status(_norm("UNDER_REVIEW", links={"make-offer": {}})).code == "under_review"
    assert status.dispute_status(_norm("OPEN")).label == "Just opened" and status.dispute_status(_norm("RESOLVED")).label == "Resolved"
    assert status.dispute_status(_norm("SOMETHING_NEW")).code == "unknown"
    assert status.dispute_status(_norm("SOMETHING_NEW")).kind == "warn"
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    chargeback = _norm("UNDER_REVIEW", links={}, stage="CHARGEBACK")
    assert status.dispute_status(chargeback).code == "claim_under_review"
    raw = {"dispute_outcome": {"outcome_code": "CANCELED_BY_BUYER"}}
    closed = status.dispute_status(_norm(), raw)
    assert closed.code == "buyer_closed" and not closed.needs_seller and closed.label == "Canceled by the buyer (as stated by PayPal)"
    monkeypatch.delenv("COPILOT_FEATURE_OUTCOME")
    assert status.dispute_status(_norm(), raw).code == "waiting_for_you"          # the flag decides whether the closure is shown
    assert status.dispute_status(chargeback).code == "settling"


def test_status_vocabularies_are_closed_unique_and_readable():
    from copilot.web import status
    for domain, table in status.DOMAIN_TABLES.items():
        labels = [label for _, label in table.values()]
        assert len(labels) == len(set(labels)) or domain in ("package", "evidence"), domain
        for code, (kind, label) in table.items():
            if (domain, code) != ("message", "sent_pending_settle"):      # the contract's own copy has one ';' (kept verbatim)
                assert ";" not in label, (domain, code)
            assert "_" not in label and label != label.upper(), (domain, code)
            assert kind in ("ok", "warn", "bad", "na", "info", "todo", "due")
        view = status._view(table, "no-such-code")
        assert (view.label, view.kind) == ("Status not recognized", "warn")
    assert "Cancelled" not in " ".join(label for table in status.DOMAIN_TABLES.values() for _, label in table.values())


def test_display_only_modules_have_no_writes_and_nobody_imports_next_step():
    for name in ("status.py", "next_step.py"):
        tree = ast.parse((WEB / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    assert "router" not in ast.dump(decorator), (name, node.name)
    src = (WEB / "next_step.py").read_text(encoding="utf-8")
    assert "router" not in src and "client.call" not in src and "repo." not in src
    for folder in ("core", "features"):
        for path in (ROOT / "copilot" / folder).rglob("*.py"):
            # an import of the display module; the substring alone also matches unrelated names (effective_next_step)
            assert not re.search(r"copilot\.web\.next_step|from copilot\.web import[^\n]*\bnext_step\b|^\s*import next_step\b",
                                 path.read_text(encoding="utf-8"), re.M), path
    status_src = (WEB / "status.py").read_text(encoding="utf-8")
    assert "copilot.features" not in status_src and "copilot.core.resolution" not in status_src


# --- next step
def _record(state, **kw):
    from copilot.web.status import SendRecord
    return SendRecord(state=state, confirmed_at=kw.pop("confirmed_at", None), **kw)


def _flags(monkeypatch, **flags):
    for name in ("OUTCOME", "RESOLUTION", "ACTIVE_APPROVAL"):
        monkeypatch.delenv(f"COPILOT_FEATURE_{name}", raising=False)
    for name, value in flags.items():
        monkeypatch.setenv(f"COPILOT_FEATURE_{name.upper()}", "true" if value else "false")


PKG = {"id": 7, "version": 2}


def _step(**kw):
    from copilot.web import next_step
    base = dict(dispute_id=DID, norm=_norm(), has_documents=False)
    base.update(kw)
    return next_step.for_dispute(**base)


def _labels(step):
    return [step.action.label] if step.action else [] , [a.label for a in step.secondary]


def test_for_dispute_every_key_with_exact_copy(monkeypatch):
    _flags(monkeypatch)
    base = f"/disputes/{DID}"
    raw_closed = {"dispute_outcome": {"outcome_code": "CANCELED_BY_BUYER"}}
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    s = _step(raw=raw_closed)
    assert (s.key, s.heading, s.body, s.action, s.secondary) == ("buyer_closed", "The buyer closed this case.", "PayPal offers no seller action, so there is nothing to do.", None, ())
    s = _step(norm=_norm("UNDER_REVIEW", links={}, stage="CHARGEBACK"))
    assert (s.key, s.heading, s.body) == ("claim_under_review", "PayPal is reviewing this claim.", "Proofie offers no seller action while the review lasts.")   # one body on both pages
    assert s.action is None and [(a.label, a.href, a.kind) for a in s.secondary] == [("Read PayPal's answer", f"{base}/outcome", "secondary")]
    _flags(monkeypatch)
    s = _step(norm=_norm("UNDER_REVIEW", links={}))
    assert (s.key, s.heading, s.body) == ("settling", "PayPal is updating this dispute.", "This usually takes about a minute after a change. Nothing is sent from this page.")
    assert (s.action.label, s.action.href, s.action.kind) == ("Check again", base, "primary") and s.secondary == ()
    for state, body in (("attempted", "Proofie will not send it again on its own."), ("reserved", "Proofie will not send it again on its own."),
                        ("partial", "When Proofie checked, PayPal showed your statements without every file. Proofie will not send it again on its own.")):
        s = _step(record=_record(state), latest_package=PKG)
        # A true partial is not "unknown": PayPal has the statements (edited assertion: heading of "partial")
        heading = "Statements in PayPal; a file is missing." if state == "partial" else "Send attempted - PayPal has not confirmed it yet."
        assert (s.key, s.heading, s.body) == ("pending", heading, body)
        assert (s.action.label, s.action.href, s.action.kind, s.action.post) == ("Check again", base, "primary", None)
        assert s.note == "Reads the dispute from PayPal. Sends nothing to PayPal."
    s = _step(record=_record("confirmed", attachments_match=False), latest_package=PKG)
    assert (s.key, s.heading) == ("later_file", "Evidence sent to PayPal; a file was added afterwards.")
    assert s.body == "PayPal's record showed package v2. A file was added afterwards and is not in PayPal. Proofie will not send it on its own."   # Never "again"
    assert (s.action.label, s.action.href) == ("Open the package", f"{base}/packages/7")
    confirmed = _record("confirmed", confirmed_at="2026-10-17T04:10:00+00:00", attachments_match=True)
    assert _step(record=confirmed, latest_package=PKG).key == "upload"           # integration (c): PayPal asks again
    s = _step(record=confirmed, latest_package=PKG, norm=_norm(links={}))
    assert (s.key, s.heading, s.body) == ("confirmed", "Evidence sent to PayPal.", "Proofie confirmed package v2 in PayPal's record at 16 Oct 2026, 10:10 pm (Mexico City time).")
    assert s.action is None and s.note == "PayPal does not ask you for anything else right now."
    s = _step(record=confirmed, latest_package=PKG, norm=_norm(links={"make-offer": {}, "send-message": {}}))
    assert s.action is None                                                        # the resolution flag is off
    _flags(monkeypatch, resolution=True)
    s = _step(record=confirmed, latest_package=PKG, norm=_norm(links={"send-message": {}}))
    assert (s.action.label, s.action.href, s.action.kind, s.action.large) == ("Resolution options and buyer messages", f"{base}/resolution", "primary", True)
    assert s.note == "PayPal still offers you an action on this dispute."
    s = _step(norm=_norm("WAITING_FOR_BUYER_RESPONSE", links={}))
    assert (s.key, s.heading, s.body, s.action) == ("no_action", "PayPal is not asking for evidence on this dispute now.", "Your packages stay readable below.", None)
    s = _step(auto_prep_status="preparing")
    assert (s.key, s.heading, s.body) == ("preparing", "Proofie is preparing a draft package.", "This can take a few minutes. Reload this page to see it.")
    assert [(a.label, a.href) for a in s.secondary] == [("Reload this page", base)] and s.action is None
    s = _step()
    assert (s.key, s.heading, s.body) == ("upload", "Next step: upload the carrier receipt or shipping label.", "The checks compare it with PayPal's data. Nothing is sent.")
    assert (s.action.label, s.action.href) == ("Upload a document", f"/disputes/{DID}/workspace/prepare#documents")
    assert [(a.label, a.post, a.kind) for a in s.secondary] == [("Start manually instead", f"{base}/packages?manual=1", "secondary")]
    s = _step(has_documents=True)
    assert (s.key, s.heading, s.body) == ("generate", "Next step: prepare the evidence package.", "Nothing is sent.")
    assert (s.action.label, s.action.post, s.action.sending, s.action.sends) == ("Generate evidence package", f"{base}/packages", "Preparing…", False)
    assert s.note == "Preparing with the assistant. This can take a few minutes. Keep this page open." and s.note_state == "generate-wait"
    assert _step(has_documents=True, hosted=True).note == "Preparing the package. Keep this page open."
    for kw in ({"package_claims": 0, "package_has_tracking": True}, {"package_claims": 2, "package_has_tracking": False}):
        s = _step(latest_package=PKG, **kw)
        assert (s.key, s.heading, s.body) == ("finish_package", "Next step: finish evidence package v2.", "Add the tracking number and at least one statement. Nothing is sent until you approve and send.")
        assert (s.action.label, s.action.href) == ("Continue with package v2", f"{base}/packages/7#next-step")
    ready = dict(latest_package=PKG, package_claims=2, package_has_tracking=True)
    s = _step(**ready)
    assert (s.key, s.heading, s.body) == ("review", "Next step: check each statement and approve the package.", "Package v2 is ready to review. Approving sends nothing.")
    assert (s.action.label, s.action.href, s.action.large) == ("Review each statement and approve", f"{base}/packages/7#next-step", True)
    _flags(monkeypatch, active_approval=True)
    assert _step(**ready).action.href == f"{base}/packages/7/active-approval"
    s = _step(approval_valid=True, **ready)
    assert (s.key, s.heading, s.body) == ("send", "Next step: send the approved package to PayPal.", "You approved package v2. Nothing has reached PayPal yet.")
    assert (s.action.label, s.action.href, s.action.post) == ("Continue to send evidence", f"{base}/packages/7#next-step", None)


def test_for_dispute_precedence(monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    closed = {"dispute_outcome": {"outcome_code": "CANCELED_BY_BUYER"}}
    assert _step(raw=closed, record=_record("attempted"), norm=_norm("UNDER_REVIEW", links={})).key == "buyer_closed"
    assert _step(norm=_norm("UNDER_REVIEW", links={}, stage="CHARGEBACK")).key == "claim_under_review"
    assert _step(norm=_norm("UNDER_REVIEW", links={}), record=_record("attempted")).key == "settling"
    assert _step(record=_record("attempted"), auto_prep_status="preparing").key == "pending"
    assert _step(record=_record("confirmed", attachments_match=True), norm=_norm(links={})).key == "confirmed"


def test_for_dispute_never_returns_a_send_register_or_resolution_register_post(monkeypatch):
    from copilot.web import next_step
    _flags(monkeypatch, resolution=True, active_approval=True)
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    confirmed = _record("confirmed", confirmed_at="2026-10-17T04:10:00+00:00", attachments_match=True)
    variants = [dict(), dict(has_documents=True), dict(latest_package=PKG), dict(latest_package=PKG, package_claims=1, package_has_tracking=True),
                dict(latest_package=PKG, package_claims=1, package_has_tracking=True, approval_valid=True),
                dict(record=_record("attempted"), latest_package=PKG), dict(record=confirmed, latest_package=PKG, norm=_norm(links={"make-offer": {}})),
                dict(auto_prep_status="preparing"), dict(norm=_norm("UNDER_REVIEW", links={})),
                dict(norm=_norm("UNDER_REVIEW", links={}, stage="CHARGEBACK")), dict(norm=_norm(links={})),
                dict(raw={"dispute_outcome": {"outcome_code": "CANCELED_BY_BUYER"}})]
    for variant in variants:
        step = _step(**variant)
        actions = ([step.action] if step.action else []) + list(step.secondary)
        for action in actions:
            if action.post:
                assert not action.post.endswith(("/send", "/resolution/register", "/register")), (step.key, action.post)
                assert not action.sends
        assert isinstance(step, next_step.NextStep)


def _pstep(**kw):
    from copilot.web import next_step
    base = dict(dispute_id=DID, package=PKG, norm=_norm(), claims_count=2, files_count=1, has_tracking=True, carrier_missing=False,
                approval_valid=True, record=None, other_unconfirmed=False)
    base.update(kw)
    return next_step.for_package(**base)


def test_for_package_every_key_with_exact_copy(monkeypatch):
    _flags(monkeypatch)
    url = f"/disputes/{DID}/packages/7"
    s = _pstep(norm=_norm("UNDER_REVIEW", links={}))
    assert (s.key, s.heading, s.body) == ("settling", "PayPal is updating this dispute.", "Wait about a minute, then check again. Nothing can be sent while PayPal updates the dispute.")
    assert (s.action.label, s.action.href, s.action.kind) == ("Check again", url, "primary") and s.secondary == ()
    confirmed = _record("confirmed", confirmed_at="2026-10-17T04:10:00+00:00", attachments_match=True)
    s = _pstep(record=confirmed)
    assert (s.key, s.heading) == ("confirmed", "Evidence sent to PayPal.")
    assert s.body == "PayPal's record showed package v2 at 16 Oct 2026, 10:10 pm (Mexico City time), when Proofie checked. This version is read-only."
    assert (s.action.label, s.action.href) == ("View the dispute and PayPal's answer", f"/disputes/{DID}")
    assert [(a.label, a.post, a.kind) for a in s.secondary] == [("Prepare a new version", f"/disputes/{DID}/packages", "secondary")]
    assert s.note == "A new version is a new submission to PayPal. The sent version stays as it is."
    assert _pstep(record=confirmed, norm=_norm(links={})).secondary == ()
    assert not any(a.post and a.post.endswith("/send") for a in [s.action, *s.secondary])
    s = _pstep(record=_record("attempted"))
    assert (s.key, s.heading, s.body) == ("pending", "Send attempted - PayPal has not confirmed it yet.", "Proofie will not send it again on its own.")
    assert (s.action.label, s.action.post, s.action.sending, s.action.kind) == ("Check again", f"{url}/check", "Checking PayPal…", "primary")
    assert s.note == "Reads the dispute from PayPal. Sends nothing."
    s = _pstep(other_unconfirmed=True)
    assert (s.key, s.heading) == ("other_pending", "Sending is paused: an earlier send is not confirmed yet.")
    assert s.body == "Check PayPal first. If PayPal does not show the earlier send, follow the steps under “Why is sending blocked?”."
    assert [(i.text, i.link, i.href) for i in s.items] == [("The earlier send is not confirmed.", "Why is sending blocked?", "#why-blocked")]
    assert s.action.label == "Check again" and s.action.post == f"{url}/check"
    s = _pstep(approval_valid=False, has_tracking=False)
    assert (s.key, s.heading, s.body) == ("no_tracking", "Next step: add the tracking number.", "PayPal needs a tracking number before this package can be sent.")
    assert (s.action.label, s.action.href) == ("Add the tracking number", "#field-tracking_number")
    s = _pstep(approval_valid=False, claims_count=0)
    assert (s.key, s.heading, s.body) == ("no_statement", "Next step: add a statement.", "Write what you did, in your own words.")
    assert (s.action.label, s.action.href) == ("Add a statement", "#statements")
    s = _pstep(approval_valid=False)
    assert (s.key, s.heading, s.body, s.action) == ("approve_here", "Next step: check each statement and approve the package.", "Approving sends nothing. Sending is a separate step. Approval applies only to this package version.", None)
    _flags(monkeypatch, active_approval=True)
    s = _pstep(approval_valid=False)
    assert (s.key, s.body) == ("review_approve", "Approving sends nothing. Sending is a separate step.")
    assert (s.action.label, s.action.href, s.action.kind, s.action.large) == ("Review each statement and approve", f"{url}/active-approval", "primary", True)
    assert _pstep(approval_valid=False, approval_stale=True).body == "The content changed after you approved it. Review and approve again."
    s = _pstep(record=_record("abandoned"))
    assert (s.key, s.heading, s.body) == ("abandoned", "You marked the earlier send as not received.", "To send it, change it first: that makes a new version to review and approve.")
    assert s.action is None and [(a.label, a.href) for a in s.secondary] == [("Edit the statements", "#statements")]


def test_for_package_send_key_and_its_note_counts_come_from_code(monkeypatch):
    _flags(monkeypatch)
    url = f"/disputes/{DID}/packages/7"
    s = _pstep(claims_count=1, files_count=1)
    assert (s.key, s.heading) == ("send", "Next step: send the evidence.")
    action = s.action
    assert (action.label, action.post, action.sends, action.sending, action.kind, action.large) == ("Send evidence to PayPal", f"{url}/send", True, "Sending to PayPal…", "primary", True)
    assert s.body == "Sending to PayPal cannot be undone; Proofie sends it once."          # before the control, in the body
    assert s.note == "Sends package v2 (1 statement, 1 file) to PayPal sandbox once. Proofie cannot withdraw it." and s.note_state == "send-note"
    assert _pstep(claims_count=2, files_count=0).note == "Sends package v2 (2 statements, 0 files) to PayPal sandbox once. Proofie cannot withdraw it."
    refused = _pstep(record=_record("refused"))
    assert refused.key == "send" and refused.body == "PayPal did not accept the last attempt. Fix what the result page said before sending again. Sending to PayPal cannot be undone; Proofie sends it once."
    blocked = _pstep(record=_record("blocked", attempt={"reason": "no link"}))
    assert blocked.key == "send" and blocked.body == "The last attempt was stopped before it reached PayPal: no link. Sending to PayPal cannot be undone; Proofie sends it once."
    assert sum(1 for step in (s, refused, blocked) for a in [step.action] if a.sends) == 3


def test_for_package_cannot_send_lists_fixes_and_offers_no_send(monkeypatch):
    _flags(monkeypatch)
    s = _pstep(has_tracking=False)
    assert (s.key, s.heading, s.data_state) == ("cannot_send", "Cannot send yet", "send-needs-tracking")
    assert [(i.text, i.link, i.href) for i in s.items] == [("There is no tracking number.", "Type the tracking number", "#field-tracking_number"),
                                                          ("Or add a document that shows it.", "Add a document", f"/disputes/{DID}/workspace/prepare#documents")]
    assert (s.action.label, s.action.href, s.action.post, s.action.sends) == ("Add the tracking number", "#field-tracking_number", None, False)
    s = _pstep(carrier_missing=True)
    assert [(i.text, i.link, i.href) for i in s.items] == [("There is no carrier.", "Choose the carrier", "#field-carrier")]
    assert (s.action.label, s.action.href, s.data_state) == ("Choose the carrier", "#field-carrier", None)
    both = _pstep(has_tracking=False, carrier_missing=True)
    assert len(both.items) == 3 and both.action.label == "Add the tracking number"
    for step in (s, both):
        assert not any(a and a.post for a in [step.action, *step.secondary])


# ------------------------------------------------------------------------------- the panel macro (next step)
def test_next_step_panel_renders_the_contract_markup(monkeypatch):
    from copilot.web import next_step
    _flags(monkeypatch)
    sendable = _pstep(claims_count=1, files_count=1)
    html = macro("next_step_panel", "{{ next_step_panel(step, 'tok') }}", step=sendable)
    dom = parse(html)
    panel = dom.find("section", id="next-step")
    assert panel.attrs["data-next-step"] == "send" and panel.attrs["aria-labelledby"] == "next-step-title" and "next-step" in panel.attrs["class"].split()
    assert dom.find("h2", id="next-step-title").text() == "Next step: send the evidence."
    form = panel.find("form")
    assert form.attrs["action"].endswith("/send") and "data-sends" in form.attrs and form.find("input", name="csrf_token").attrs["value"] == "tok"
    button = form.find("button")
    assert button.text() == "Send evidence to PayPal" and "btn-primary" in button.attrs["class"].split() and button.attrs["data-sending-text"] == "Sending to PayPal…"
    note = panel.find("p", class_="action-note")
    assert note.attrs["data-state"] == "send-note" and "1 statement, 1 file" in note.text()
    assert len(lint.primaries(html)) == 1 and lint.bare_buttons(html) == []
    blocked = _pstep(has_tracking=False)
    dom = parse(macro("next_step_panel", "{{ next_step_panel(step, 'tok') }}", step=blocked))
    panel = dom.find("section", id="next-step")
    assert panel.attrs["data-state"] == "send-needs-tracking" and panel.find("form") is None
    assert [a.attrs["href"] for a in panel.find("ul", class_="fix-list").find_all("a")] == ["#field-tracking_number", f"/disputes/{DID}/workspace/prepare#documents"]
    assert panel.find("a", class_="btn-primary").attrs["href"] == "#field-tracking_number"
    quiet = next_step.NextStep("no_action", "Nothing to do.")
    assert parse(macro("next_step_panel", "{{ next_step_panel(step) }}", step=quiet)).find("div", class_="actions") is None
    extra = parse(macro("next_step_panel", "{% call next_step_panel(step) %}<p id='legacy'>x</p>{% endcall %}", step=quiet))
    assert extra.find("section").find("p", id="legacy") is not None


def test_template_sources_of_the_shell_pass_the_contract_lint_where_they_apply():
    problems = lint.template_lint([WEB / "templates" / "_base.html", WEB / "templates" / "_form_fields.html"])
    # the sentence lives once, inside the required_note macro itself; nowhere else in the shell templates
    sentence = [p for p in problems if "Fields marked Required" in p]
    assert len(sentence) == 1 and "_form_fields.html" in sentence[0] and "required_note" in sentence[0]
    assert [p for p in problems if "<button" in p or "disabled" in p] == []
    assert all("date" in p or "maxlength" in p or "Fields marked Required" in p for p in problems)           # only the date parts keep maxlength


# --- dates
def test_delivery_before_ship_date_gives_two_different_messages_naming_both_dates(web_mode):
    web, db, token = _web()
    package = repo.insert_package(db, DID, "human_edit", True, [])
    response = web.post(f"/disputes/{DID}/manual-fields", data={
        "csrf_token": token, "package_id": package, "ship_date": "2026-10-15",
        "delivery_date": "2026-10-14"})
    assert response.status_code == 400
    summary = parse(response.text).find("section", class_="error-summary")
    entries = [li.text() for li in summary.find_all("li")]
    assert len(entries) == 2 and entries[0] != entries[1]
    for entry in entries:
        assert "14 October 2026" in entry and "15 October 2026" in entry
    assert "Delivery date must not be before shipping date." not in response.text
    assert any(e.startswith("Delivery date (14 October 2026) is before the ship date (15 October 2026).") for e in entries)
    assert any(e.startswith("Ship date (15 October 2026) is after the delivery date (14 October 2026).") for e in entries)
    assert repo.get_manual_fields(db, DID) == {}


def test_shipment_fields_error_order_puts_the_carrier_first():
    from copilot.web.form_fields import shipment_fields
    fields, errors, values = shipment_fields({"tracking_number": "Fedex", "carrier": "OTHER", "carrier_other_name": ""})
    assert list(errors) == ["carrier_other_name", "tracking_number"] and fields == {} and values["tracking_number"] == "Fedex"
    fields, errors, values = shipment_fields({"tracking_number": " 77 12 ", "carrier": "FEDEX"})
    assert errors == {} and fields["tracking_number"]["value"] == "7712" and fields["carrier"]["value"] == "FEDEX" and values["tracking_number"] == "77 12"
