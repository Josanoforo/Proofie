"""One command to start Proofie for a judge (docs/JUDGE-RUN.md, "Check your setup").

    python tools/judge_start.py                    # checks the environment, starts the app, opens the browser on your case
    python tools/judge_start.py --state <seed.json> --port 8000 --no-browser

It does what sections 4 and "Check your setup" ask in order, in one command: (1) the Python version and virtual environment,
(2) the sandbox credentials in the settings file (present or not; never their values), (3) starts `python -m copilot.app`
(or uses the app already answering on the port; `--once` stops it again after the check), (4) runs the setup check and prints its table, (5) opens the browser on the
case the setup script seeded (`--state` names its state file) or, without one, on the first dispute waiting for your response,
or on the inbox when there is none. It sends only GET requests to the local app and writes nothing to PayPal. The app stops
when you press Ctrl+C here."""
from __future__ import annotations

import argparse
import json
import re
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge_check as jc  # noqa: E402

ROOT = jc.ROOT
INBOX = "/disputes"


def read_seeded_dispute(state_file) -> str | None:
    """The dispute id the setup script saved in its state file (key "dispute"), or None when it is absent or unreadable."""
    if not state_file:
        return None
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("dispute") if isinstance(data, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"PP-R-[A-Za-z0-9-]+", value) else None


def pick_case(client, preferred: str | None) -> str | None:
    """The seeded case if the inbox lists it, else the first dispute waiting for the seller in the Inquiry stage, else None."""
    try:
        status, inbox = client.get(INBOX)
    except OSError:
        return None
    if status != 200:
        return None
    listed = jc.all_cases(inbox)
    if preferred and preferred in listed:
        return preferred
    for dispute in jc.open_cases(inbox):
        try:
            code, page = client.get(f"{INBOX}/{dispute}")
        except OSError:
            continue
        if code == 200 and re.search(r'data-state="stage">\s*' + jc.OPEN_STAGE, page):
            return dispute
    return None


def target_path(dispute: str | None) -> str:
    return f"{INBOX}/{dispute}" if dispute else INBOX


def run(argv=None, *, root: Path = ROOT, start=jc.start_app, client=None, opener=webbrowser.open, hold=None,
        environ=None, python_row=jc.check_python, out=print) -> int:
    ap = argparse.ArgumentParser(description="Check, start and open Proofie in one command (GET requests to the local app only).")
    ap.add_argument("--port", type=int, default=jc.DEFAULT_PORT, help="port of the local app (default 8000)")
    ap.add_argument("--state", help="the setup script's state file; its dispute is the case that opens")
    ap.add_argument("--no-browser", action="store_true", help="print the address instead of opening the browser")
    ap.add_argument("--once", action="store_true", help="check, print the address and stop the app again (for scripts)")
    args = ap.parse_args(argv)
    environ = jc.os.environ if environ is None else environ
    client = client or jc.HttpClient(f"http://127.0.0.1:{args.port}")

    first = python_row()
    if first.result == jc.FAIL:
        out(jc.render([first]))
        out("Not started: " + first.fix)
        return 1
    creds = jc.check_credentials(jc.read_pairs(root / ".env"), environ, (root / ".env").exists())
    if creds.result == jc.FAIL:
        out(jc.render([creds]))
        out("Not started: " + creds.fix)
        return 1

    proc, started = None, False
    try:
        try:
            client.get(INBOX)
        except OSError:
            proc = start(root, args.port)
            started = True
            if not jc.wait_for(client):
                out(f"The app did not answer on port {args.port}. Run it by hand to see why: python -m copilot.app")
                return 1
        rows = jc.run_checks(root, client, environ=environ, start=None, port=args.port)
        out(jc.render(rows))
        out(jc.verdict(rows))
        dispute = pick_case(client, read_seeded_dispute(args.state))
        url = f"http://127.0.0.1:{args.port}{target_path(dispute)}"
        out(("Your case: " if dispute else "No open case yet, the inbox: ") + url)
        if not args.no_browser:
            opener(url)
        if started and hold is not None:
            hold(proc)
        elif started and not args.once:
            out("The app is running. Press Ctrl+C here to stop it.")
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
        return 0 if all(r.result == jc.PASS for r in rows) else 1
    finally:
        if started and proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.path.insert(0, str(ROOT))
    sys.exit(run())
