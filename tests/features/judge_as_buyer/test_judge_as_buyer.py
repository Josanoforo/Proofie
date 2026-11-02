import json
from datetime import datetime, timezone

from copilot.features.judge_as_buyer import service


def test_guide_has_seven_human_steps_and_sandbox_links():
    assert len(service.GUIDE) == 7
    assert service.GUIDE[0]["url"] == "https://www.sandbox.paypal.com/"
    assert service.GUIDE[1]["url"] == "https://www.sandbox.paypal.com/disputes/"
    assert "yourself" in service.GUIDE[4]["text"].lower()


def test_seed_capture_ids_are_explicit_bounded_and_exact(monkeypatch, tmp_path):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    ids = service.save_capture_ids("DEMOTXN0000000001\nDEMOTXN0000000002")
    assert ids == ["DEMOTXN0000000001", "DEMOTXN0000000002"]
    assert service.load_capture_ids() == ids
    assert service.marker_for_norm({"seller_transaction_ids": ["OTHER000000000001"], "create_time": "2026-01-01T00:00:00Z"}) is None


def test_queue_marker_requires_exact_seed_id_and_uses_code_derived_elapsed(monkeypatch, tmp_path):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(service.config, "db_path", lambda: tmp_path / "missing.db")
    service.save_capture_ids("DEMOTXN0000000001")
    marker = service.marker_for_norm({"dispute_id": "PP-R-TST-0000001", "seller_transaction_ids": ["DEMOTXN0000000001"],
                                      "create_time": "2026-10-25T10:00:00Z"},
                                    now=datetime(2026, 10, 25, 10, 4, 12, tzinfo=timezone.utc))
    assert marker["opened_by_buyer"] is True
    assert marker["readback"] is False
    assert marker["elapsed"] == 252
    assert marker["label"].endswith("00:04:12; waiting for send read-back")


def test_readback_timer_uses_confirmed_send_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(service.config, "db_path", lambda: tmp_path / "missing.db")
    service.save_capture_ids("DEMOTXN0000000001")
    db = tmp_path / "proofie.db"
    conn = __import__("sqlite3").connect(db)
    conn.execute("CREATE TABLE send_attempts (dispute_id TEXT, state TEXT, readback_json TEXT)")
    conn.execute("INSERT INTO send_attempts VALUES (?, ?, ?)",
                 ("PP-R-TST-0000001", "confirmed", json.dumps({"read_at": "2026-10-25T10:02:00+00:00"})))
    conn.commit()
    conn.close()
    assert service._readback_at(db, "PP-R-TST-0000001") == "2026-10-25T10:02:00+00:00"
    marker = service.marker_for_norm({"dispute_id": "PP-R-TST-0000001", "seller_transaction_ids": ["DEMOTXN0000000001"],
                                      "create_time": "2026-10-25T10:00:00Z"}, db_path=db,
                                    now=datetime(2026, 10, 25, 10, 4, tzinfo=timezone.utc))
    assert marker["readback"] is True
    assert marker["label"] == "Elapsed from dispute opening to PayPal read-back: 00:02:00"


def test_lazy_registration_and_queue_template_include():
    from pathlib import Path
    app = Path("copilot/app.py").read_text(encoding="utf-8")
    block = app.split('feature_enabled("judge_as_buyer")', 1)[1]
    assert "from copilot.features.judge_as_buyer.routes" in block
    template = Path("copilot/web/templates/disputes_list.html").read_text(encoding="utf-8")
    assert 'feature_enabled("judge_as_buyer")' in template
    assert 'features/judge_as_buyer/_queue_marker.html' in template


def test_guide_template_contains_human_only_seven_step_list():
    from pathlib import Path
    template = Path("copilot/web/templates/features/judge_as_buyer/index.html").read_text(encoding="utf-8")
    assert "Open the case as the sandbox buyer" in template
    assert "{% for step in guide %}" in template
    assert "Proofie does not sign in" in template
    assert "does not verify a PayPal account" in template


def test_route_renders_seven_steps_directly_without_socket_or_network(monkeypatch):
    import inspect
    import re
    from pathlib import Path
    from starlette.requests import Request
    from copilot.features.judge_as_buyer import routes
    from copilot.web.routes_disputes import templates

    class FakeClient:
        def __init__(self):
            self.calls = []

        def call(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("judge_as_buyer must not make provider calls")

    fake_client = FakeClient()

    monkeypatch.setattr(service, "load_capture_ids", lambda: [])
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/features/judge_as_buyer",
             "raw_path": b"/features/judge_as_buyer", "query_string": b"", "root_path": "",
             "headers": [], "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 8000),
             "app": object()}
    response = routes.page(Request(scope))
    body = response.body.decode("utf-8")
    ordered_list = re.search(r"<ol>(.*?)</ol>", body, re.DOTALL)
    assert response.status_code == 200 and ordered_list
    assert ordered_list.group(1).count("<li>") == 7
    assert "Open the case as the sandbox buyer" in body
    source = inspect.getsource(routes.page).casefold()
    assert "client.call" not in source and "httpx" not in source and "requests" not in source
    monkeypatch.setattr(service, "load_capture_ids", lambda: ["DEMOTXN0000000001"])
    rendered_marker = templates.env.get_template("features/judge_as_buyer/_queue_marker.html").render(
        row={"norm": {"dispute_id": "PP-SYNTHETIC-1", "seller_transaction_ids": ["DEMOTXN0000000001"],
                      "create_time": "2026-10-25T10:00:00Z"}})
    other_marker = templates.env.get_template("features/judge_as_buyer/_queue_marker.html").render(
        row={"norm": {"dispute_id": "PP-SYNTHETIC-2", "seller_transaction_ids": ["OTHER00000000001"],
                      "create_time": "2026-10-25T10:00:00Z"}})
    assert 'data-state="judge-opened-by-buyer"' in rendered_marker
    assert 'data-state="judge-buyer-elapsed"' in rendered_marker
    assert re.search(r"\d{2}:\d{2}:\d{2}; waiting for send read-back", rendered_marker)
    assert "judge-opened-by-buyer" not in other_marker
    assert fake_client.calls == []


def test_readme_opens_with_for_judges_and_says_held_out_beside_the_figure():
 from pathlib import Path
 text=(Path(__file__).resolve().parents[3]/'README.md').read_text(encoding='utf-8')
 head=text.split('**Why it exists.**')[0]
 assert '**For judges.**' in head and 'tools/judge_check.py' in head and 'Check your setup' in head
 assert 'held-out measurement dated 2026-10-21' in text and 'frozen held-out synthetic cases' in text


def test_capture_ids_come_from_the_seed_state_file_without_a_paste(monkeypatch, tmp_path):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    state = tmp_path / "state.json"
    monkeypatch.setenv(service.SEED_STATE_ENV, str(state))
    assert service.load_capture_ids() == []                                    # no state yet
    state.write_text(json.dumps({"schema": 1, "capture": "DEMOTXN0000000001", "capture2": "DEMOTXN0000000002"}), encoding="utf-8")
    assert service.load_capture_ids() == ["DEMOTXN0000000001", "DEMOTXN0000000002"]
    norm = {"seller_transaction_ids": ["DEMOTXN0000000002"], "create_time": "2026-01-01T00:00:00Z"}
    assert service.marker_for_norm(norm) is not None
    service.save_capture_ids("DEMOTXN0000000009")                              # a saved id wins over the state file
    assert service.load_capture_ids() == ["DEMOTXN0000000009"]


def test_a_broken_or_foreign_seed_state_file_adds_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    state = tmp_path / "state.json"
    monkeypatch.setenv(service.SEED_STATE_ENV, str(state))
    for text in ("{not json", "[]", json.dumps({"capture": "short"}), json.dumps({"capture": 5})):
        state.write_text(text, encoding="utf-8")
        assert service.load_capture_ids() == []
