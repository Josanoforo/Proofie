"""W1-3: the clause is shown in each check row of the package page and on the triage page while
COPILOT_FEATURE_CLAUSES is on, and nowhere while it is off. Offline: the synthetic sandbox dispute body
(pilot/fixtures/synthetic) replayed by a fake client; no network."""
import copy
from dataclasses import replace
import html
import re

import pytest
from _hosted import authenticated_client

from copilot import config
from copilot.app import create_app
from copilot.core import checklist_input
from copilot.core.checklist import STATES, load_rules
from copilot.db import repo
from copilot.web import routes_package
from copilot.web.routes_disputes import get_client
from test_uic1_copy import DID, WAITING

US = load_rules(country="US")
MX = load_rules(country="MX")
TRACKING_QUOTE = "An online and verifiable tracking number."


class Fake:
    def call(self, method, path, op, **kw):
        assert method == "GET"
        if op == "show_dispute":
            return copy.deepcopy(WAITING)
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        return {}


def _web(monkeypatch, rules, **flags):
    monkeypatch.setattr(checklist_input, "_RULES_CACHE", [rules])
    for name, on in flags.items():
        monkeypatch.setenv("COPILOT_FEATURE_" + name.upper(), "true" if on else "false")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    return authenticated_client(app, client=("127.0.0.1", 12345))


def _package_page(web):
    package = repo.insert_package(config.db_path(), DID, source="human_edit", validation_ok=False, review_flags=[])
    return web.get(f"/disputes/{DID}/packages/{package}")


def _why(text, cid):
    row = re.search(rf'<tr id="check-{cid}".*?</tr>', text, re.S)
    assert row, cid
    return re.search(r'<td data-label="Why">(.*?)</td>', row.group(0), re.S).group(1)


def test_package_page_quotes_paypal_in_the_why_cell_of_a_published_check(monkeypatch):
    r = _package_page(_web(monkeypatch, US, clauses=True))
    assert r.status_code == 200
    why = _why(r.text, "tracking_number")
    assert 'data-clause="published"' in why and '<blockquote lang="en">' in why
    assert TRACKING_QUOTE in html.unescape(why)
    assert "Establishing Proof of Shipment or Proof of Delivery" in html.unescape(why)
    assert f'href="{US.source_url}" rel="noopener"' in why
    assert "Proofie's own rule" in html.unescape(_why(r.text, "carrier"))
    assert r.context["clauses"]["tracking_number"]["kind"] == "published"


def test_package_page_shows_no_clause_with_the_flag_off(monkeypatch):
    r = _package_page(_web(monkeypatch, US, clauses=False))
    assert r.status_code == 200 and r.context["clauses"] is None
    assert "data-clause" not in r.text and TRACKING_QUOTE not in html.unescape(r.text)


def test_mx_published_checks_quote_the_spanish_page_inside_lang_es(monkeypatch):
    r = _package_page(_web(monkeypatch, MX, clauses=True))
    assert r.status_code == 200
    why = _why(r.text, "tracking_number")
    assert 'data-clause="published"' in why
    assert '<blockquote lang="es"><p>un número de rastreo en línea verificable</p></blockquote>' in html.unescape(why)
    # the Spanish section and page title in the source line are marked lang="es" whole (WCAG 3.1.2)
    assert '<span lang="es">Comprobante de entrega o comprobante de envío</span>' in html.unescape(why)
    assert '<span lang="es">Programa de Protección al Vendedor de PayPal</span>' in html.unescape(why)
    assert "Proofie's own rule" in html.unescape(_why(r.text, "carrier"))


def test_triage_page_shows_the_clause_beside_a_missing_check_and_in_the_technical_details(monkeypatch):
    web = _web(monkeypatch, US, clauses=True, triage=True)
    repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    r = web.get(f"/disputes/{DID}/triage")
    assert r.status_code == 200
    text = html.unescape(r.text)
    missing = text[text.index("<h2>What is missing or cannot be verified</h2>"):text.index("<h2>PayPal's request")]
    assert TRACKING_QUOTE in missing and 'data-clause="published"' in missing
    details = text[text.index("<summary>Technical details</summary>"):]
    assert "Proofie's own rule" in details and TRACKING_QUOTE in details


def test_triage_page_shows_no_clause_with_the_flag_off(monkeypatch):
    web = _web(monkeypatch, US, clauses=False, triage=True)
    repo.insert_package(config.db_path(), DID, "human_edit", True, [])
    r = web.get(f"/disputes/{DID}/triage")
    assert r.status_code == 200 and r.context["verdict"].clauses is None
    assert "data-clause" not in r.text


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("rules", [US, MX], ids=["US", "MX"])
def test_published_clause_renders_for_every_check_state(monkeypatch, rules, state):
    """Render-contract fixture: not a claim that tracking naturally reaches every business state."""
    original = routes_package._checks_for

    def with_render_state(*args, **kwargs):
        checks, context = original(*args, **kwargs)
        assert sum(c.id == "tracking_number" for c in checks) == 1
        return [replace(c, state=state) if c.id == "tracking_number" else c for c in checks], context

    monkeypatch.setattr(routes_package, "_checks_for", with_render_state)
    response = _package_page(_web(monkeypatch, rules, clauses=True))
    assert response.status_code == 200
    rendered_check = next(c for c in response.context["checks"] if c.id == "tracking_number")
    assert rendered_check.state == state

    expected = rules.checks["tracking_number"]["clause"]
    clause = response.context["clauses"]["tracking_number"]
    assert clause["kind"] == "published" and clause["quote"] == expected["quote"]
    assert clause["quote_lang"] == expected["quote_lang"] and clause["url"] == expected["url"]
    why = _why(response.text, "tracking_number")
    plain = html.unescape(re.sub(r"<[^>]+>", "", why))
    assert 'data-clause="published"' in why
    assert f'<blockquote lang="{expected["quote_lang"]}">' in why
    assert expected["quote"] in plain
    assert clause["citation_text"] in plain
    for field in ("page_title", "page_date", "section", "read_date"):
        assert expected[field] in plain
    assert f'href="{expected["url"]}" rel="noopener"' in why
