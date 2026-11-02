"""The Spanish version sits inside the active approval's review_sha (flag on, MX): approving covers both texts, a Spanish
change clears the approval, and PayPal's request input and the send fingerprint never see the Spanish."""
import pytest

from copilot import config
from copilot.db import repo
from copilot.features.active_approval import service as approval
from copilot.features.bilingual import service, store
from copilot.web.routes_disputes import get_client

from .conftest import DISPUTE_ID, FLAG, ReadOnlyPayPal, claim_ids, make_package

EN_A = "Shipment is in transit."
EN_B = "The order was packed on 3 Oct 2026."
ES_A = "El envío está en tránsito."
ES_B = "El pedido se empacó el 3 oct 2026."


@pytest.fixture
def approval_on(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")


def _package():
    return make_package(texts=(EN_A, EN_B))


def _read(package_id):
    return approval.read(ReadOnlyPayPal(), config.db_path(), DISPUTE_ID, package_id)


def _store_spanish(package_id, texts):
    db = config.db_path()
    for cid, es in zip(claim_ids(package_id), texts):
        store.save(db, package_id, cid, service.english_sha256(
            next(c["text"] for c in repo.list_claims(db, package_id) if c["id"] == cid)), es, "seller")


def test_flag_off_the_spanish_does_not_enter_the_digest(approval_on):
    package_id = _package()
    before = _read(package_id)
    _store_spanish(package_id, (ES_A, ES_B))
    after = _read(package_id)
    assert after.review_sha256 == before.review_sha256 and after.spanish is None


def test_flag_on_outside_mx_the_digest_is_unchanged(approval_on, monkeypatch):
    package_id = _package()
    before = _read(package_id).review_sha256
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    _store_spanish(package_id, (ES_A, ES_B))
    assert _read(package_id).review_sha256 == before


def test_flag_on_mx_the_spanish_text_is_bound_into_the_digest(approval_on, mx_on):
    package_id = _package()
    missing = _read(package_id)
    assert {r["es_state"] for r in missing.spanish.values()} == {"missing"}
    _store_spanish(package_id, (ES_A, ES_B))
    written = _read(package_id)
    assert written.review_sha256 != missing.review_sha256
    assert [written.spanish[cid]["es"] for cid in claim_ids(package_id)] == [ES_A, ES_B]
    _store_spanish(package_id, (ES_A, ES_B.replace("empacó", "preparó")))
    assert _read(package_id).review_sha256 not in (written.review_sha256, missing.review_sha256)


def test_the_send_request_and_fingerprint_never_carry_the_spanish(approval_on, monkeypatch):
    package_id = _package()
    off = _read(package_id)
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "MX")
    _store_spanish(package_id, (ES_A, ES_B))
    on = _read(package_id)
    assert on.fingerprint == off.fingerprint and on.request_input == off.request_input
    assert "tránsito" not in repr(on.request_input) and "empacó" not in repr(on.request_input)


def test_an_approval_is_cleared_when_the_spanish_changes(approval_on, mx_on):
    db = config.db_path()
    package_id = _package()
    _store_spanish(package_id, (ES_A, ES_B))
    review = _read(package_id)
    choices = {c.key: ("seller_statement" if c.verdict == "unverifiable" else "confirmed") for c in review.claims}
    approval.approve(ReadOnlyPayPal(), db, DISPUTE_ID, package_id, review.review_sha256, choices, "", "")
    assert repo.get_latest_valid_approval(db, package_id)
    assert _read(package_id).approval is not None                      # unchanged content: the approval stands
    _store_spanish(package_id, (ES_A, ES_B + " Con cuidado."))
    after = _read(package_id)
    assert after.cleared is True and after.approval is None
    assert repo.get_latest_valid_approval(db, package_id) is None


def test_the_approval_page_shows_each_spanish_version_inside_lang_es(approval_on, mx_on, monkeypatch):
    from _hosted import authenticated_client, configure_hosted
    from _lang import spanish_outside_lang
    from copilot.app import create_app
    package_id = _package()
    _store_spanish(package_id, (ES_A,))
    configure_hosted(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: ReadOnlyPayPal()
    html = authenticated_client(app).get(f"/disputes/{DISPUTE_ID}/packages/{package_id}/active-approval").text
    first, second = claim_ids(package_id)
    assert f'<span lang="es">{ES_A}</span>' in html
    assert 'data-es-state="seller"' in html and 'data-es-state="missing"' in html
    assert f'href="/disputes/{DISPUTE_ID}/packages/{package_id}#es-claim-{second}"' in html
    assert "not sent to PayPal" in html
    assert spanish_outside_lang(html) == []
