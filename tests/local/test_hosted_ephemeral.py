"""Fresh stores/assets and current C-D guards, fakes only; no Render or model."""
import hashlib
import sqlite3
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import is_private_checkout
from copilot import config
from copilot.hosted_ephemeral import NOTICE, create_app, seed_local_assets

ROOT = Path(__file__).resolve().parents[2]


def test_fresh_process_sdk_evidence_uses_selected_ephemeral_session(tmp_path):
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(('COPILOT_', 'PROOFIE_', 'PAYPAL_', 'OLLAMA_'))}
    environment.update(PYTHONPATH=str(ROOT), PYTHON_DOTENV_DISABLED='1',
                       COPILOT_HOSTED_DEMO='true', COPILOT_ACCESS_CODE='synthetic-access-code',
                       COPILOT_EPHEMERAL_ROOT=str(tmp_path / 'sessions'),
                       COPILOT_DATA_DIR=str(tmp_path / 'sessions'),
                       PROOFIE_EVIDENCE_DIR=str(tmp_path / 'sessions/evidence'),
                       PAYPAL_CLIENT_ID='[redacted]', PAYPAL_CLIENT_SECRET='[redacted]')
    code = '''
import json
from pathlib import Path
from copilot.hosted_ephemeral import create_app
from copilot import config
create_app()
from copilot.core.disputes import SandboxClient
client = SandboxClient('synthetic-no-call')
try:
    print(json.dumps({'sdk_log_in_session': client.log_path.parent == config.data_dir() / 'evidence'}))
finally:
    client._http.close()
'''
    child = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=environment,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)['sdk_log_in_session'] is True


@pytest.fixture(autouse=True)
def isolate_factory_environment_and_template_notice(monkeypatch):
    from copilot.web.routes_disputes import templates
    from copilot.web.routes_package import templates as package_templates
    envs = [templates.env, package_templates.env]
    old = [env.globals["hosted_demo_sentence"] for env in envs]
    previous = {name: os.environ.get(name) for name in ("COPILOT_DATA_DIR", "PROOFIE_EVIDENCE_DIR")}
    for name in ("COPILOT_DATA_DIR", "PROOFIE_EVIDENCE_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield
    for env, sentence in zip(envs, old):
        env.globals["hosted_demo_sentence"] = sentence
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def configured(monkeypatch, root):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "synthetic-access-code")
    monkeypatch.setenv("COPILOT_EPHEMERAL_ROOT", str(root))
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "false")
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)


def test_fresh_sessions_reseed_exact_assets_with_empty_tables_and_retain_prior_data(tmp_path):
    first = seed_local_assets(tmp_path)
    retained = first / "evidence" / "synthetic.jsonl"
    retained.write_text("DEMO DATA", encoding="utf-8")
    second = seed_local_assets(tmp_path)
    assert first != second and retained.read_text(encoding="utf-8") == "DEMO DATA"
    files = list((second / "demo/documents").iterdir())
    assert len(files) == 5
    for p in files:
        assert hashlib.sha256(p.read_bytes()).digest() == hashlib.sha256((ROOT / "demo/documents" / p.name).read_bytes()).digest()
    with sqlite3.connect(second / "copilot.db") as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        assert tables and "approvals" in tables and "documents" in tables
        assert all(db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0] == 0 for name in tables)


@pytest.mark.parametrize("hosted,root", [("false", "absolute"), ("true", ""), ("true", "relative")])
def test_invalid_configuration_has_no_startup_writes(monkeypatch, tmp_path, hosted, root):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", hosted)
    monkeypatch.setenv("COPILOT_EPHEMERAL_ROOT", str(tmp_path) if root == "absolute" else root)
    with pytest.raises(RuntimeError): create_app()
    assert not list(tmp_path.iterdir())


def test_ephemeral_factory_starts_with_the_appeal_flag_on(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "true")
    assert create_app() is not None


def test_short_access_code_refused_before_reseed(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "short")
    with pytest.raises(RuntimeError, match="access code|ACCESS_CODE"): create_app()
    assert not list(tmp_path.iterdir())


def test_current_cd_write_subflags_are_retained_not_disabled(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT_REGISTER", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    create_app()
    assert config.feature_enabled("fulfilment_register") and config.feature_enabled("resolution_writes")
    assert config.db_path().is_file() and config.db_path().parent.parent == tmp_path


def test_blueprint_keeps_exact_seventy_one_flags_no_disk_and_single_worker():
    variant = (ROOT / "render.free.yaml").read_text(encoding="utf-8")
    key = "      - key: COPILOT_FEATURE_"
    assert variant.count(key) == 75
    others = [p for p in ROOT.glob("render*.yaml") if p.name != "render.free.yaml"]   # the paid-plan Blueprint is private
    if is_private_checkout(ROOT):
        assert others, "the private checkout keeps the paid-plan Blueprint"
    if others:
        original = others[0].read_text(encoding="utf-8")
        assert variant[variant.index(key):] == original[original.index(key):]
    assert "    disk:" not in variant and "    plan: free" in variant
    assert "--factory" in variant and '--workers 1' in variant
    assert 'autoDeployTrigger: "off"' in variant and 'numInstances: 1' in variant
    assert "COPILOT_DATA_DIR" in variant and "PROOFIE_EVIDENCE_DIR" in variant
    assert "starts this demo with a fresh session" in NOTICE and "idle sleep" in NOTICE and "about one minute" in NOTICE


def test_auth_health_docs_and_credentials_notice_unchanged(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    configured(monkeypatch, tmp_path)
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    app = create_app()
    c = TestClient(app, base_url="http://testserver")
    assert c.get('/healthz').status_code == 200
    refusal = c.get('/disputes', follow_redirects=False)
    assert refusal.status_code == 303 and refusal.headers['location'].startswith('/access?next=')
    assert c.post('/disputes', data={}).status_code == 401
    from _hosted import authenticated_client
    c = authenticated_client(app)
    assert c.get('/docs').status_code == 404
    r = c.get('/disputes')
    import html as _html, re as _re   # the note is one sentence per line (spans), so compare the text without the tags
    note = " ".join(_html.unescape(_re.sub(r"<[^>]+>", " ", _re.search(r'<p class="hosted-demo-note"[^>]*>(.*?)</p>', r.text, _re.S).group(1))).split())
    assert r.status_code == 503 and "hosted-no-credentials" in r.text and NOTICE in note


def test_ephemeral_factory_enforces_actual_upload_budget_before_handler(monkeypatch, tmp_path):
    from _hosted import authenticated_client
    from copilot.db import repo
    configured(monkeypatch, tmp_path)
    app = create_app()
    uploads = config.uploads_dir()
    uploads.mkdir()
    sentinel = uploads / 'synthetic-budget.bin'
    with sentinel.open('wb') as output:
        output.truncate(config.HOSTED_UPLOAD_BUDGET_BYTES)
    assert sentinel.stat().st_size == 200 * 1024 * 1024
    client = authenticated_client(app)
    response = client.post('/disputes/PP-R-DEMO-0000001/documents', content=b'synthetic')
    assert response.status_code == 413 and 'upload limit' in response.text
    assert list(uploads.iterdir()) == [sentinel]
    assert repo.list_documents(config.db_path(), 'PP-R-DEMO-0000001') == []
    assert not (uploads.parent / '.proofie-upload.lock').exists()
