"""tools/judge_start.py: one command that checks, starts the app and opens the browser on the judge's case. No network, no app."""
import json

import pytest

import judge_check as jc
import judge_start as js

OK = jc.Row("1 Python and virtual environment", "x", "ok", jc.PASS)
BAD = jc.Row("1 Python and virtual environment", "x", "old", jc.FAIL, "Use Python 3.12.")
INBOX_ROWS = ('<table><tr data-dispute="PP-R-A-1"><td>Waiting for your response</td></tr>'
              '<tr data-dispute="PP-R-B-2"><td>Waiting for your response</td></tr></table>')


class FakeClient:
    """Down until `up`; then the inbox and the stage of each case."""
    def __init__(self, up=True, inbox=INBOX_ROWS, stages=None):
        self.up, self.inbox, self.stages = up, inbox, stages or {"PP-R-A-1": "Inquiry", "PP-R-B-2": "Inquiry"}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if not self.up:
            raise OSError("down")
        if path == "/disputes":
            return 200, self.inbox
        did = path.rsplit("/", 1)[-1]
        return 200, f'<span data-state="stage">{self.stages.get(did, "Chargeback")}</span>'


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".env.example").write_text("PAYPAL_CLIENT_ID=your-client-id-here\nPAYPAL_CLIENT_SECRET=your-secret-here\n"
                                           "COPILOT_FEATURE_X=true\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PAYPAL_CLIENT_ID=TESTID123\nPAYPAL_CLIENT_SECRET=TESTSECRET456\nCOPILOT_FEATURE_X=true\n",
                                   encoding="utf-8")
    return tmp_path


def _run(root, monkeypatch, client, argv=(), python_row=lambda: OK, start=None, state_rows=None):
    monkeypatch.setattr(jc, "run_checks", lambda *a, **k: state_rows or [jc.Row("4 App reachable", "x", "answers", jc.PASS)])
    printed, opened, procs = [], [], []

    def fake_start(r, port):
        procs.append(FakeProc())
        client.up = True                      # the app answers once started
        return procs[-1]
    code = js.run(list(argv), root=root, start=start or fake_start, client=client, opener=opened.append,
                  hold=lambda proc: None, environ={}, python_row=python_row, out=printed.append)
    return code, printed, opened, procs


def test_a_wrong_python_stops_before_the_app_is_started(root, monkeypatch):
    client = FakeClient(up=False)
    code, printed, opened, procs = _run(root, monkeypatch, client, python_row=lambda: BAD)
    assert code == 1 and procs == [] and opened == [] and any("Not started" in p for p in printed)


def test_missing_credentials_stop_before_the_app_is_started(root, monkeypatch):
    (root / ".env").write_text("COPILOT_FEATURE_X=true\n", encoding="utf-8")
    client = FakeClient(up=False)
    code, printed, opened, procs = _run(root, monkeypatch, client)
    assert code == 1 and procs == [] and opened == [] and any("Not started" in p for p in printed)
    assert not any("TESTSECRET456" in p for p in printed)


def test_starts_the_app_opens_the_first_open_case_and_stops_the_app(root, monkeypatch):
    client = FakeClient(up=False)
    code, printed, opened, procs = _run(root, monkeypatch, client)
    assert code == 0 and len(procs) == 1 and procs[0].terminated
    assert opened == ["http://127.0.0.1:8000/disputes/PP-R-A-1"]


def test_the_seeded_case_wins_when_the_inbox_lists_it(root, monkeypatch, tmp_path):
    state = tmp_path / "seed.json"
    state.write_text(json.dumps({"dispute": "PP-R-B-2"}), encoding="utf-8")
    code, printed, opened, procs = _run(root, monkeypatch, FakeClient(up=False), argv=["--state", str(state)])
    assert opened == ["http://127.0.0.1:8000/disputes/PP-R-B-2"]


def test_a_seeded_case_the_inbox_does_not_list_falls_back_to_the_first_open_case(root, monkeypatch, tmp_path):
    state = tmp_path / "seed.json"
    state.write_text(json.dumps({"dispute": "PP-R-GONE-9"}), encoding="utf-8")
    code, printed, opened, procs = _run(root, monkeypatch, FakeClient(up=False), argv=["--state", str(state)])
    assert opened == ["http://127.0.0.1:8000/disputes/PP-R-A-1"]


def test_with_no_open_case_the_browser_opens_on_the_inbox(root, monkeypatch):
    client = FakeClient(up=False, inbox="<table></table>")
    code, printed, opened, procs = _run(root, monkeypatch, client)
    assert opened == ["http://127.0.0.1:8000/disputes"] and any("No open case yet" in p for p in printed)


def test_an_app_already_running_is_used_and_left_running(root, monkeypatch):
    client = FakeClient(up=True)
    code, printed, opened, procs = _run(root, monkeypatch, client)
    assert procs == [] and opened == ["http://127.0.0.1:8000/disputes/PP-R-A-1"]


def test_an_app_that_never_answers_is_reported_and_stopped(root, monkeypatch):
    client = FakeClient(up=False)
    made = []

    def start(r, port):
        made.append(FakeProc())
        return made[0]                        # stays down
    monkeypatch.setattr(jc, "wait_for", lambda c, seconds=40.0: False)
    code, printed, opened, procs = _run(root, monkeypatch, client, start=start)
    assert code == 1 and opened == [] and made[0].terminated and any("did not answer" in p for p in printed)


def test_no_browser_prints_the_address_and_opens_nothing(root, monkeypatch):
    code, printed, opened, procs = _run(root, monkeypatch, FakeClient(up=False), argv=["--no-browser", "--port", "8064"])
    assert opened == [] and any("http://127.0.0.1:8064/disputes/PP-R-A-1" in p for p in printed)


def test_a_failing_check_row_is_the_exit_code_but_the_browser_still_opens(root, monkeypatch):
    rows = [jc.Row("5 An open dispute", "x", "none", jc.FAIL, "next step")]
    code, printed, opened, procs = _run(root, monkeypatch, FakeClient(up=False, inbox="<table></table>"), state_rows=rows)
    assert code == 1 and opened == ["http://127.0.0.1:8000/disputes"]


@pytest.mark.parametrize("content", ["not json", json.dumps(["x"]), json.dumps({"dispute": "bad id"}), json.dumps({})])
def test_an_unusable_state_file_gives_no_dispute(tmp_path, content):
    f = tmp_path / "s.json"
    f.write_text(content, encoding="utf-8")
    assert js.read_seeded_dispute(f) is None and js.read_seeded_dispute(None) is None
    assert js.read_seeded_dispute(tmp_path / "missing.json") is None


def test_once_stops_the_app_after_printing_the_address_without_waiting(root, monkeypatch):
    class Hanging(FakeProc):
        def wait(self, timeout=None):
            if timeout is None:
                raise AssertionError("--once must not wait for Ctrl+C")
            return 0
    client = FakeClient(up=False)
    monkeypatch.setattr(jc, "run_checks", lambda *a, **k: [jc.Row("4 App reachable", "x", "answers", jc.PASS)])
    procs, opened = [], []

    def start(r, port):
        procs.append(Hanging())
        client.up = True
        return procs[0]
    code = js.run(["--once", "--no-browser"], root=root, start=start, client=client, opener=opened.append,
                  environ={}, python_row=lambda: OK, out=lambda t: None)
    assert code == 0 and procs[0].terminated and opened == []
