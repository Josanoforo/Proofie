"""Hosted-demo preparation that holds in any hosted mode: /healthz and no API documentation routes when hosted, one
data folder setting (COPILOT_DATA_DIR) for every local write, and credentials pages a host visitor can read. With the
flag off everything stays as before. Offline: fake or raising PayPal client, no model, no network."""
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pilot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dom import parse, state  # noqa: E402
from copilot import app as app_module  # noqa: E402
from copilot import config  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.core.disputes import PayPalError  # noqa: E402
from copilot.web.routes_disputes import CredentialsMissing, get_client  # noqa: E402
from copilot.web.security import CSRF_COOKIE_NAME  # noqa: E402
from _hosted import authenticated_client, configure_hosted

DID = "PP-R-DEMO-0000001"
DOC_ROUTES = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")

# The local texts exactly as they were before this change (the earlier strings; they must not move by one byte).
LOCAL_MISSING = ("This app reads your own PayPal sandbox app's Client ID and Secret from a file named .env in the app "
                 "folder. Copy .env.example to .env, fill in both values, restart the app and reload this page (see "
                 "the README's Configure section). Nothing was sent to PayPal.")
LOCAL_REJECTED = ("PayPal rejected this app's sandbox credentials. Check the Client ID and Secret in the .env file in "
                  "the app folder (both must be from the same sandbox app), restart the app and reload this page. "
                  "Retrying will not help until they are fixed.")


def _flag(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    else:
        monkeypatch.setenv("COPILOT_HOSTED_DEMO", value)
    configure_hosted(monkeypatch)


def _raise_missing():
    raise CredentialsMissing("x")


class Rejecting:
    def call(self, method, path, op, **kw):
        raise PayPalError(401, "invalid_client", "d", {})


def _client(dependency):
    app = create_app()
    app.dependency_overrides[get_client] = dependency

    @app.post("/__reject")
    async def reject():
        raise PayPalError(403, "invalid_client", "d", {})

    return authenticated_client(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


# ---- 1 and 2: /healthz and the documentation routes --------------------------------------------------------------
@pytest.mark.parametrize("value", ["true", " TRUE "])
def test_hosted_healthz_answers_200_with_a_tiny_json_and_calls_nothing(monkeypatch, value):
    _flag(monkeypatch, value)

    def _never():
        raise AssertionError("/healthz must not build a PayPal client")

    c = _client(_never)
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("value", [None, "", "false", "1"])
def test_local_healthz_is_404_as_before(monkeypatch, value):
    _flag(monkeypatch, value)
    assert _client(_raise_missing).get("/healthz").status_code == 404


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_hosted_documentation_routes_do_not_exist(monkeypatch, path):
    _flag(monkeypatch, "true")
    assert _client(_raise_missing).get(path).status_code == 404


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_local_documentation_routes_are_served_as_before(monkeypatch, path):
    _flag(monkeypatch, None)
    r = _client(_raise_missing).get(path)
    assert r.status_code == 200
    if path == "/openapi.json":
        paths = r.json()["paths"]
        assert "/healthz" not in paths and "/disputes" in paths


def test_the_route_tables_differ_only_by_healthz_and_the_documentation_routes(monkeypatch):
    def paths():
        return sorted(getattr(r, "path", "") for r in create_app().router.routes)

    _flag(monkeypatch, None)
    local = paths()
    _flag(monkeypatch, "true")
    hosted = paths()
    assert sorted(set(hosted) - set(local)) == ["/access", "/healthz", "/healthz/features"]
    assert sorted(set(local) - set(hosted)) == ["/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"]


# ---- 4: the credentials pages ------------------------------------------------------------------------------------
def _box_text(r, name):
    box = state(parse(r.text), name)
    assert box is not None, name
    return box.text()


@pytest.mark.parametrize("value", [None, "false"])
def test_local_credentials_pages_keep_their_exact_text_and_state(monkeypatch, value):
    _flag(monkeypatch, value)
    r = _client(_raise_missing).get("/disputes")
    assert r.status_code == 503 and _box_text(r, "credentials-missing").count(LOCAL_MISSING) == 1
    assert state(parse(r.text), "hosted-no-credentials") is None
    r = _client(lambda: Rejecting()).get(f"/disputes/{DID}")
    assert r.status_code == 502 and (LOCAL_REJECTED + " Nothing was changed.") in _box_text(r, "credentials-rejected")
    r = _client(lambda: Rejecting()).post("/__reject")
    assert r.status_code == 502 and "could not confirm what happened" in _box_text(r, "credentials-rejected")
    assert LOCAL_REJECTED in _box_text(r, "credentials-rejected")


def test_local_pages_are_the_same_bytes_with_the_flag_unset_or_not_true(monkeypatch):
    bodies = []
    for value in (None, "", "false"):
        _flag(monkeypatch, value)
        bodies.append((_client(_raise_missing).get("/disputes").content,
                       _client(lambda: Rejecting()).get(f"/disputes/{DID}").content))
    assert bodies[0] == bodies[1] == bodies[2]


FORBIDDEN_IN_HOSTED_BOX = (".env", "env file", "app folder", "README", "Configure", "Copy", "restart", "Client ID",
                           "Secret", "\\", "/", "python", "pip ")


def _assert_hosted_box(r):
    text = _box_text(r, app_module.HOSTED_NO_CREDENTIALS_STATE)
    assert app_module.HOSTED_NO_CREDENTIALS_STATE == "hosted-no-credentials"
    assert app_module.HOSTED_NO_CREDENTIALS_MESSAGE in text
    for word in FORBIDDEN_IN_HOSTED_BOX:
        assert word not in text, word
    dom = parse(r.text)
    assert state(dom, "credentials-missing") is None and state(dom, "credentials-rejected") is None
    assert ".env" not in r.text
    assert "banner-error" in r.text
    return text


def test_hosted_missing_credentials_page_says_the_host_has_none_yet(monkeypatch):
    _flag(monkeypatch, "true")
    r = _client(_raise_missing).get("/disputes")
    assert r.status_code == 503
    assert _assert_hosted_box(r).count("Nothing was sent to PayPal.") == 1


def test_hosted_rejected_credentials_page_on_a_read_and_on_a_post(monkeypatch):
    _flag(monkeypatch, "true")
    r = _client(lambda: Rejecting()).get(f"/disputes/{DID}")
    assert r.status_code == 502 and "Nothing was changed." in _assert_hosted_box(r)
    r = _client(lambda: Rejecting()).post("/__reject")
    text = _assert_hosted_box(r)
    assert r.status_code == 502 and "could not confirm what happened" in text and "Nothing was changed" not in text


def test_the_hosted_sentence_is_one_sentence_without_a_command_or_a_path():
    s = app_module.HOSTED_NO_CREDENTIALS_MESSAGE
    assert s.count(".") == 1 and s.endswith(".") and "come back later" in s
    for word in (".env", "/", "\\", "README", "restart"):
        assert word not in s


# ---- 3: COPILOT_DATA_DIR ------------------------------------------------------------------------------------------
@pytest.fixture
def no_overrides():
    """The suite redirects the database and uploads for every test; these tests check the real resolution."""
    config.set_db_path_override(None)
    config.set_uploads_dir_override(None)
    yield


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_keeps_the_default_paths_exactly(monkeypatch, no_overrides, value):
    if value is None:
        monkeypatch.delenv("COPILOT_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("COPILOT_DATA_DIR", value)
    assert config.db_path() is config.DB_PATH and config.uploads_dir() is config.UPLOADS_DIR
    assert config.data_dir() is config.DATA_DIR
    assert str(config.db_path()) == str(ROOT / "data" / "copilot.db")
    assert str(config.uploads_dir()) == str(ROOT / "data" / "uploads")


def test_set_moves_the_database_and_the_uploads_under_it(monkeypatch, no_overrides, tmp_path):
    target = tmp_path / "host-disk"
    monkeypatch.setenv("COPILOT_DATA_DIR", str(target))
    assert config.data_dir() == target.resolve()
    assert config.db_path() == target.resolve() / "copilot.db"
    assert config.uploads_dir() == target.resolve() / "uploads"


def test_the_test_overrides_still_win_over_the_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "ignored"))
    config.set_db_path_override(tmp_path / "o.db")
    config.set_uploads_dir_override(tmp_path / "o-up")
    assert config.db_path() == tmp_path / "o.db" and config.uploads_dir() == tmp_path / "o-up"


def _tree_snapshot(folder):
    """What the app itself writes under the code folder's data/: its database file and its uploads. The rest of data/ is
    other tools' output (kit logs, runs) and can hold thousands of files, so it is not walked."""
    if not folder.exists():
        return None
    db = folder / "copilot.db"
    uploads = folder / "uploads"
    files = [db] if db.is_file() else []
    if uploads.exists():
        files += [p for p in uploads.rglob("*") if p.is_file()]
    return sorted((str(p.relative_to(folder)), p.stat().st_size) for p in files)


def test_an_upload_through_the_app_writes_its_bytes_and_rows_under_the_setting(monkeypatch, no_overrides, tmp_path):
    from copilot.db import repo
    target = tmp_path / "host-disk"
    monkeypatch.setenv("COPILOT_DATA_DIR", str(target))
    _flag(monkeypatch, None)
    real_data_before = _tree_snapshot(ROOT / "data")
    c = _client(_raise_missing)
    token = secrets.token_urlsafe(32)
    set_test_csrf(c, token)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("label.png", png, "image/png")},
               data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert (target / "copilot.db").is_file()
    assert [p.name for p in (target / "uploads").iterdir()] == [hashlib.sha256(png).hexdigest()]
    assert [d["filename"] for d in repo.list_documents(target / "copilot.db", DID)] == ["label.png"]
    assert _tree_snapshot(ROOT / "data") == real_data_before     # nothing new under the code folder


PROBE = r"""
import json, os, sys
from pathlib import Path
root = Path(os.environ["PROBE_ROOT"])
sys.path[:0] = [str(root), str(root / "pilot")]
import client
from copilot.core import vision_model
from copilot import config
out = {"evidence": str(client.EVIDENCE), "fn": str(client.evidence_dir()), "vision": str(vision_model.LOG),
       "db": str(config.db_path()), "uploads": str(config.uploads_dir())}
if os.environ.get("PROBE_WRITE"):
    os.environ["PAYPAL_CLIENT_ID"] = "x"; os.environ["PAYPAL_CLIENT_SECRET"] = "x"
    c = client.SandboxClient("probe_client")
    c._log({"op": "probe"})
    out["log"] = str(c.log_path)
print(json.dumps(out))
"""


def _probe(tmp_path, data_dir=None, evidence_dir=None, write=False):
    env = {k: v for k, v in os.environ.items() if k not in ("PROOFIE_EVIDENCE_DIR", "COPILOT_DATA_DIR")}
    env["PROBE_ROOT"] = str(ROOT)
    if data_dir is not None:
        env["COPILOT_DATA_DIR"] = str(data_dir)
    if evidence_dir is not None:
        env["PROOFIE_EVIDENCE_DIR"] = str(evidence_dir)
    if write:
        env["PROBE_WRITE"] = "1"
    done = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, cwd=str(tmp_path),
                          timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_unset_the_evidence_folder_and_data_paths_are_the_repo_defaults(tmp_path):
    out = _probe(tmp_path)
    assert out["evidence"] == out["fn"] == str(ROOT / "evidence")
    assert out["vision"] == str(ROOT / "evidence" / "copilot_vision.jsonl")
    assert out["db"] == str(ROOT / "data" / "copilot.db") and out["uploads"] == str(ROOT / "data" / "uploads")


def test_set_the_paypal_call_log_goes_under_the_setting(tmp_path):
    target = tmp_path / "host-disk"
    before = _tree_snapshot(ROOT / "evidence")
    out = _probe(tmp_path, data_dir=target, write=True)
    evidence = target.resolve() / "evidence"
    assert out["evidence"] == out["fn"] == str(evidence)
    assert out["vision"] == str(evidence / "copilot_vision.jsonl")
    assert out["log"] == str(evidence / "probe_client.jsonl") and (evidence / "probe_client.jsonl").is_file()
    assert out["db"] == str(target.resolve() / "copilot.db") and out["uploads"] == str(target.resolve() / "uploads")
    assert _tree_snapshot(ROOT / "evidence") == before


def test_the_explicit_evidence_setting_still_wins(tmp_path):
    out = _probe(tmp_path, data_dir=tmp_path / "data", evidence_dir=tmp_path / "elsewhere")
    assert out["evidence"] == str((tmp_path / "elsewhere").resolve())

from _hosted import set_test_csrf
