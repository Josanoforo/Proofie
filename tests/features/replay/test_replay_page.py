"""RC4.3 W1-4c: the hosted demo's labelled, dated replay of a recorded local-model run.

Offline only: no model call, no PayPal call. The page is static (a committed synthetic fixture), takes effect only with
COPILOT_FEATURE_REPLAY=true AND COPILOT_HOSTED_DEMO=true, writes nothing, and nothing on it can enter a package or a send.
Written first; red for the right reason (no fixture, no route, no links).
"""
import json
import re
from html import unescape
from pathlib import Path

import pytest

pytestmark = pytest.mark.pool_scope        # the hosted bound is ON here (root conftest.py turns it off for other tests)
from _dom import parse
from _hosted import authenticated_client
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = ROOT / "copilot" / "fixtures" / "replay"
CASE = "PP-R-DEMO-0000001"
FIXTURE = FIXTURE_DIR / f"{CASE}.json"
ACCESS_CODE = "test-hosted-access-code"


def _env(monkeypatch, *, hosted=True, flag=True):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true" if hosted else "false")
    if hosted:
        monkeypatch.setenv("COPILOT_ACCESS_CODE", ACCESS_CODE)
    if flag:
        monkeypatch.setenv("COPILOT_FEATURE_REPLAY", "true")
    else:
        monkeypatch.delenv("COPILOT_FEATURE_REPLAY", raising=False)


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class NoPayPal:
    """Any PayPal call from the replay or from the access page is a failure."""

    def call(self, *a, **k):
        raise AssertionError("the replay must not call PayPal")


def _anon(app):
    return TestClient(app, base_url="https://localhost", client=("127.0.0.1", 12345), follow_redirects=False)


# --- the committed fixture ---------------------------------------------------------------------------------------------
def test_the_fixture_is_a_synthetic_recorded_run_with_its_provenance():
    data = _fixture()
    assert data["schema"] == "proofie-replay/1" and data["case"] == CASE
    assert data["model"] == "qwen3.5:9b"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["recorded_at"])
    assert re.fullmatch(r"[0-9a-f]{64}", data["prompt_sha256"])
    assert data["seed"] == 7 and data["temperature"] == 0.4
    assert data["_note"].startswith("DEMO DATA")
    out = json.loads(data["raw_output"])                        # the model's raw text, kept as it was
    assert set(out) >= {"proposed_evidence", "missing_evidence", "conflicts", "recommended_next_step", "reviewer_note_draft"}
    assert isinstance(data["validation_ok"], bool) and data["documents"], data.get("documents")
    assert data["checks"] and all(set(c) >= {"check", "ok"} for c in data["checks"])
    # a recording that failed a check says which one and why; it is kept as it came back, never re-run to get green
    assert data["validation_ok"] == all(c["ok"] for c in data["checks"])
    assert all(c.get("detail") for c in data["checks"] if not c["ok"])


def test_the_fixture_holds_no_sensitive_looking_value():
    from tools.redact import scan_text
    # the one kind allowed is the synthetic street address of the demo document (exporter SCAN_EXCEPTIONS: copilot/fixtures/**)
    assert {kind for _, kind in scan_text(FIXTURE.read_text(encoding="utf-8"))} <= {"street_address"}


# --- the page ---------------------------------------------------------------------------------------------------------
def _page(monkeypatch, tmp_path=None):
    _env(monkeypatch)
    app = create_app()
    from copilot.web.routes_disputes import get_client
    app.dependency_overrides[get_client] = lambda: NoPayPal()
    web = authenticated_client(app, follow_redirects=False)
    response = web.get(f"/replay/{CASE}")
    return response, web, app


def test_the_page_shows_replay_metadata_readiness_and_not_running(monkeypatch):
    response, _, _ = _page(monkeypatch)
    assert response.status_code == 200
    dom = parse(response.text)
    label = dom.find(data_state="replay-label")
    assert label is not None
    assert "Legacy synthetic replay fixture; not running now." in label.text()
    assert "qwen3.5:9b" in label.text()
    assert dom.find("h1").text() == "Legacy synthetic replay"
    readiness = dom.find(data_state="replay-readiness")
    assert readiness is not None
    assert "Candidate fixture SHA-256:" in readiness.text()
    assert "legacy, not independently verified" in readiness.text()
    assert "Provenance readiness: unavailable" in readiness.text()
    assert "Privacy review readiness: unavailable" in readiness.text()
    assert "Oct 29–30 provenance freeze: future gate" in readiness.text()
    assert dom.find(data_state="replay-scope") is not None            # says what this page is not (no package, no send)


def test_the_page_shows_the_recorded_statements_and_the_checked_result(monkeypatch):
    out = json.loads(_fixture()["raw_output"])
    claims = [c for ev in out["proposed_evidence"] for c in ev["claims"]]
    response, _, _ = _page(monkeypatch)
    dom = parse(response.text)
    shown = dom.find_all("li", data_state="replay-claim")
    assert len(shown) == len(claims) and claims
    assert [unescape(n.find(data_state="replay-claim-text").text()) for n in shown] == [c["text"] for c in claims]
    assert {n.attrs["data-basis"] for n in shown} <= {"verified", "seller_statement", "buyer_statement", "model_interpretation"}
    assert dom.find(data_state="replay-next-step") is not None and dom.find(data_state="replay-checks") is not None
    data = _fixture()
    checks = dom.find_all("li", data_state="replay-check")
    assert [n.attrs["data-ok"] for n in checks] == ["yes" if c["ok"] else "no" for c in data["checks"]]
    assert (dom.find(data_state="replay-failed") is None) == data["validation_ok"]
    if not data["validation_ok"]:
        # what the app does after a failed check (auto_prep: _output is None, so no statement is stored)
        assert "keeps none of its statements" in dom.find(data_state="replay-failed").text()
    for c in data["checks"]:
        for line in c.get("detail", []):
            assert unescape(line) in unescape(response.text), line


def test_nothing_on_the_page_can_be_sent_or_enter_a_package(monkeypatch):
    response, _, _ = _page(monkeypatch)
    html = response.text
    assert "<form" not in html and "<button" not in html and 'method="post"' not in html.lower()
    assert not re.search(r"/packages|/approve|/send|/draft/model|/upload", html)
    assert "Approve" not in html and "Send " not in html


def test_the_page_follows_the_copy_rules_and_escapes_the_recorded_text(monkeypatch):
    from copilot.features.replay import service
    data = _fixture()
    data["raw_output"] = json.dumps({**json.loads(data["raw_output"]), "reviewer_note_draft": "<script>alert(1)</script> note"})
    out = json.loads(data["raw_output"])
    out["proposed_evidence"][0]["claims"][0]["text"] = "<b>bold</b> claim"
    data["raw_output"] = json.dumps(out)
    monkeypatch.setattr(service, "load", lambda case: data if case == CASE else None)
    response, _, _ = _page(monkeypatch)
    assert "<script>alert(1)</script>" not in response.text and "<b>bold</b>" not in response.text
    assert "&lt;b&gt;bold&lt;/b&gt; claim" in response.text
    plain = unescape(re.sub(r"<[^>]+>", " ", response.text[response.text.index("<main"):response.text.index("</main>")]))
    assert not re.search(r"\b(we|our|us|please)\b", plain, re.I)


def test_an_unknown_case_is_not_found_and_a_malformed_one_never_touches_the_disk(monkeypatch):
    _env(monkeypatch)
    app = create_app()
    web = authenticated_client(app, follow_redirects=False)
    assert web.get("/replay/PP-R-NOPE-9999999").status_code == 404
    for bad in ("..%2f..%2fconfig", "a.json", "pp-r-demo-0000001", "%00", "PP-R-DEMO-0000001%2f..%2fx"):
        assert web.get(f"/replay/{bad}").status_code in (400, 404, 422, 307, 303), bad


# --- the gate ---------------------------------------------------------------------------------------------------------
def test_the_page_stays_behind_the_access_code(monkeypatch):
    _env(monkeypatch)
    app = create_app()
    anon = _anon(app)
    for method in ("get", "head"):
        refused = getattr(anon, method)(f"/replay/{CASE}")
        assert refused.status_code == 303 and refused.headers["location"].startswith("/access?next="), method
    assert anon.post(f"/replay/{CASE}", data={}).status_code == 401
    assert anon.get("/replay/%2e%2e/disputes").status_code in (303, 404)
    assert authenticated_client(app, follow_redirects=False).get(f"/replay/{CASE}").status_code == 200      # with the code it opens


def test_flag_off_or_local_mode_has_no_replay(monkeypatch):
    _env(monkeypatch, flag=False)
    assert _anon(create_app()).get(f"/replay/{CASE}", follow_redirects=False).status_code in (303, 404)
    _env(monkeypatch, hosted=False, flag=True)
    web = TestClient(create_app(), base_url="http://localhost", client=("127.0.0.1", 12345), follow_redirects=False)
    assert web.get(f"/replay/{CASE}").status_code == 404                          # the flag alone does nothing locally


# --- links ------------------------------------------------------------------------------------------------------------
def test_the_inbox_links_to_it_in_hosted_mode_with_the_flag_only_and_the_access_page_does_not(monkeypatch):
    from copilot.web.routes_disputes import get_client

    class Inbox:
        def call(self, method, path, op, **kw):
            if op == "list_disputes":
                return {"items": [], "links": []}
            raise AssertionError(op)

    def app_for(**kw):
        _env(monkeypatch, **kw)
        app = create_app()
        app.dependency_overrides[get_client] = lambda: Inbox()
        return app

    app = app_for()
    assert 'data-state="replay-link"' not in _anon(app).get("/access").text         # the only public surfaces stay as they were
    link = parse(authenticated_client(app, follow_redirects=False).get("/disputes").text).find(data_state="replay-link")
    assert link is not None and link.find("a").attrs["href"] == f"/replay/{CASE}"
    assert "legacy synthetic replay example" in link.text()
    assert "not a recording of a model run" in link.text()
    assert "not running now" not in link.text()
    app = app_for(flag=False)
    assert 'data-state="replay-link"' not in _anon(app).get("/access").text
    assert 'data-state="replay-link"' not in authenticated_client(app, follow_redirects=False).get("/disputes").text
    app = app_for(hosted=False)
    web = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345), follow_redirects=False)
    assert 'data-state="replay-link"' not in web.get("/disputes").text


# --- what the replay must not do --------------------------------------------------------------------------------------
def test_the_model_call_sites_still_refuse_in_hosted_mode_with_the_flag_on(monkeypatch):
    _env(monkeypatch)
    with pytest.raises(RuntimeError, match="switched off in the hosted demo"):
        config.refuse_model_if_hosted()
    from copilot.core import evidence_model
    with pytest.raises(RuntimeError, match="switched off in the hosted demo"):
        evidence_model.propose_package({}, {}, {})


def test_viewing_the_replay_writes_no_database_row(monkeypatch, tmp_path):
    import sqlite3
    from copilot.hosted_ephemeral import create_app as create_ephemeral_app
    _env(monkeypatch)
    monkeypatch.setenv("COPILOT_EPHEMERAL_ROOT", str(tmp_path))
    # create_app() of the ephemeral factory writes these two variables straight into os.environ; registering them with
    # monkeypatch first makes teardown restore them, so later tests do not inherit this test's session folder.
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("COPILOT_FEATURE_OUTCOME_APPEAL", "false")
    monkeypatch.setattr(config, "_db_path_override", None)
    monkeypatch.setattr(config, "_uploads_dir_override", None)
    app = create_ephemeral_app()
    web = authenticated_client(app, follow_redirects=False)
    assert web.get(f"/replay/{CASE}").status_code == 200
    with sqlite3.connect(config.db_path()) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    assert tables and all(n == 0 for n in counts.values()), counts

def test_data_only_recut_is_allowlisted_partial_readonly_and_stays_behind_access_code(monkeypatch):
    import hashlib
    from copilot.features.replay import service

    _env(monkeypatch)
    app = create_app()
    from copilot.web.routes_disputes import get_client
    app.dependency_overrides[get_client] = lambda: NoPayPal()
    case = service.DATA_ONLY_CASE
    assert _anon(app).get(f"/replay/{case}").status_code == 303
    web = authenticated_client(app, follow_redirects=False)
    response = web.get(f"/replay/{case}")
    assert response.status_code == 200
    html = response.text
    dom = parse(html)
    assert dom.find("h1").text() == "DR4 data-only recut"
    assert "Synthetic local data-only evidence; not running now. This is not a model replay." in dom.find(data_state="replay-label").text()
    assert "Final W3-4 admission: OFF" in dom.find(data_state="replay-admission").text()
    assert "no eligible dispute is evidenced in this recut" in dom.find(data_state="replay-admission").text()

    stages = dom.find_all("li", data_state="replay-stage")
    assert [(node.attrs["data-stage"], node.attrs["data-status"]) for node in stages] == [
        ("Stage1", "observed"), ("Stage2", "observed"), ("Stage3", "observed"),
        ("Stage4", "not_run"), ("Stage5", "not_run"),
    ]
    assert "no upload route or browser upload is evidenced" in html
    assert "identifiers and document bodies are omitted" in html
    assert "source ZIP itself has no verified redaction review" in html
    assert "0 external hosts" in html
    assert "This static view performs no model call, send, PayPal read-back, or write." in html
    assert "<form" not in html and "<button" not in html and 'method="post"' not in html.lower()
    assert not re.search(r"/packages|/approve|/send|/draft/model|/upload", html)

    payload = (service.FIXTURE_DIR / service.DATA_ONLY_FILENAME).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == service.DATA_ONLY_SHA256
    data = json.loads(payload.decode("utf-8"))
    assert data["privacy"]["identifiers_included"] is False
    assert data["privacy"]["bodies_included"] is False
    assert data["privacy"]["source_payload_redaction_verified"] is False
    assert data["admission"]["status"] == "off_no_eligible_dispute_evidenced"
    assert data["run_observations"]["fake_writes"] == 0
    assert data["run_observations"]["actual_external_hosts"] == 0

def test_data_only_loader_binds_to_git_index_bytes_and_pin():
    import hashlib
    import subprocess
    from copilot.features.replay import service

    fixture = service.FIXTURE_DIR / service.DATA_ONLY_FILENAME
    relative = fixture.relative_to(ROOT).as_posix()
    working_bytes = fixture.read_bytes()
    try:
        index_bytes = subprocess.check_output(["git", "show", f":{relative}"], cwd=ROOT, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        index_bytes = working_bytes   # an exported kit has no Git index; the pin and the file name below still bind the bytes
    digest = hashlib.sha256(index_bytes).hexdigest()
    assert working_bytes == index_bytes
    assert fixture.name == f"dr4-dataonly-{digest}.json" == service.DATA_ONLY_FILENAME
    assert digest == service.DATA_ONLY_SHA256

    loaded = service.load(service.DATA_ONLY_CASE)
    assert loaded is not None
    assert loaded.pop("_loaded_candidate_sha256") == digest
    assert loaded == json.loads(index_bytes.decode("utf-8"))

