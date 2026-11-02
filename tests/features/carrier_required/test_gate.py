"""Carrier gate contexts moved from an earlier test module; the original assertions are retained."""
import sqlite3
import pytest
from pathlib import Path
from copilot.app import create_app
from copilot.db import repo
from _hosted import set_test_csrf
from .fixture_gate import DID, MESSAGE, prepared, web, rows, choices

def test_missing_carrier_route_rejects_before_note_approval_receipt_passive(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'false')
    client = web(fake)
    set_test_csrf(client, 'DEMO-CSRF')
    data = {'csrf_token': 'DEMO-CSRF', 'conflict_note': 'Synthetic note must not persist.', 'sufficiency_override': 'Reviewed.'}
    before = rows(db)
    url = f'/disputes/{DID}/packages/{pid}/' + 'approve'
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 400 and MESSAGE in response.text
    assert rows(db) == before
    assert fake.calls and all((m == 'GET' for m, _, _ in fake.calls))


def test_named_carrier_approval_and_csrf_preserved_passive(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    with sqlite3.connect(db) as c:
        c.execute('UPDATE documents SET extracted_text=extracted_text || ? WHERE id=?', ('\nCarrier: FedEx', doc))
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'false')
    client = web(fake)
    set_test_csrf(client, 'DEMO-CSRF')
    data = {'csrf_token': 'DEMO-CSRF'}
    url = f'/disputes/{DID}/packages/{pid}/' + 'approve'
    before = rows(db)
    assert client.post(url, data=data | {'csrf_token': 'bad'}, follow_redirects=False).status_code == 403
    assert rows(db) == before
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert repo.get_latest_valid_approval(db, pid)


@pytest.mark.parametrize('carrier,missing', [(None, True), ('', True), (' \t ', True), ('MEX_ESTAFETA', False), ('DOMINO', False), ('DHL', False), ('OTHER', False), ('99minutos', False)])
def test_pure_missing_characterization(carrier, missing):
    from copilot.features.carrier_required.service import carrier_missing
    assert carrier_missing(carrier) is missing


def test_flag_off_keeps_no_carrier_approval(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    monkeypatch.delenv('COPILOT_FEATURE_CARRIER_REQUIRED')
    client = web(fake)
    set_test_csrf(client, 'DEMO-CSRF')
    result = client.post(f'/disputes/{DID}/packages/{pid}/approve', data={'csrf_token': 'DEMO-CSRF'}, follow_redirects=False)
    assert result.status_code == 303 and repo.get_latest_valid_approval(db, pid)


def test_missing_carrier_csrf_still_precedes_gate_passive(prepared, monkeypatch):
    db, pid, doc, fake = prepared
    monkeypatch.setenv('COPILOT_FEATURE_ACTIVE_APPROVAL', 'false')
    client = web(fake)
    set_test_csrf(client, 'DEMO-CSRF')
    response = client.post(f'/disputes/{DID}/packages/{pid}/' + 'approve', data={'csrf_token': 'bad'}, follow_redirects=False)
    assert response.status_code == 403 and MESSAGE not in response.text
    assert fake.calls == [] and rows(db)['approvals'] == []


def test_standalone_carrier_declares_no_active_route(prepared):
    import json
    db, pid, doc, fake = prepared
    app = create_app()
    assert not any("/active-approval" in path for path in app.openapi()["paths"])
    assert web(fake).get(f"/disputes/{DID}/packages/{pid}/active-approval").status_code == 404
    root = Path(__file__).resolve().parents[3]
    manifest = root / "MANIFEST.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        entries = data["files"]
        paths = [entry["path"] for entry in entries]
        present = any(path.startswith("copilot/features/active_approval/") for path in paths)
        assert present == (root / "copilot/features/active_approval").is_dir()
        if not present:
            import sys
            assert not any(name == "copilot.features.active_approval" or
                           name.startswith("copilot.features.active_approval.") for name in sys.modules)
    assert all(method == "GET" for method, _, _ in fake.calls)


@pytest.mark.parametrize("has_tracking", [False, True], ids=["no-tracking", "tracking"])
@pytest.mark.parametrize("gate_enabled", [False, True], ids=["flag-off", "flag-on"])
def test_gate_only_reports_carrier_when_tracking_exists_passive(prepared, monkeypatch, has_tracking, gate_enabled):
    active = False
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
