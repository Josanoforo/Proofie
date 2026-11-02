"""D-101 seller item 4: the carrier, the tracking number and a statement are proposed from what the uploaded document states
(COPILOT_FEATURE_SELLER_SHORTCUTS). Read by code; shown as a proposal in the form's inputs; Save and Add stay the writes."""
import re
import html as html_module

import pytest
from _hosted import reset_csrf, set_test_csrf

from copilot import config
from copilot.db import repo

from test_fix1_visible_failures import DID, ROOT, FakeClient, _app, _client

FLAG = "COPILOT_FEATURE_SELLER_SHORTCUTS"
IN_TRANSIT = "03_in_transit.pdf"
COHERENT = "01_coherent_delivery.pdf"


class GetOnly(FakeClient):
    def call(self, method, path, op, **kw):
        assert method == "GET", "reading a proposal cannot write to PayPal"
        return super().call(method, path, op, **kw)


def _upload(c, token, name):
    data = (ROOT / "demo" / "documents" / name).read_bytes()
    r = c.post(f"/disputes/{DID}/documents", files={"file": (name, data, "application/pdf")},
               data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303


def _package(monkeypatch, *documents, flag="true"):
    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    c, token = _client(_app(GetOnly()))
    for name in documents:
        _upload(c, token, name)
    package = repo.insert_package(config.db_path(), DID, source="human_edit", validation_ok=False, review_flags=[])
    return c, package


def _page(c, package):
    r = c.get(f"/disputes/{DID}/packages/{package}")
    assert r.status_code == 200
    return r.text


def _input(page, name):
    tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    value = re.search(r'value="([^"]*)"', tag.group(0)) if tag else None
    return html_module.unescape(value.group(1)) if value else ""


def _statement(page):
    found = re.search(r'<textarea id="field-new-claim"[^>]*>(.*?)</textarea>', page, re.S)
    return html_module.unescape(found.group(1).strip()) if found else None


def test_the_form_comes_with_what_the_document_states_and_says_where_it_is_from(monkeypatch):
    c, package = _package(monkeypatch, IN_TRANSIT)
    page = _page(c, package)
    assert _input(page, "tracking_number") == "DEMO-EST-000005"
    assert re.search(r'<option value="MEX_ESTAFETA" selected', page) or "Estafeta" in page
    assert "I shipped this parcel with Estafeta; the tracking number is DEMO-EST-000005." == _statement(page)
    assert 'data-state="document-proposal"' in page and IN_TRANSIT in page and 'data-state="statement-proposal"' in page


def test_reading_a_proposal_writes_nothing(monkeypatch):
    c, package = _package(monkeypatch, IN_TRANSIT)
    _page(c, package)
    assert repo.get_manual_fields(config.db_path(), DID) == {} and repo.list_claims(config.db_path(), package) == []


def test_without_the_setting_the_form_is_empty_as_before(monkeypatch):
    c, package = _package(monkeypatch, IN_TRANSIT, flag=None)
    page = _page(c, package)
    assert _input(page, "tracking_number") == "" and (_statement(page) or "") == ""
    assert 'data-state="document-proposal"' not in page


def test_the_seller_saving_the_proposal_stores_it_as_the_seller_entry(monkeypatch):
    c, package = _package(monkeypatch, IN_TRANSIT)
    page = _page(c, package)
    token = "seller-shortcuts-token"
    reset_csrf(c)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": str(package),
                                                       "tracking_number": _input(page, "tracking_number"), "carrier": "MEX_ESTAFETA"},
               follow_redirects=False)
    assert r.status_code in (200, 303)
    stored = repo.get_manual_fields(config.db_path(), DID)
    assert stored["tracking_number"]["value"] == "DEMO-EST-000005"


def test_nothing_is_proposed_once_a_tracking_number_is_saved(monkeypatch):
    c, package = _package(monkeypatch, IN_TRANSIT)
    repo.set_manual_fields(config.db_path(), DID, {"tracking_number": {"value": "MY-OWN-1234567"}})
    page = _page(c, package)
    assert 'data-state="document-proposal"' not in page and _input(page, "tracking_number") == "MY-OWN-1234567"


def test_two_documents_with_different_numbers_propose_nothing(monkeypatch):
    c, package = _package(monkeypatch, IN_TRANSIT, COHERENT)
    page = _page(c, package)
    assert 'data-state="document-proposal"' not in page and _input(page, "tracking_number") == ""
    assert 'data-state="statement-proposal"' not in page and (_statement(page) or "") == ""
    # the typed path stays intact: the seller types the number and it is stored as the seller's own entry
    token = "seller-shortcuts-token"
    reset_csrf(c)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": token, "package_id": str(package),
                                                       "tracking_number": "TYPED-BY-SELLER-77", "carrier": "MEX_ESTAFETA"},
               follow_redirects=False)
    assert r.status_code in (200, 303)
    assert repo.get_manual_fields(config.db_path(), DID)["tracking_number"]["value"] == "TYPED-BY-SELLER-77"


def test_no_document_proposes_nothing(monkeypatch):
    c, package = _package(monkeypatch)
    page = _page(c, package)
    assert 'data-state="document-proposal"' not in page


def test_the_statement_is_not_proposed_when_the_package_already_has_one(monkeypatch):
    c, package = _package(monkeypatch, IN_TRANSIT)
    repo.insert_claims(config.db_path(), package, [{"evidence_index": 0, "claim_index": 0, "text": "My own statement.",
                                                  "source_ids": [], "basis": "seller_statement"}])
    page = _page(c, package)
    assert 'data-state="statement-proposal"' not in page and (_statement(page) or "") == ""
    assert _input(page, "tracking_number") == "DEMO-EST-000005"
