"""POST /disputes/{id}/packages/{pid}/claims/{cid}/spanish: the seller types the Spanish version of one statement. The router is
included here the way copilot/app.py will include it behind COPILOT_FEATURE_BILINGUAL (an integration edit of this lane)."""
import json
import sqlite3
from pathlib import Path

import pytest

from _hosted import authenticated_client, configure_hosted, set_test_csrf

from copilot import config
from copilot.app import create_app
from copilot.features.bilingual import routes, service, store
from copilot.web.routes_disputes import get_client

from .conftest import DISPUTE_ID, ES_1, FLAG, claim_ids, make_package

TOKEN = "test-csrf-token"
REC = Path(__file__).resolve().parents[3] / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]


class _ReadOnlyPayPal:
    """Answers only the reads the package page makes when it is drawn again with a field error; a save itself reads
    nothing. Any write, or any other call, fails the test."""
    def __init__(self):
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append(op)
        assert method == "GET", f"PayPal write attempted: {op}"
        if op == "show_dispute":
            return WAITING
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _web(monkeypatch):
    configure_hosted(monkeypatch)
    app = create_app()
    app.include_router(routes.router)
    fake = _ReadOnlyPayPal()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app)
    set_test_csrf(web, TOKEN)
    web.fake = fake
    return web


def _url(package_id, claim_id, dispute_id=DISPUTE_ID):
    return f"/disputes/{dispute_id}/packages/{package_id}/claims/{claim_id}/spanish"


def _rows():
    with sqlite3.connect(str(config.db_path())) as conn:
        try:
            return conn.execute(f"SELECT claim_id, es, source FROM {store.TABLE} ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return []


def test_saving_redirects_to_the_row_with_the_saved_notice(mx_on, monkeypatch, web_mode):
    package_id = make_package()
    first = claim_ids(package_id)[0]
    web = _web(monkeypatch)
    response = web.post(_url(package_id, first), data={"csrf_token": TOKEN, "text": ES_1}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/disputes/{DISPUTE_ID}/packages/{package_id}?es_saved={first}#es-claim-{first}"
    assert _rows() == [(first, ES_1, "seller")]
    assert web.fake.calls == []
    assert service.view(config.db_path(), package_id)["rows"][0]["es_state"] == "seller"


def test_empty_text_answers_400_and_stores_nothing(mx_on, monkeypatch):
    package_id = make_package()
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(package_id)[0]), data={"csrf_token": TOKEN, "text": "  "},
                        follow_redirects=False)
    assert response.status_code == 400
    assert service.EMPTY_MESSAGE in response.text
    assert _rows() == []


def test_a_bad_csrf_token_stores_nothing(mx_on, monkeypatch):
    package_id = make_package()
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(package_id)[0]), data={"csrf_token": "wrong", "text": ES_1},
                        follow_redirects=False)
    assert response.status_code in (400, 403)
    assert _rows() == []


def test_a_statement_of_another_package_is_404(mx_on, monkeypatch):
    package_id = make_package()
    other = make_package()
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(other)[0]), data={"csrf_token": TOKEN, "text": ES_1},
                        follow_redirects=False)
    assert response.status_code == 404
    assert _rows() == []


def test_a_package_of_another_dispute_is_404(mx_on, monkeypatch):
    package_id = make_package(dispute_id="PP-R-DEMO-0000002")
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(package_id)[0]), data={"csrf_token": TOKEN, "text": ES_1},
                        follow_redirects=False)
    assert response.status_code == 404
    assert _rows() == []


def test_a_bad_dispute_id_is_refused(mx_on, monkeypatch):
    package_id = make_package()
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(package_id)[0], dispute_id="bad id!"),
                        data={"csrf_token": TOKEN, "text": ES_1}, follow_redirects=False)
    assert response.status_code in (400, 404)
    assert _rows() == []


def test_a_sent_version_is_409(mx_on, monkeypatch):
    package_id = make_package()
    monkeypatch.setattr(routes, "_is_sent_version", lambda db, dispute_id, package: True)
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(package_id)[0]), data={"csrf_token": TOKEN, "text": ES_1},
                        follow_redirects=False)
    assert response.status_code == 409
    assert _rows() == []


@pytest.mark.parametrize("country", ["US", ""])
def test_outside_mx_the_route_is_404(monkeypatch, country):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", country)
    package_id = make_package()
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(package_id)[0]), data={"csrf_token": TOKEN, "text": ES_1},
                        follow_redirects=False)
    assert response.status_code == 404
    assert _rows() == []


def test_flag_off_the_route_is_404(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setattr(config, "RULES_COUNTRY", "MX")
    package_id = make_package()
    web = _web(monkeypatch)
    response = web.post(_url(package_id, claim_ids(package_id)[0]), data={"csrf_token": TOKEN, "text": ES_1},
                        follow_redirects=False)
    assert response.status_code == 404
    assert _rows() == []
