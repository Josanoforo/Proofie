"""Two distinct in-process clients; no external host or provider transport."""
import re
from starlette.requests import Request

from copilot import config
from copilot.db import repo
from copilot.db import schema
from copilot.features.second_opinion import routes
from copilot.web.security import CSRF_COOKIE_NAME, new_csrf_token


DISPUTE_ID = "PP-R-LOCAL-0000001"


def _request(path, method, port, csrf=None):
    headers = [(b"host", b"proofie.test")]
    if csrf:
        headers.append((b"cookie", f"{CSRF_COOKIE_NAME}={csrf}".encode("ascii")))
    return Request({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": method, "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
                    "query_string": b"", "headers": headers, "client": ("127.0.0.1", port),
                    "server": ("127.0.0.1", 80)})


def test_second_reviewer_uses_separate_local_request_context_and_cannot_change_package(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    db = config.db_path()
    schema.ensure_schema(db)
    package_id = repo.insert_package(db, DISPUTE_ID, "human_edit", True, [])
    initial = repo.get_package(db, package_id)

    seller_csrf = new_csrf_token()
    create_path = f"/disputes/{DISPUTE_ID}/packages/{package_id}/second-opinion"
    created = routes.create(_request(create_path, "POST", 41001, seller_csrf), DISPUTE_ID,
                            package_id, csrf_token=seller_csrf)
    assert created.status_code == 200
    match = re.search(r"/second-opinion/([A-Za-z0-9_-]{40,60})", created.body.decode("utf-8"))
    assert match, "seller request context receives a local one-time review link"
    token = match.group(1)

    review_path = f"/second-opinion/{token}"
    opened = routes.review(_request(review_path, "GET", 41002), token)
    body = opened.body.decode("utf-8")
    assert opened.status_code == 200
    assert "read-only" in body.lower()
    assert "cannot edit or approve" in body.lower()
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert csrf, "review form carries its CSRF token"
    note = "Please check the parcel photo."
    saved = routes.note(_request(f"{review_path}/note", "POST", 41002, csrf.group(1)), token,
                        csrf_token=csrf.group(1), reviewer_note=note)
    assert saved.status_code == 200
    assert note in saved.body.decode("utf-8")
    assert repo.get_package(config.db_path(), package_id) == initial
    assert repo.list_claims(config.db_path(), package_id) == []
    assert len((tmp_path / "second_opinion" / "notes.jsonl").read_text(encoding="utf-8").splitlines()) == 1
