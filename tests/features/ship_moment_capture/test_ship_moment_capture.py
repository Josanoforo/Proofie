from datetime import datetime, timezone
import io
import inspect

import pytest
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from copilot.features.ship_moment_capture import service

ID = "12345678901234567"
JPEG = ("label.jpg", "image/jpeg", b"\xff\xd8\xffsample")


def test_saves_seller_inputs_with_code_timestamp_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    saved = service.save(ID, "Local Carrier", JPEG, None, now=now)
    assert saved["captured_at"] == "2026-10-25T12:00:00+00:00"
    assert saved["source"] == "seller-entered"
    assert (tmp_path / "ship_moment_capture" / ID / "label.jpg").read_bytes() == JPEG[2]
    assert service.read(ID) == saved


def test_replacement_keeps_the_unmodified_photo_and_case_link(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    dispute_id = "PP-R-DEMO-0000001"
    parcel = ("parcel.png", "image/png", b"\x89PNG\r\n\x1a\nparcel")
    service.save(ID, "Estafeta", JPEG, parcel, dispute_id=dispute_id)
    updated = service.save(ID, "Estafeta", ("new.jpg", "image/jpeg", b"\xff\xd8\xffnew"), None)
    assert updated["dispute_id"] == dispute_id
    assert updated["files"]["label"] == "label.jpg"
    assert updated["files"]["parcel"] == "parcel.png"
    assert (tmp_path / "ship_moment_capture" / ID / "parcel.png").read_bytes() == parcel[2]


def test_shipment_photos_become_same_case_documents_idempotently(tmp_path, monkeypatch):
    from copilot.core.raw_storage import read_raw
    from copilot.db import repo

    dispute_id = "PP-R-DEMO-0000001"
    db = tmp_path / "proofie.sqlite3"
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(service.config, "db_path", lambda: db)
    monkeypatch.setattr(service.config, "uploads_dir", lambda: uploads)
    record = service.save(ID, "Skydropx", JPEG, None, dispute_id=dispute_id)
    first = service.link_case_documents(dispute_id, ID, record)
    second = service.link_case_documents(dispute_id, ID, record)

    assert len(first) == 1 and first == second
    row = repo.get_document(db, first[0]["id"])
    assert row["dispute_id"] == dispute_id
    assert row["filename"].startswith("shipment-label-" + ID)
    assert row["sha256"] == first[0]["sha256"]
    assert row["extraction_ok"] is False and row["extracted_text"] is None
    assert read_raw(uploads, row["sha256"]) == JPEG[2]


@pytest.mark.parametrize("item", [("x.jpg", "image/jpeg", b"text"),
                                    ("x.png", "image/png", b"\x89PNG")])
def test_rejects_mismatched_image_signatures(tmp_path, monkeypatch, item):
    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="valid JPEG or PNG"):
        service.save(ID, "Carrier", item, None)


def test_rejects_oversized_upload_before_persisting(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="5 MB"):
        service.save(ID, "Carrier", ("x", "image/jpeg", b"\xff\xd8\xff" + b"x" * service.MAX_IMAGE_BYTES), None)
    assert not (tmp_path / "ship_moment_capture" / ID).exists()


def test_tracking_page_links_to_shipment_capture():
    from pathlib import Path
    page = Path("copilot/web/templates/features/fulfilment/form.html").read_text(encoding="utf-8")
    assert 'feature_enabled("ship_moment_capture")' in page
    assert 'features/ship_moment_capture/_entry.html' in page


def _request(path, *, method="GET", cookie=""):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": [(b"cookie", cookie.encode())] if cookie else [],
             "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 8000), "app": object()}
    return Request(scope)


class ForbiddenClient:
    def __init__(self):
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("shipment moment capture must not make provider calls")


def test_direct_get_and_post_routes_save_local_capture_and_render_readback(tmp_path, monkeypatch):
    from copilot.features.ship_moment_capture import routes

    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    fixed = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", FixedDateTime)
    fake = ForbiddenClient()
    path = f"/fulfilment/{ID}/moment"
    response = routes.form(_request(path), ID)
    assert response.status_code == 200
    assert "Save shipment-time evidence" in response.body.decode("utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert "client" not in inspect.signature(routes.form).parameters

    token = "synthetic-csrf-token"
    upload = UploadFile(filename=JPEG[0], file=io.BytesIO(JPEG[2]),
                        headers=Headers({"content-type": JPEG[1]}))
    redirect = __import__("asyncio").run(routes.save(_request(path, method="POST", cookie=f"csrf_token={token}"),
                                                       ID, csrf_token=token, carrier="Local Carrier",
                                                       label=upload, parcel=None))
    assert redirect.status_code == 303
    record = service.read(ID)
    assert record == {"capture_id": ID, "carrier": "Local Carrier", "captured_at": "2026-10-25T12:00:00+00:00",
                      "files": {"label": "label.jpg"}, "source": "seller-entered"}
    assert (tmp_path / "ship_moment_capture" / ID / "label.jpg").read_bytes() == JPEG[2]
    readback = routes.form(_request(path), ID)
    shown = __import__("re").sub(r"<[^>]+>", " ", readback.body.decode("utf-8"))      # what a seller reads, no machine date
    assert "25 Oct 2026" in shown and "2026-10-25T12:00:00" not in shown
    assert fake.calls == []


@pytest.mark.parametrize("path,token,cookie,status", [
    ("/fulfilment/invalid/moment", "ok", "csrf_token=ok", 400),
    (f"/fulfilment/{ID}/moment", "wrong", "csrf_token=right", 403),
])
def test_direct_post_route_rejects_invalid_id_and_csrf_without_writing(tmp_path, monkeypatch, path, token, cookie, status):
    from copilot.features.ship_moment_capture import routes

    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    upload = UploadFile(filename=JPEG[0], file=io.BytesIO(JPEG[2]),
                        headers=Headers({"content-type": JPEG[1]}))
    response = __import__("asyncio").run(routes.save(_request(path, method="POST", cookie=cookie),
                                                      ID if status == 403 else "invalid", csrf_token=token,
                                                      carrier="Local Carrier", label=upload, parcel=None))
    assert response.status_code == status
    assert not (tmp_path / "ship_moment_capture").exists()


def test_direct_post_route_rejects_bad_signature_and_preserves_readable_page(tmp_path, monkeypatch):
    from copilot.features.ship_moment_capture import routes

    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    token = "synthetic-csrf-token"
    bad = UploadFile(filename="label.jpg", file=io.BytesIO(b"not a JPEG"),
                     headers=Headers({"content-type": "image/jpeg"}))
    response = __import__("asyncio").run(routes.save(_request(f"/fulfilment/{ID}/moment", method="POST",
                                                               cookie=f"csrf_token={token}"),
                                                      ID, csrf_token=token, carrier="Local Carrier",
                                                      label=bad, parcel=None))
    assert response.status_code == 400
    assert "valid JPEG or PNG" in response.body.decode("utf-8")
    assert not (tmp_path / "ship_moment_capture" / ID).exists()
