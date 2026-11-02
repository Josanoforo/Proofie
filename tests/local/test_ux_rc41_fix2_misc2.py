"""CSS regressions of the first pass, error pages, Spanish phrase, exact-content tense, seed wording.

Offline only: fake PayPal clients, a temp database per test (conftest.py). The CSS facts are also measured in a real
browser; the tests here pin the rules that produce those numbers.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")


def _rules(selector_regex):
    return [(m.group(1), m.group(2)) for m in re.finditer(r"(?m)^(" + selector_regex + r")\s*\{([^{}]*)\}", CSS, re.S)]


def _all_rules():
    """(selector list, declarations) of every rule, media wrappers ignored."""
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}@/][^{}]*)\{([^{}]*)\}", CSS)]


def _declares(selector_fragment, declaration):
    return [sel for sel, body in _all_rules() if selector_fragment in sel and declaration in body]


# ---- th and td do not break anywhere only long tokens ------
def test_mcs_1_th_and_td_do_not_break_anywhere_only_long_tokens_do():
    for sel, body in _all_rules():
        parts = [p.strip() for p in sel.split(",")]
        assert not ({"th", "td", "th, td"} & set(parts) and "overflow-wrap: anywhere" in body), sel
    for token in (".mono-id", "td code", 'td[data-label="File"]'):
        assert _declares(token, "overflow-wrap: anywhere"), token


def test_mcs_1_the_large_button_keeps_its_min_width_in_a_wide_cell():
    cell = [(sel, body) for sel, body in _all_rules() if "td .btn-large" in sel]
    assert cell, "td .btn-large rules exist"
    # the base rule (no media query) must not zero the min-width; only the phone block does
    base = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", CSS)
    for m in re.finditer(r"([^{}]*td \.btn-large[^{}]*)\{([^{}]*)\}", base):
        assert "min-width" not in m.group(2), m.group(0)
    phone = re.search(r"@media \(max-width: 1024px\) \{ td \.btn-large[^{}]*\{ min-width: 0; \} \}", CSS)
    assert phone, "up to 1024px (fix3 MC2-1) a large button in a cell may shrink"


# ---- the stacked caption rule skips the visually hidden caption ---
def test_mcs_2_the_stacked_caption_rule_skips_the_visually_hidden_caption():
    assert "table.stack caption:not(.visually-hidden)" in CSS
    assert not re.search(r"table\.stack caption\s*\{", CSS)                     # no unscoped rule is left
    assert not re.search(r"table\.stack caption,", CSS)


# ---- anywhere is scoped to the triage page and the required mark stays ---
def test_mcs_3_anywhere_is_scoped_to_the_triage_page_and_the_required_mark_stays_whole():
    for sel, body in _all_rules():
        if "overflow-wrap: anywhere" in body:
            assert not re.search(r"(^|,)\s*main (li|dd|small)\b", sel), sel
    assert _declares("#triage li", "overflow-wrap: anywhere")
    assert _declares(".field-mark", "white-space: nowrap")


# ---- the webhooks table is wider than a phone exactly where the hint ---
def test_mcs_4_the_webhooks_table_is_wider_than_a_phone_exactly_where_the_hint_shows():
    hint = re.search(r"@media \(max-width: (\d+)px\) \{ \.table-scroll-hint \{ display: block; \} \}", CSS)
    table = re.search(r"@media \(max-width: (\d+)px\) \{ table\.webhook-events \{ min-width: ([\d.]+)rem; \} \}", CSS)
    assert hint and table and hint.group(1) == table.group(1) == "639"
    assert float(table.group(2)) * 16 > 639                                        # wider than any screen the hint shows on


# ---- a web client helper ----------------------------------------------
def _web_with(client):
    import secrets
    from _hosted import authenticated_client, set_test_csrf
    from copilot.app import create_app
    from copilot.web.routes_disputes import get_client
    app = create_app()
    app.dependency_overrides[get_client] = lambda: client
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, secrets.token_urlsafe(32))
    return web


def test_mcs_7_the_not_found_dispute_page_ends_the_trail_at_all_disputes_and_the_error_title():
    import _ux_lint as lint
    from copilot.core.disputes import PayPalError
    from test_ux_rc41_shell import DID, FakeClient

    class Missing(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "show_dispute":
                raise PayPalError(404, "RESOURCE_NOT_FOUND", "dbg-1", {})
            return super().call(method, path, op, **kw)

    web = _web_with(Missing())
    page = web.get(f"/disputes/{DID}")
    assert page.status_code == 404
    assert lint.breadcrumb_items(page.text) == [("All disputes", "/disputes"), ("That dispute was not found", None)]
    assert f'href="/disputes/{DID}"' in page.text                                  # Try again still points at the address
    # every other error page keeps the dispute crumb: a bad session on a package route names its dispute
    other = web.post(f"/disputes/{DID}/documents", data={"csrf_token": "wrong"})
    assert [t for t, _h in lint.breadcrumb_items(other.text)] == ["All disputes", f"Dispute {DID}", "Your session expired"]


def test_mcs_8_a_get_to_a_post_only_address_renders_the_error_page_and_keeps_the_allow_header():
    import _ux_lint as lint
    from _dom import parse
    from test_ux_rc41_shell import DID, FakeClient
    web = _web_with(FakeClient())
    for path in (f"/disputes/{DID}/packages/1/approve", f"/disputes/{DID}/resolution/preview"):
        response = web.get(path)
        assert response.status_code in (405, 404), path
        if response.status_code == 404:                                    # resolution is a flag: only a routed POST address counts
            continue
        assert response.headers["content-type"].startswith("text/html"), path
        assert response.headers["allow"] == "POST", path
        dom = parse(response.text)
        assert dom.find("h1").text().strip() == "This address does not open as a page"
        assert "Nothing was changed" in response.text and "detail" not in response.text.lower().replace("details", "")
        assert "Try again" not in response.text
        assert [a for a in dom.find_all("a") if a.attrs.get("href") == "/disputes"], "header link to All disputes"
        assert lint.breadcrumb_items(response.text) == [("All disputes", "/disputes"), (f"Dispute {DID}", f"/disputes/{DID}"),
                                                         ("This address does not open as a page", None)]
    approve = web.get(f"/disputes/{DID}/packages/1/approve")
    assert approve.status_code == 405                                      # the package routes are always on
    # not a page: the static files keep the plain answer, and a deliberate route detail is untouched
    assert "text/html" not in web.post("/static/app.css").headers.get("content-type", "")
    assert web.post("/static/app.css").status_code == 405


# ---- a spanish phrase in a delivery block sentence is marked as spanish ---
def test_mu_12_a_spanish_phrase_in_a_delivery_block_sentence_is_marked_as_spanish():
    from html import unescape
    from markupsafe import Markup
    from copilot.web import presentation
    from copilot.web.routes_disputes import templates as dispute_templates
    from copilot.web.routes_package import templates as package_templates
    for phrase in ("entregado", "Entregada", "firmado", "recibida"):
        for kind, filename in (("unsupported", None), ("unreadable", "a<b>.pdf")):
            message = presentation.delivery_block_message(1, phrase, kind, filename)
            assert isinstance(message, Markup), (phrase, kind)
            assert f"“<span lang=\"es\" translate=\"no\">{phrase}</span>”" in message
            assert "<b>" not in message                                       # the file name is escaped, not trusted
            for env in (dispute_templates.env, package_templates.env):
                rendered = env.from_string("<li>{{ i }}</li>").render(i=message)
                assert rendered.count('lang="es"') == 1 and "&lt;span" not in rendered
    english = presentation.delivery_block_message(1, "delivered", "unsupported")
    assert not isinstance(english, Markup) and "lang=" not in english         # an English phrase stays plain text
    assert presentation.delivery_block_message(1, "entregado", "conflict") == presentation.delivery_block_message(1, "x", "conflict")


def test_mu_12_the_blocked_approval_list_and_the_package_refusal_page_show_the_marked_phrase():
    from copilot.web.routes_package import _delivery_block_items
    items = _delivery_block_items(
        [{"text": "Entregado el lunes", "source_ids": []}],
        [{"text": "Entregado el lunes", "matched_phrase": "Entregado", "label": "unsupported"}], [], [], [])
    assert len(items) == 1 and 'lang="es"' in str(items[0])
    from copilot.web.routes_disputes import templates
    rendered = templates.env.from_string("{% for i in items %}<li>{{ i }}</li>{% endfor %}").render(items=items)
    assert '<span lang="es" translate="no">Entregado</span>' in rendered


# ---- the exact content note reads in the right tense -------
def test_mu_3_the_exact_content_note_reads_in_the_right_tense():
    from test_ux_rc41_shell import macro
    from _dom import parse
    will = macro("exact_content", "{% call exact_content() %}{{ text }}{% endcall %}", text="Line one")
    was = macro("exact_content", "{% call exact_content(sent=true) %}{{ text }}{% endcall %}", text="Line one")
    assert parse(will).find("p", class_="exact-note").text() == "Shown exactly as PayPal will receive it. Your browser will not translate this part."
    assert parse(was).find("p", class_="exact-note").text() == "Shown exactly as PayPal received it. Your browser will not translate this part."
    for html in (will, was):
        dom = parse(html)
        assert dom.find(None, data_exact_content="") is not None and dom.find(None, data_exact_content="").attrs.get("translate") == "no"
