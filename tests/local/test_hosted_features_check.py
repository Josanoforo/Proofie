"""GET /healthz/features: the read-only proof of which flags are on in a hosted copy; names only, health check untouched."""
import os
import re
from pathlib import Path

from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app

ROOT = Path(__file__).resolve().parents[2]
ACCESS = "synthetic-access-code-1234"


def _yaml_flags():
    text = (ROOT / "render.free.yaml").read_text(encoding="utf-8")
    return dict(re.findall(r'^      - key: (COPILOT_FEATURE_[A-Z0-9_]+)\r?\n        value: "(true|false)"\r?$', text, re.M))


def _hosted(monkeypatch, tmp_path):
    for key in [k for k in os.environ if k.startswith("COPILOT_FEATURE_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", ACCESS)
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)


def test_blueprint_has_74_flags_and_all_are_on():
    flags = _yaml_flags()
    assert len(flags) == 75 == (ROOT / "render.free.yaml").read_text(encoding="utf-8").count("key: COPILOT_FEATURE_")
    assert set(flags.values()) == {"true"}
    assert flags["COPILOT_FEATURE_MCP_LOCAL_CONTRACTS"] == flags["COPILOT_FEATURE_DEMO_DOCUMENTS"] == "true"


def test_the_check_matches_the_yaml_once_the_environment_holds_those_values(monkeypatch, tmp_path):
    _hosted(monkeypatch, tmp_path)
    expected = _yaml_flags()
    for name, value in expected.items():                  # what the Render ops script sets on the live service
        monkeypatch.setenv(name, value)
    web = TestClient(create_app(), base_url="http://testserver")
    body = web.get("/healthz/features")                   # no access cookie
    assert body.status_code == 200 and set(body.json()) == {"on", "off"}
    assert set(body.json()["on"]) == {k for k, v in expected.items() if v == "true"}
    assert body.json()["off"] == []
    assert ACCESS not in body.text and "PAYPAL" not in body.text
    table = os.environ.get("PROOFIE_FLAG_TABLE_OUT")
    if table:
        example = dict(re.findall(r"^(COPILOT_FEATURE_[A-Z0-9_]+)=(\w+)$", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
        data = body.json()
        rows = ["| Flag | .env.example | render.free.yaml | Check URL with the yaml values in the environment |", "|---|---|---|---|"]
        for name in sorted(set(example) | set(expected)):
            shown = "on" if name in data["on"] else "off" if name in data["off"] else "(not listed)"
            rows.append(f"| {name[len('COPILOT_FEATURE_'):]} | {example.get(name, '(absent)')} | {expected.get(name, '(absent)')} | {shown} |")
        Path(table).write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_a_flag_the_host_never_set_shows_as_off_and_a_set_one_as_on(monkeypatch, tmp_path):
    _hosted(monkeypatch, tmp_path)
    monkeypatch.setenv("COPILOT_FEATURE_CASE_HISTORY", "true")
    data = TestClient(create_app(), base_url="http://testserver").get("/healthz/features").json()
    assert data["on"] == ["COPILOT_FEATURE_CASE_HISTORY"]
    assert "COPILOT_FEATURE_GAP_RESOLUTION" in data["off"] and "COPILOT_FEATURE_OUTCOME_APPEAL" in data["off"]


def test_healthz_keeps_its_200_and_only_get_is_open(monkeypatch, tmp_path):
    _hosted(monkeypatch, tmp_path)
    web = TestClient(create_app(), base_url="http://testserver")
    assert web.get("/healthz").status_code == 200 and web.get("/healthz").json() == {"status": "ok"}
    assert web.head("/healthz").status_code == 200
    assert web.post("/healthz/features").status_code == 401


def test_local_mode_has_no_features_check(monkeypatch, tmp_path):
    for key in [k for k in os.environ if k.startswith("COPILOT_FEATURE_")]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    app = create_app()
    assert TestClient(app, client=("127.0.0.1", 50000)).get("/healthz/features").status_code == 404


def test_the_two_blueprints_carry_the_same_flags():
    private = ROOT / "render.yaml"
    if private.is_file():                          # the paid-plan Blueprint is private; the kit has render.free.yaml only
        text = private.read_text(encoding="utf-8")
        assert dict(re.findall(r'^      - key: (COPILOT_FEATURE_[A-Z0-9_]+)\r?\n        value: "(true|false)"\r?$', text, re.M)) == _yaml_flags()
