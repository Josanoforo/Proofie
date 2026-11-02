"""Offline tests for tools/hosts_contacted.py (local sockets only)."""
import socket
import subprocess
import sys
from pathlib import Path

import hosts_contacted as hc

TOOL = str(Path(hc.__file__))


def _tool(*child):
    return subprocess.run([sys.executable, TOOL, "--", *child], capture_output=True, text=True)


def test_local_connection_is_recorded():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        r = _tool(sys.executable, "-c", f"import socket; socket.create_connection(('127.0.0.1', {port})).close()")
    finally:
        srv.close()
    assert r.returncode == 0
    assert f"127.0.0.1:{port}" in r.stdout.splitlines()


def test_child_without_network_records_nothing():
    r = _tool(sys.executable, "-c", "print('quiet')")
    assert r.returncode == 0
    lines = r.stdout.splitlines()
    assert lines[lines.index("HOSTS CONTACTED") + 1:] == ["exit code: 0"]


def test_output_is_one_host_port_per_line_sorted_unique():
    code = "import socket\nfor h in ('localhost','127.0.0.1','localhost'):\n  try: socket.getaddrinfo(h, 9)\n  except OSError: pass"
    r = _tool(sys.executable, "-c", code)
    hosts = r.stdout.splitlines()[1:-1]
    assert hosts == ["127.0.0.1:9", "localhost:9"]
    assert all(h.count(":") == 1 for h in hosts)


def test_exit_code_mirrors_child():
    r = _tool(sys.executable, "-c", "raise SystemExit(7)")
    assert r.returncode == 7
    assert r.stdout.splitlines()[-1] == "exit code: 7"


def test_missing_command_is_usage_error():
    r = subprocess.run([sys.executable, TOOL], capture_output=True, text=True)
    assert r.returncode == 2


def test_a_recorder_that_never_loads_exits_3_with_the_stderr_line_whatever_the_child_returns():
    # -I (isolated mode) ignores PYTHONPATH, so the child's startup never imports the recorder's sitecustomize
    r = _tool(sys.executable, "-I", "-c", "print('child ran')")
    assert r.returncode == 3
    assert "RECORDER NOT LOADED" in r.stderr
    assert "exit code" not in r.stdout


def test_a_normal_child_keeps_its_exit_code_and_the_marker_never_shows_in_stdout():
    r = _tool(sys.executable, "-c", "raise SystemExit(5)")
    assert r.returncode == 5 and "RECORDER NOT LOADED" not in r.stderr
    assert "#recorder-loaded" not in r.stdout and hc.MARKER not in r.stdout
    assert r.stdout.splitlines() == ["HOSTS CONTACTED", "exit code: 5"]


class _FakeChild:
    """A Popen stand-in: writes the recorder's marker and one host line through PROOFIE_HOSTS_LOG, like the sitecustomize
    of a running app would, and whose first wait() is the user's Ctrl+C."""

    def __init__(self, cmd, env=None, stop_by_itself=True):
        self.cmd, self.waits, self.terminated, self.killed = cmd, 0, False, False
        self.stop_by_itself = stop_by_itself
        with open(env["PROOFIE_HOSTS_LOG"], "a", encoding="utf-8") as fh:
            fh.write(hc.MARKER + "\n127.0.0.1:8000\n")

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits <= 2:                                          # the app is still running: the polling loop spins
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if self.waits == 3:                                          # then the user's Ctrl+C
            raise KeyboardInterrupt
        if not self.stop_by_itself and not self.terminated and not self.killed:   # does not stop inside the grace time
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return 130 if self.stop_by_itself else -15

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_ctrl_c_while_the_child_runs_still_gives_the_list_the_marker_and_the_childs_code(monkeypatch, capsys):
    made = []
    monkeypatch.setattr(hc.subprocess, "Popen", lambda cmd, env=None: made.append(_FakeChild(cmd, env)) or made[-1])
    assert hc.run(["app"]) == (["127.0.0.1:8000"], 130, True)
    assert made[0].waits == 4 and not made[0].terminated            # two polls, the Ctrl+C, then the child ended by itself
    assert hc.main(["--", "app"]) == 130
    assert capsys.readouterr().out.splitlines() == ["HOSTS CONTACTED", "127.0.0.1:8000", "exit code: 130"]


def test_ctrl_c_with_a_child_that_does_not_stop_terminates_it_after_the_grace_time(monkeypatch):
    made = []
    monkeypatch.setattr(hc, "INTERRUPT_GRACE_SECONDS", 0)
    monkeypatch.setattr(hc.subprocess, "Popen", lambda cmd, env=None: made.append(_FakeChild(cmd, env, stop_by_itself=False)) or made[-1])
    hosts, code, loaded = hc.run(["app"])
    assert (hosts, code, loaded) == (["127.0.0.1:8000"], -15, True)
    assert made[0].terminated and not made[0].killed


def test_out_file_is_written_by_the_tool_itself_after_ctrl_c(monkeypatch, tmp_path, capsys):
    made = []
    monkeypatch.setattr(hc.subprocess, "Popen", lambda cmd, env=None: made.append(_FakeChild(cmd, env)) or made[-1])
    out = tmp_path / "hosts.txt"
    assert hc.main(["--out", str(out), "--", "app"]) == 130
    assert out.read_text(encoding="utf-8").splitlines() == ["HOSTS CONTACTED", "127.0.0.1:8000", "exit code: 130"]
    assert capsys.readouterr().out.splitlines() == ["HOSTS CONTACTED", "127.0.0.1:8000", "exit code: 130"]


def test_out_with_a_real_child_and_a_bad_option_is_a_usage_error(tmp_path):
    out = tmp_path / "h.txt"
    r = subprocess.run([sys.executable, TOOL, "--out", str(out), "--", sys.executable, "-c", "raise SystemExit(4)"],
                       capture_output=True, text=True)
    assert r.returncode == 4 and out.read_text(encoding="utf-8").splitlines() == ["HOSTS CONTACTED", "exit code: 4"]
    assert subprocess.run([sys.executable, TOOL, "--bogus", "--", "x"], capture_output=True, text=True).returncode == 2
