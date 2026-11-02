"""The shipment-moment and third-party-statement uploads share the documents upload cap and budget in the hosted demo.

Expected, written before the run: in hosted mode a body over MAX_UPLOAD_BODY_BYTES, or any body once the budget is used up,
gets the same 413 page as POST /disputes/<id>/documents and never reaches the handler; bytes in the third-party drawer
count toward the budget; other paths are untouched; in local mode the middleware is not installed and nothing changes.
"""
import asyncio
from collections import deque

import pytest
from fastapi.testclient import TestClient

from _hosted import TEST_ACCESS_CODE
from copilot import config
from copilot.web import security

CAPTURE = "1AB23456CD789012E"
PATHS = ["/disputes/PP-R-DEMO-0000001/documents",
         "/disputes/PP-R-DEMO-0000001/third-party-statements",
         f"/fulfilment/{CAPTURE}/moment"]


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)
    return tmp_path


def post(path, chunks, method="POST"):
    """Send one request through the hosted middleware; return (status, body text, handler calls)."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "scheme": "https", "server": ("localhost", 443),
             "client": ("10.0.0.1", 1), "http_version": "1.1",
             "headers": [(b"cookie", ("proofie_access=" + security.access_cookie_value()).encode())]}
    calls, sent = [], []

    async def handler(scope, receive, send):
        calls.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    queue = deque({"type": "http.request", "body": body, "more_body": i < len(chunks) - 1} for i, body in enumerate(chunks))

    async def receive():
        return queue.popleft() if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(security.HostedAccessMiddleware(handler)(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    text = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()
    return status, text, calls


def test_the_upload_pattern_names_exactly_the_three_file_routes():
    for path in PATHS + [p + "/" for p in PATHS]:
        assert security.UPLOAD_PATH.fullmatch(path), path
    for path in ("/disputes/X/third-party-statements/1/case-document", f"/fulfilment/{CAPTURE}/moment/x",
                 "/lessons-exchange/import", "/disputes/X/documents/1/original"):
        assert not security.UPLOAD_PATH.fullmatch(path), path


@pytest.mark.parametrize("path", PATHS)
def test_hosted_body_over_the_cap_is_refused_like_documents_and_never_reaches_the_handler(hosted, path):
    big = b"x" * (security.MAX_UPLOAD_BODY_BYTES + 1)
    status, text, calls = post(path, [big])
    assert status == 413 and security.UPLOAD_LIMIT_MESSAGE in text and calls == []
    reference = post(PATHS[0], [big])
    assert (status, text) == reference[:2]               # the very same refusal the documents route gives


@pytest.mark.parametrize("path", PATHS)
def test_hosted_body_at_or_under_the_cap_reaches_the_handler(hosted, path):
    status, text, calls = post(path, [b"x" * 1000])
    assert status == 200 and calls == [b"x" * 1000]


@pytest.mark.parametrize("path", PATHS)
def test_hosted_budget_used_up_by_uploads_refuses_every_file_route(hosted, monkeypatch, path):
    monkeypatch.setattr(config, "HOSTED_UPLOAD_BUDGET_BYTES", 100)
    uploads = config.uploads_dir()
    uploads.mkdir(parents=True)
    (uploads / "existing").write_bytes(b"x" * 100)
    status, text, calls = post(path, [b"small"])
    assert status == 413 and security.UPLOAD_LIMIT_MESSAGE in text and calls == []


def test_hosted_budget_is_charged_for_bytes_kept_in_the_third_party_drawer(hosted, monkeypatch):
    monkeypatch.setattr(config, "HOSTED_UPLOAD_BUDGET_BYTES", 1000)
    drawer = config.data_dir() / security.BUDGET_DRAWER
    drawer.write_bytes(b"x" * 400)
    assert post(PATHS[2], [b"small"])[0] == 200          # 400 of 1000 used: room left
    (config.data_dir() / (security.BUDGET_DRAWER + "-wal")).write_bytes(b"x" * 600)
    status, text, calls = post(PATHS[2], [b"small"])     # 1000 of 1000 used: the sidecar file counts too
    assert status == 413 and security.UPLOAD_LIMIT_MESSAGE in text and calls == []


def test_hosted_other_paths_and_reads_are_not_buffered_or_capped(hosted, monkeypatch):
    monkeypatch.setattr(config, "HOSTED_UPLOAD_BUDGET_BYTES", 1)
    big = b"x" * (security.MAX_UPLOAD_BODY_BYTES + 1)
    assert post("/lessons-exchange/import", [big])[0] == 200
    assert post(PATHS[2], [big], method="GET")[0] == 200


def test_local_mode_has_no_cap_on_these_routes(monkeypatch, tmp_path):
    from copilot.app import create_app
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")
    monkeypatch.setenv("COPILOT_FEATURE_SHIP_MOMENT_CAPTURE", "true")
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)
    monkeypatch.setattr(config, "HOSTED_UPLOAD_BUDGET_BYTES", 1)
    web = TestClient(create_app(), base_url="http://localhost", client=("127.0.0.1", 12345))
    big = b"x" * (security.MAX_UPLOAD_BODY_BYTES + 1)
    response = web.post(PATHS[2], data={"csrf_token": "wrong"}, files={"label": ("l.png", big, "image/png")})
    assert response.status_code == 403 and security.UPLOAD_LIMIT_MESSAGE not in response.text   # the handler's own CSRF refusal
