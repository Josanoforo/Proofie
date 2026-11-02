import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.features.status_link import service

KEY = "k" * 40
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
FIELDS = {"carrier": "DEMO CARRIER", "tracking_number": "DEMO-R3-B4-0001", "status": "In transit",
          "registered": "2026-11-01", "description": "Cotton desk runner", "locale": "en"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_FEATURE_STATUS_LINK", "true")
    monkeypatch.setenv(service.KEY_ENV, KEY)
    monkeypatch.delenv(service.BASE_ENV, raising=False)
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    monkeypatch.setattr(service, "LIMITER", service.PageLimiter())
    config.set_db_path_override(tmp_path / "t.db")
    config.set_uploads_dir_override(tmp_path / "uploads")


def _client(host="127.0.0.1"):
    return TestClient(create_app(), base_url="http://testserver", client=(host, 12345))


def _csrf(page):
    import html
    return html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))


def _create_link(client, fields=None):
    fields = fields or FIELDS
    page = client.get("/status-link")
    assert page.status_code == 200
    preview = client.post("/status-link/preview", data={**fields, "csrf_token": _csrf(page)})
    assert preview.status_code == 200
    sha = re.search(r'name="sha" value="([0-9a-f]{64})"', preview.text).group(1)
    approved = client.post("/status-link/approve", data={**fields, "sha": sha, "csrf_token": _csrf(preview)})
    return preview, approved


def _dl(text):
    return re.search(r"<dl>.*?</dl>", text, re.S).group(0)


def test_token_roundtrip_carries_only_the_approved_fields():
    token = service.make_token(FIELDS, now=NOW)
    view = service.read_token(token, now=NOW + timedelta(days=1))
    assert {k: view[k] for k in service.FIELDS} == FIELDS
    assert view["expires"] == (NOW + timedelta(days=service.TTL_DAYS)).date().isoformat()
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert set(payload) == {"v", "exp", *service.FIELDS}  # no dispute id, buyer name, address, e-mail or amount


def test_expired_tampered_wrong_key_and_missing_key_all_read_as_none(monkeypatch):
    token = service.make_token(FIELDS, now=NOW)
    assert service.read_token(token, now=NOW + timedelta(days=service.TTL_DAYS, seconds=1)) is None
    body, mac = token.split(".")
    forged = base64.urlsafe_b64encode(base64.urlsafe_b64decode(body + "==").replace(b"In transit", b"Delivered")).decode().rstrip("=")
    assert service.read_token(f"{forged}.{mac}", now=NOW) is None
    assert service.read_token(f"{body}.{mac[:-2]}AA", now=NOW) is None
    assert service.read_token("not-a-token", now=NOW) is None
    monkeypatch.setenv(service.KEY_ENV, "z" * 40)
    assert service.read_token(token, now=NOW) is None
    monkeypatch.setenv(service.KEY_ENV, "short")
    assert service.read_token(token, now=NOW) is None
    with pytest.raises(service.LinkError):
        service.make_token(FIELDS, now=NOW)


@pytest.mark.parametrize("field,value", [
    ("description", "write me at jane@example.com"), ("description", "see https://x.test/a"),
    ("description", "call 555 123 4567 now"), ("description", ""), ("description", "x" * 121),
    ("carrier", "<script>"), ("tracking_number", "12 34"), ("tracking_number", "A1"),
    ("status", "Lost"), ("registered", "2026-13-40"), ("locale", "fr")])
def test_clean_refuses_private_or_malformed_input(field, value):
    with pytest.raises(service.LinkError):
        service.clean({**FIELDS, field: value})


def test_seller_flow_preview_equals_what_the_buyer_page_shows():
    client = _client()
    preview, approved = _create_link(client)
    assert approved.status_code == 200
    link = re.search(r"http://testserver/s/[A-Za-z0-9_.-]+", approved.text).group(0)
    buyer = client.get(link.removeprefix("http://testserver"))
    assert buyer.status_code == 200
    assert _dl(buyer.text) == _dl(preview.text)
    assert "From the seller, not from PayPal." in buyer.text
    assert "no reply form" in buyer.text
    assert buyer.headers["x-robots-tag"].startswith("noindex")
    assert buyer.headers["cache-control"] == "no-store"
    assert 'name="robots" content="noindex' in buyer.text
    assert "PP-" not in buyer.text and "dispute" not in buyer.text.lower()


def test_approval_is_bound_to_the_previewed_content():
    client = _client()
    page = client.get("/status-link")
    preview = client.post("/status-link/preview", data={**FIELDS, "csrf_token": _csrf(page)})
    sha = re.search(r'name="sha" value="([0-9a-f]{64})"', preview.text).group(1)
    changed = client.post("/status-link/approve", data={**FIELDS, "status": "Delivered", "sha": sha,
                                                         "csrf_token": _csrf(preview)})
    assert changed.status_code == 400 and "/s/" not in changed.text
    no_csrf = client.post("/status-link/approve", data={**FIELDS, "sha": sha, "csrf_token": "nope"})
    assert "/s/" not in no_csrf.text


def test_every_public_failure_is_the_same_404_and_the_rate_limit_answers_429(monkeypatch):
    client = _client()
    token = service.make_token(FIELDS)
    bad = [f"/s/{token}x", "/s/" + "a" * 30, f"/s/{token.split('.')[0]}.{'A' * 43}"]
    bodies = {client.get(path).text for path in bad}
    assert {client.get(path).status_code for path in bad} == {404}
    assert bodies == {"Not found."}
    monkeypatch.setattr(service, "LIMITER", service.PageLimiter(per_address=3))
    codes = [client.get(f"/s/{token}").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_page_without_a_signing_key_is_404(monkeypatch):
    token = service.make_token(FIELDS)
    monkeypatch.delenv(service.KEY_ENV)
    assert _client().get(f"/s/{token}").status_code == 404
    page = _client().get("/status-link")
    assert "cannot be signed" in page.text


def test_route_and_template_make_no_paypal_call_and_read_no_store():
    route = Path("copilot/features/status_link/routes.py").read_text(encoding="utf-8")
    code = Path("copilot/features/status_link/service.py").read_text(encoding="utf-8")
    for text in (route, code):
        assert "client.call" not in text and "get_client" not in text and "httpx" not in text and "sqlite" not in text
    assert "repo." not in route and "repo." not in code


def test_hosted_gate_opens_only_the_public_status_path(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "an-access-code-123")
    client = _client("203.0.113.9")
    token = service.make_token(FIELDS)
    assert client.get(f"/s/{token}").status_code == 200
    assert client.head(f"/s/{token}").status_code in (200, 405)
    assert client.get("/status-link", follow_redirects=False).status_code == 303
    assert client.post("/status-link/preview", data={}, follow_redirects=False).status_code == 401
    assert client.post(f"/s/{token}", follow_redirects=False).status_code == 401
    assert client.get(f"/s/{token}/extra", follow_redirects=False).status_code == 303
    monkeypatch.setenv("COPILOT_FEATURE_STATUS_LINK", "false")
    assert _client("203.0.113.9").get(f"/s/{token}", follow_redirects=False).status_code == 303


def test_public_base_is_used_only_when_it_is_https(monkeypatch):
    monkeypatch.setenv(service.BASE_ENV, "https://hosted.example.test")
    _preview, approved = _create_link(_client())
    assert "https://hosted.example.test/s/" in approved.text
    monkeypatch.setenv(service.BASE_ENV, "http://plain.example.test")
    _preview, approved = _create_link(_client())
    assert "http://testserver/s/" in approved.text


def test_the_signing_key_value_appears_in_no_page_header_log_or_output(monkeypatch, caplog, capsys, tmp_path):
    marker = "SECRET-KEY-VALUE-" + "z" * 30
    monkeypatch.setenv(service.KEY_ENV, marker)
    evidence = tmp_path / "evidence"
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(evidence))
    client = _client()
    form = client.get("/status-link")
    preview, approved = _create_link(client)
    link = re.search(r"/s/[A-Za-z0-9_.-]+", approved.text).group(0)
    pages = [form, preview, approved, client.get(link), client.get(link + "x"), client.get("/s/" + "a" * 30)]
    for page in pages:
        assert marker not in page.text and marker not in str(dict(page.headers)) and marker not in str(page.cookies)
    out = capsys.readouterr()
    assert marker not in out.out + out.err + caplog.text
    assert not evidence.exists() or all(marker not in f.read_text(encoding="utf-8", errors="replace") for f in evidence.rglob("*") if f.is_file())
    # a token carries the payload and a MAC, never the key
    token = link.removeprefix("/s/")
    assert marker not in token and marker.encode() not in base64.urlsafe_b64decode(token.split(".")[0] + "==")
