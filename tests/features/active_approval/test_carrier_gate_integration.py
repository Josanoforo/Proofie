"""Carrier gate contexts moved from an earlier test module; the original assertions are retained."""
import sqlite3
import pytest
from pathlib import Path
from copilot.app import create_app
from copilot.db import repo
from _hosted import set_test_csrf
from carrier_required.fixture_gate import DID, MESSAGE, prepared, web, rows, choices
from copilot.features.active_approval import service
from copilot.web.routes_disputes import get_client

def test_missing_carrier_route_rejects_before_note_approval_receipt_active(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'true')
    client = web(fake)
    set_test_csrf(client, 'DEMO-CSRF')
    data = {'csrf_token': 'DEMO-CSRF', 'conflict_note': 'Synthetic note must not persist.', 'sufficiency_override': 'Reviewed.'}
    review = service.read(fake, db, DID, pid)
    data.update(review_sha256=review.review_sha256)
    data.update({'claim-' + k: v for k, v in choices(review).items()})
    before = rows(db)
    url = f'/disputes/{DID}/packages/{pid}/' + 'active-approval'
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 400 and MESSAGE in response.text
    assert rows(db) == before
    assert fake.calls and all((m == 'GET' for m, _, _ in fake.calls))


def test_missing_carrier_direct_active_service_cannot_persist(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'true')
    review = service.read(fake, db, DID, pid)
    before = rows(db)
    with pytest.raises(service.ReviewError, match=MESSAGE):
        service.approve(fake, db, DID, pid, review.review_sha256, choices(review), '', 'Reviewed.')
    assert rows(db) == before


def test_named_carrier_approval_and_csrf_preserved_active(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    with sqlite3.connect(db) as c:
        c.execute('UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?', ('\nCarrier: FedEx', doc))
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'true')
    client = web(fake)
    set_test_csrf(client, 'DEMO-CSRF')
    data = {'csrf_token': 'DEMO-CSRF'}
    review = service.read(fake, db, DID, pid)
    data.update(review_sha256=review.review_sha256)
    data.update({'claim-' + k: v for k, v in choices(review).items()})
    url = f'/disputes/{DID}/packages/{pid}/' + 'active-approval'
    before = rows(db)
    assert client.post(url, data=data | {'csrf_token': 'bad'}, follow_redirects=False).status_code == 403
    assert rows(db) == before
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert repo.get_latest_valid_approval(db, pid)


@pytest.mark.hosted_spec
def test_hosted_unauthenticated_is_outermost(prepared, monkeypatch):
    from fastapi.testclient import TestClient
    db, pid, doc, fake = prepared
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'true')
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    client = TestClient(app, base_url='https://testserver', client=('127.0.0.1', 1234))
    response = client.post(f'/disputes/{DID}/packages/{pid}/active-approval', data={}, follow_redirects=False)
    assert response.status_code == 401
    assert fake.calls == [] and rows(db)['approvals'] == []


def test_missing_carrier_csrf_still_precedes_gate_active(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'true')
    client = web(fake)
    set_test_csrf(client, 'DEMO-CSRF')
    response = client.post(f'/disputes/{DID}/packages/{pid}/' + 'active-approval', data={'csrf_token': 'bad'}, follow_redirects=False)
    assert response.status_code == 403 and MESSAGE not in response.text
    assert fake.calls == [] and rows(db)['approvals'] == []



@pytest.mark.parametrize("has_tracking", [False, True], ids=["no-tracking", "tracking"])
@pytest.mark.parametrize("gate_enabled", [False, True], ids=["flag-off", "flag-on"])
def test_gate_only_reports_carrier_when_tracking_exists_active(prepared, monkeypatch, has_tracking, gate_enabled):
    active = True
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import build_request_input, pick_tracking

    db, pid, doc, fake = prepared
    if not has_tracking:
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE documents SET extracted_text=? WHERE id=?",
                               ("DEMO DATA\nStatus: En transit", doc))
    monkeypatch.setenv("COPILOT_FEATURE_CARRIER_REQUIRED", "true" if gate_enabled else "false")
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true" if active else "false")
    tracking, carrier = pick_tracking(extracted_docs_for(db, DID))
    assert bool(tracking) is has_tracking
    if not has_tracking:
        with pytest.raises(ValueError, match="no tracking number available yet"):
            build_request_input(repo.list_claims(db, pid), tracking, carrier)
    client = web(fake)
    set_test_csrf(client, "DEMO-CSRF")
    data = {"csrf_token": "DEMO-CSRF", "sufficiency_override": "Reviewed."}
    if active:
        review = service.read(fake, db, DID, pid)
        assert (MESSAGE in review.blocked) is (gate_enabled and has_tracking)
        data.update(review_sha256=review.review_sha256)
        data.update({"claim-" + key: value for key, value in choices(review).items()})
    before = rows(db)
    endpoint = "active-approval" if active else "approve"
    response = client.post(f"/disputes/{DID}/packages/{pid}/{endpoint}", data=data,
                           follow_redirects=False)
    if gate_enabled and has_tracking:
        assert response.status_code == 400 and MESSAGE in response.text
        assert rows(db) == before
    else:
        assert response.status_code == 303 and MESSAGE not in response.text
        approval = repo.get_latest_valid_approval(db, pid)
        assert approval
        if not has_tracking:
            assert approval["approval_sha256"] == f"not-sendable-yet-{pid}"
    assert fake.calls and all(method == "GET" for method, _, _ in fake.calls)
