from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from starlette.requests import Request

from copilot import config
from copilot.db import repo
from copilot.db.schema import ensure_schema
from copilot.features.fulfilment import store as fulfilment_store

CAPTURE = "5S647034CC1979815"
OTHER = "12345678901234567"


def _request(path: str, *, method: str = "GET", cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 8000), "app": object()}
    return Request(scope)


def _save_local_shipment(capture_id: str = CAPTURE) -> None:
    conn = fulfilment_store._connect()
    try:
        body = {"trackers": [{"transaction_id": capture_id, "tracking_number": "TRACK12345",
                              "carrier": "USPS", "status": "SHIPPED"}]}
        conn.execute(
            "INSERT INTO fulfilment_files (capture_id, created_at, request_json, request_sha256, approval_sha256, "
            "tracker_readback_json, checks_json, file_sha256, status, limitation_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, "2026-10-25T12:00:00+0000", json.dumps(body), "a" * 64, "b" * 64,
             "{}", "{}", "c" * 64, "saved_without_registration", None))
        conn.commit()
    finally:
        conn.close()


def test_sale_ledger_migration_preserves_existing_dispute_log_and_not_null(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                 "actor TEXT NOT NULL CHECK(actor IN ('vendor','system')), action TEXT NOT NULL, "
                 "dispute_id TEXT NOT NULL, detail TEXT NOT NULL)")
    conn.execute("INSERT INTO audit_log(ts,actor,action,dispute_id,detail) "
                 "VALUES ('2026-10-24T12:00:00+0000','vendor','existing','PP-R-DEMO-0000001','{}')")
    conn.commit()
    conn.close()

    ensure_schema(db)
    ensure_schema(db)
    conn = sqlite3.connect(db)
    try:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(audit_log)")}
        assert columns["dispute_id"][3] == 1
        assert conn.execute("SELECT id, dispute_id, action FROM audit_log").fetchall() == [
            (1, "PP-R-DEMO-0000001", "existing")]
        sale_columns = {row[1] for row in conn.execute("PRAGMA table_info(sale_audit_log)")}
        assert sale_columns == {"id", "ts", "actor", "action", "capture_id", "detail"}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(sale_audit_log)")}
        assert "idx_sale_audit_capture" in indexes
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sale_audit_log(ts,actor,action,capture_id,detail) "
                         "VALUES ('x','system','buyer_note_response',?, '{}')", (CAPTURE,))
        conn.rollback()
    finally:
        conn.close()


def test_sale_repo_append_read_validation_and_capture_isolation(tmp_path):
    db = tmp_path / "sale-ledger.db"
    first = repo.append_sale_action(db, CAPTURE, "buyer_note_response",
                                   {"response": "delivered", "source": "seller_recorded"})
    repo.append_sale_action(db, OTHER, "buyer_note_response",
                            {"response": "not_received", "source": "seller_recorded"})
    rows = repo.list_sale_actions(db, CAPTURE)
    assert len(rows) == 1 and rows[0]["id"] == first
    assert rows[0]["actor"] == "vendor" and rows[0]["capture_id"] == CAPTURE
    assert rows[0]["detail"] == {"response": "delivered", "source": "seller_recorded"}
    assert rows[0]["ts"]
    with pytest.raises(ValueError):
        repo.append_sale_action(db, "not-a-real-id", "buyer_note_response",
                                {"response": "delivered", "source": "seller_recorded"})
    with pytest.raises(ValueError):
        repo.append_sale_action(db, CAPTURE, "other_action",
                                {"response": "delivered", "source": "seller_recorded"})
    with pytest.raises(ValueError):
        repo.append_sale_action(db, CAPTURE, "buyer_note_response",
                                {"response": "approved", "source": "model"})


def _record(*args, **kwargs):
    """O4 made the sale response route async (it reads the posted form itself); the direct call awaits it."""
    import asyncio
    import inspect
    from copilot.features.buyer_note import routes
    result = routes.record_sale_buyer_response(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def test_pre_dispute_routes_use_saved_sale_csrf_and_core_ledger_readback(tmp_path, monkeypatch):
    from copilot.features.buyer_note import routes

    db = tmp_path / "route.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    _save_local_shipment()
    monkeypatch.setattr(routes, "new_csrf_token", lambda: "direct-csrf-token")

    page = routes.sale_buyer_note_page(_request(f"/fulfilment/{CAPTURE}/buyer-note"), CAPTURE)
    assert page.status_code == 200
    html = page.body.decode("utf-8")
    assert "TRACK12345" in html and "USPS" in html
    assert "No estimated delivery date is recorded." in html
    assert "Copy-only seller preparation based on the saved local fulfilment record" in html
    assert "Proofie has not contacted the buyer or PayPal" in html
    assert "sale_audit_log" in {row[0] for row in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    invalid = routes.sale_buyer_note_page(_request("/fulfilment/not-a-capture/buyer-note"), "not-a-capture")
    missing = routes.sale_buyer_note_page(_request(f"/fulfilment/{OTHER}/buyer-note"), OTHER)
    assert invalid.status_code == 400
    assert missing.status_code == 404

    rejected = _record(
        _request(f"/fulfilment/{CAPTURE}/buyer-note/response", method="POST", cookie="csrf_token=wrong"),
        CAPTURE, "delivered", "en", "direct-csrf-token")
    assert rejected.status_code == 403
    invalid_state = _record(
        _request(f"/fulfilment/{CAPTURE}/buyer-note/response", method="POST", cookie="csrf_token=direct-csrf-token"),
        CAPTURE, "closed", "en", "direct-csrf-token")
    assert invalid_state.status_code == 400
    assert repo.list_sale_actions(db, CAPTURE) == []

    saved = _record(
        _request(f"/fulfilment/{CAPTURE}/buyer-note/response", method="POST", cookie="csrf_token=direct-csrf-token"),
        CAPTURE, "not_received", "en", "direct-csrf-token")
    assert saved.status_code == 303
    assert repo.list_sale_actions(db, CAPTURE)[0]["detail"]["response"] == "not_received"
    reread = routes.sale_buyer_note_page(_request(f"/fulfilment/{CAPTURE}/buyer-note"), CAPTURE)
    assert "Sale ledger readback" in reread.body.decode("utf-8")
    assert "Buyer still says item is missing" in reread.body.decode("utf-8")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
        assert conn.execute("SELECT dispute_id FROM audit_log WHERE dispute_id = ?", (CAPTURE,)).fetchone() is None
    finally:
        conn.close()


def test_pre_dispute_render_has_two_copy_only_destinations_and_no_send(tmp_path, monkeypatch):
    from copilot.features.buyer_note import routes

    db = tmp_path / "copy-targets.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    _save_local_shipment()
    response = routes.sale_buyer_note_page(
        _request(f"/fulfilment/{CAPTURE}/buyer-note"), CAPTURE)

    assert response.status_code == 200
    html = response.body.decode("utf-8")
    assert 'data-copy-target="sale-buyer-note"' in html
    assert 'data-copy-target="sale-buyer-note-paypal"' in html
    assert "Copy for WhatsApp / SMS" in html
    assert "Copy for PayPal message" in html
    assert "same deterministic note" in html
    assert "Copying never sends it or grants permission to send" in html
    assert html.count("TRACK12345") == 2
    assert "No estimated delivery date is recorded." in html
    assert "/send" not in html and "negotiation_drafts" not in html
    assert "api-m.paypal.com" not in html and "fetch(" not in html


def test_existing_dispute_route_and_sale_route_remain_distinct():
    from copilot.features.buyer_note import routes

    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in routes.router.routes}
    assert ("/disputes/{dispute_id}/buyer-note", ("GET",)) in paths
    assert ("/disputes/{dispute_id}/buyer-note/response", ("POST",)) in paths
    assert ("/fulfilment/{capture_id}/buyer-note", ("GET",)) in paths
    assert ("/fulfilment/{capture_id}/buyer-note/response", ("POST",)) in paths
    form = Path("copilot/web/templates/features/fulfilment/form.html").read_text(encoding="utf-8")
    assert '{% if feature_enabled("buyer_note") %}' in form
    assert 'href="/fulfilment/{{ capture_id }}/buyer-note"' in form


def test_existing_dispute_buyer_note_route_still_renders(tmp_path, monkeypatch):
    from copilot.features.buyer_note import routes

    db = tmp_path / "dispute-route.db"
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(routes, "new_csrf_token", lambda: "dispute-csrf")
    response = routes.buyer_note_page(_request("/disputes/PP-R-DEMO-0000001/buyer-note"),
                                      "PP-R-DEMO-0000001")
    assert response.status_code == 200
    assert "Copy-only proposal" in response.body.decode("utf-8")
    assert "Record what the seller heard" in response.body.decode("utf-8")
