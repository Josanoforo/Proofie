"""End to end through create_app: the package page with lane PK's include hook, the Spanish save route registered behind the
flag by copilot/app.py, the model route, and the approval that covers the Spanish text."""
import re
import sqlite3
from html import unescape

import pytest

from _hosted import authenticated_client, configure_hosted, set_test_csrf
from _lang import spanish_outside_lang

from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.features.bilingual import routes, service, store
from copilot.web.routes_disputes import get_client

from .conftest import DISPUTE_ID, EN_1, EN_2, ES_1, ES_2, FLAG, FakeModel, ReadOnlyPayPal, claim_ids, make_package

TOKEN = "test-csrf-token"


def _web(monkeypatch, call=None):
    configure_hosted(monkeypatch)
    app = create_app()
    fake = ReadOnlyPayPal()
    app.dependency_overrides[get_client] = lambda: fake
    if call is not None:
        app.dependency_overrides[routes.get_model_call] = lambda: call
    web = authenticated_client(app)
    set_test_csrf(web, TOKEN)
    return web


def _page(web, package_id):
    response = web.get(f"/disputes/{DISPUTE_ID}/packages/{package_id}")
    assert response.status_code == 200, response.text[:400]
    set_test_csrf(web, TOKEN)        # the page set its own token; the next POST uses the test's
    return response.text


def _rows():
    with sqlite3.connect(str(config.db_path())) as conn:
        try:
            return conn.execute(f"SELECT claim_id, es, source FROM {store.TABLE} ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return []


def test_flag_on_mx_the_package_page_shows_the_spanish_section(mx_on, monkeypatch, web_mode):
    package_id = make_package()
    html = _page(_web(monkeypatch), package_id)
    first, second = claim_ids(package_id)
    assert 'id="statements-es"' in html
    assert f'<textarea id="field-es-claim-{first}" name="text" rows="3" lang="es"' in html
    assert html.index('id="statements"') < html.index('id="statements-es"') < html.index('id="checks"')
    assert spanish_outside_lang(html) == []


def test_flag_off_or_us_the_package_page_has_no_spanish_section(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    package_id = make_package()
    assert 'statements-es' not in _page(_web(monkeypatch), package_id)
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    assert 'statements-es' not in _page(_web(monkeypatch), package_id)


def test_an_empty_save_links_the_error_summary_to_the_field(mx_on, monkeypatch):
    package_id = make_package()
    first = claim_ids(package_id)[0]
    web = _web(monkeypatch)
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/claims/{first}/spanish",
                        data={"csrf_token": TOKEN, "text": " "}, follow_redirects=False)
    assert response.status_code == 400
    html = response.text
    assert f'href="#field-es-claim-{first}"' in html
    assert len(re.findall(rf'id="field-es-claim-{first}"', html)) == 1
    assert service.EMPTY_MESSAGE in html and f'id="error-es-claim-{first}"' in html
    assert _rows() == []


def test_a_save_lands_on_its_row_with_the_saved_notice(mx_on, monkeypatch):
    package_id = make_package()
    first = claim_ids(package_id)[0]
    web = _web(monkeypatch)
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/claims/{first}/spanish",
                        data={"csrf_token": TOKEN, "text": ES_1}, follow_redirects=True)
    assert response.status_code == 200
    html = response.text
    assert 'data-state="es-saved"' in html and "Written by you" in html
    assert ES_1 in unescape(html)


def test_saving_spanish_clears_a_valid_approval(mx_on, monkeypatch):
    db = config.db_path()
    package_id = make_package()
    repo.insert_approval(db, package_id, "0" * 64, "vendor")
    assert repo.get_latest_valid_approval(db, package_id)
    web = _web(monkeypatch)
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/claims/{claim_ids(package_id)[0]}/spanish",
                        data={"csrf_token": TOKEN, "text": ES_1}, follow_redirects=False)
    assert response.status_code == 303 and "es_cleared=1" in response.headers["location"]
    assert repo.get_latest_valid_approval(db, package_id) is None
    assert any(a["action"] == "invalidate_approval_after_edit" for a in repo.list_audit(db, DISPUTE_ID))


def test_the_model_route_fills_the_missing_rows_locally(mx_on, monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    package_id = make_package()
    call = FakeModel({EN_1: ES_1, EN_2: ES_2})
    web = _web(monkeypatch, call=call)
    html = _page(web, package_id)
    assert "Ask the AI model for a Spanish draft" in html
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/spanish/model", data={"csrf_token": TOKEN},
                        follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (f"/disputes/{DISPUTE_ID}/packages/{package_id}"
                                            "?es_model=done&made=2&refused=0#statements-es")
    assert [r[2] for r in _rows()] == ["ai", "ai"]
    html = web.get(response.headers["location"]).text
    assert html.count("Written by the AI model: check every word") == 2
    assert 'data-state="es-model-done"' in html
    assert "Ask the AI model for a Spanish draft" not in html     # nothing is missing any more


def test_the_model_route_reports_an_unavailable_model(mx_on, monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    package_id = make_package()
    web = _web(monkeypatch, call=FakeModel(raises=RuntimeError("busy")))
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/spanish/model", data={"csrf_token": TOKEN},
                        follow_redirects=True)
    assert 'data-state="es-model-unavailable"' in response.text and _rows() == []


@pytest.mark.hosted_spec
def test_the_hosted_demo_offers_no_model_and_the_route_calls_none(mx_on, monkeypatch, web_mode):
    package_id = make_package()
    call = FakeModel({EN_1: ES_1})
    web = _web(monkeypatch, call=call)
    html = _page(web, package_id)
    assert "Ask the AI model for a Spanish draft" not in html
    assert "the AI assistant is switched off" in html
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/spanish/model", data={"csrf_token": TOKEN},
                        follow_redirects=False)
    assert response.status_code == 303 and "es_model=off" in response.headers["location"]
    assert call.calls == [] and _rows() == []


def test_the_model_route_needs_csrf(mx_on, monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    package_id = make_package()
    call = FakeModel({EN_1: ES_1})
    web = _web(monkeypatch, call=call)
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/spanish/model", data={"csrf_token": "bad"},
                        follow_redirects=False)
    assert response.status_code in (400, 403) and call.calls == [] and _rows() == []


def test_the_model_route_is_404_outside_mx(monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    package_id = make_package()
    call = FakeModel({EN_1: ES_1})
    web = _web(monkeypatch, call=call)
    response = web.post(f"/disputes/{DISPUTE_ID}/packages/{package_id}/spanish/model", data={"csrf_token": TOKEN},
                        follow_redirects=False)
    assert response.status_code == 404 and call.calls == []
