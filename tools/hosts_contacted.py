"""Runtime recorder: run a command and list every host:port it resolves or connects to.

Usage: python tools/hosts_contacted.py [--out FILE] -- <command ...>

The child gets a temporary sitecustomize.py (via PYTHONPATH) that wraps socket.getaddrinfo and
socket.create_connection and appends "host:port" to the file named by PROOFIE_HOSTS_LOG. Hostnames and
ports only: never a path, header or body. After the child exits the sorted unique list is printed, then
"exit code: N"; this tool exits with the child's code. With --out FILE the tool also writes that same text to FILE
itself (UTF-8), so the list survives a shell that drops redirected output when Ctrl+C ends the session (PowerShell 5.1). The recorder writes a marker line to the log the moment it
loads; if the marker is missing after the child exits (the recorder never loaded or crashed) the tool prints
RECORDER NOT LOADED on stderr and exits 3 whatever the child returned. Ctrl+C (a long-running child such as the
app, stopped by hand): the console sends it to the child as well; the tool waits for the child in a short polling loop (a blocking
wait is not interrupted by Ctrl+C on Windows), then waits up to INTERRUPT_GRACE_SECONDS for the
child to exit by itself, then terminates it, and still prints the list and the exit code. Only Python children are recorded; a raw
socket.connect() to a literal address that bypasses getaddrinfo is not seen (stated limit)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = "#recorder-loaded"
INTERRUPT_GRACE_SECONDS = 20
POLL_SECONDS = 0.2

SITECUSTOMIZE = r'''\
import os
import socket

_log = os.environ.get("PROOFIE_HOSTS_LOG")
if _log:
    try:
        with open(_log, "a", encoding="utf-8") as _fh:
            _fh.write("#recorder-loaded\n")
    except OSError:
        pass


def _record(host, port):
    if not _log or not isinstance(host, (str, bytes)):
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        with open(_log, "a", encoding="utf-8") as fh:
            fh.write(f"{host}:{port}\n")
    except OSError:
        pass


_orig_getaddrinfo = socket.getaddrinfo
_orig_create_connection = socket.create_connection


def _getaddrinfo(host, port, *args, **kwargs):
    _record(host, port)
    return _orig_getaddrinfo(host, port, *args, **kwargs)


def _create_connection(address, *args, **kwargs):
    _record(address[0], address[1])
    return _orig_create_connection(address, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo
socket.create_connection = _create_connection
'''


def _wait(child):
    """Wait for the child in a polling loop, so a Ctrl+C is seen as KeyboardInterrupt on Windows too."""
    while True:
        try:
            return child.wait(timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            continue


def _stop(child):
    """After Ctrl+C: let the child exit by itself for a while, then terminate it, then kill it. Returns its exit code."""
    try:
        return child.wait(timeout=INTERRUPT_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.terminate()
    try:
        return child.wait(timeout=5)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.kill()
        return child.wait()


def run(cmd):
    """Run cmd with the recorder active; return (sorted unique host:port list, child exit code, marker seen)."""
    with tempfile.TemporaryDirectory(prefix="proofie-hosts-") as tmp:
        (Path(tmp) / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        log = Path(tmp) / "hosts.log"
        env = dict(os.environ)
        env["PROOFIE_HOSTS_LOG"] = str(log)
        env["PYTHONPATH"] = tmp + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        child = subprocess.Popen(cmd, env=env)
        try:
            code = _wait(child)
        except KeyboardInterrupt:
            code = _stop(child)
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return sorted({line for line in lines if line and line != MARKER}), code, MARKER in lines


def main(argv):
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print("usage: hosts_contacted.py [--out FILE] -- <command ...>", file=sys.stderr)
        return 2
    options = argv[:argv.index("--")]
    out = None
    if options:
        if len(options) != 2 or options[0] != "--out":
            print("usage: hosts_contacted.py [--out FILE] -- <command ...>", file=sys.stderr)
            return 2
        out = Path(options[1])
    hosts, code, loaded = run(argv[argv.index("--") + 1:])
    if not loaded:
        print("RECORDER NOT LOADED", file=sys.stderr)
        if out:
            out.write_text("RECORDER NOT LOADED\n", encoding="utf-8")
        return 3
    lines = ["HOSTS CONTACTED", *hosts, f"exit code: {code}"]
    if out:
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
