"""A self-check of your Proofie setup (docs/JUDGE-RUN.md, "Check your setup").

    python tools/judge_check.py            # checks, starts the app if it is not running, stops it again
    python tools/judge_check.py --port 8030 --no-start

It reads your own settings file and sends GET requests to the local app only. It never writes to PayPal, never needs
a browser, and prints only whether a setting is present, never its value. Exit code 0 only when every row passes.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8000
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
CREDENTIALS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
OPEN_STATUS = "Waiting for your response"
OPEN_STAGE = "Inquiry"
GUIDE_OPEN_CASE = "docs/JUDGE-RUN.md section 3 (create a sandbox payment and open an 'Item not received' dispute, then wait a few minutes)"
VIEWS = (("prepare", "Prepare"), ("review", "Review claims"), ("approve", "Approve package"), ("send", "Send and check"),
         ("records", "Case records"), ("other", "Other responses"), ("sale", "Sale records"))
EXTRA_PAGES = (("history", "Case history"), ("ledger", "What this app recorded"), ("outcome", "What PayPal has recorded"),
               ("resolution", "Resolution options and buyer messages"), ("draft", "Draft a message to the buyer"),
               ("triage", "What the evidence supports"))


class Row:
    def __init__(self, step, expected, seen, result, fix=""):
        self.step, self.expected, self.seen, self.result, self.fix = step, expected, seen, result, fix


# ------------------------------------------------------------------------------------------------ settings files
def read_pairs(path: Path) -> dict:
    """KEY=value lines of a settings file; comments and blanks skipped. Values stay in memory, never printed."""
    pairs = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return pairs
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            pairs[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return pairs


def is_placeholder(value: str) -> bool:
    return not value or value.startswith("your-") or value.endswith("-here")


# ------------------------------------------------------------------------------------------------------ the pages
def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", "", html))).strip()


def heading_of(html: str) -> str:
    """Title, h1 and h2 text, the places a page names itself."""
    parts = [text_of(p) for p in re.findall(r"(?s)<(?:title|h1|h2)[^>]*>(.*?)</(?:title|h1|h2)>", html)]
    return " | ".join(dict.fromkeys(parts))


def open_cases(inbox_html: str) -> list:
    """Dispute ids of inbox rows that read 'Waiting for your response'."""
    found = []
    for m in re.finditer(r'(?s)<tr data-dispute="([^"]+)">(.*?)</tr>', inbox_html):
        if OPEN_STATUS in text_of(m.group(2)):
            found.append(m.group(1))
    return found


def all_cases(inbox_html: str) -> list:
    return re.findall(r'<tr data-dispute="([^"]+)">', inbox_html)


# --------------------------------------------------------------------------------------------------------- client
class HttpClient:
    """GET only, to the local app. `get` returns (status, text) or raises OSError when nothing answers."""
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def get(self, path: str):
        import httpx
        try:
            r = httpx.get(self.base + path, timeout=60, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise OSError(type(exc).__name__) from None
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location", "").startswith("/"):
            return self.get(r.headers["location"])
        return r.status_code, r.text


def start_app(root: Path, port: int):
    """JUDGE-RUN's command, `python -m copilot.app` (port 8000). Another port runs the same app object on that port."""
    if port == DEFAULT_PORT:
        cmd = [sys.executable, "-m", "copilot.app"]
    else:
        cmd = [sys.executable, "-c", "import uvicorn; from copilot.app import app; "
               f"uvicorn.run(app, host='127.0.0.1', port={int(port)})"]
    return subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(client, seconds: float = 40.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            client.get("/disputes")
            return True
        except OSError:
            time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------------------------------------- checks
def check_python(version=None, prefix=None, base_prefix=None) -> Row:
    version = version or sys.version_info[:3]
    in_venv = (prefix or sys.prefix) != (base_prefix or sys.base_prefix)
    seen = f"Python {version[0]}.{version[1]}.{version[2]}, {'inside a virtual environment' if in_venv else 'not in a virtual environment'}"
    ok = tuple(version[:2]) == (3, 12) and in_venv
    return Row("1 Python and virtual environment", "Python 3.12, run with the .venv interpreter", seen, PASS if ok else FAIL,
               "" if ok else "Create the environment with Python 3.12 (JUDGE-RUN section 1) and run this command with "
                             ".venv's python (Windows: .\\.venv\\Scripts\\python.exe tools/judge_check.py).")


def check_credentials(env_file: dict, environ: dict, env_present: bool) -> Row:
    values = {k: environ.get(k) or env_file.get(k, "") for k in CREDENTIALS}
    missing = [k for k, v in values.items() if is_placeholder(v)]
    if not env_present and not any(environ.get(k) for k in CREDENTIALS):
        return Row("2 Settings file and sandbox credentials", "a .env file with both sandbox credentials set",
                   "no .env file", FAIL, "Copy .env.example to .env and fill in your sandbox Client ID and Secret (JUDGE-RUN section 2, step 4).")
    if missing:
        return Row("2 Settings file and sandbox credentials", "both credentials set, not the placeholders",
                   "not set: " + ", ".join(missing), FAIL, "Open .env and replace the placeholder lines with your own sandbox app's values (JUDGE-RUN section 2).")
    return Row("2 Settings file and sandbox credentials", "both credentials set, not the placeholders",
               "both are set (values not shown)", PASS)


def check_flags(example: dict, env_file: dict, environ: dict) -> Row:
    flags = {k: v for k, v in example.items() if k.startswith("COPILOT_FEATURE_")}
    if not flags:
        return Row("3 Feature settings", "the COPILOT_FEATURE_* lines of .env.example", ".env.example not found", FAIL,
                   "Run this command from the project folder; the folder that holds README.md is the root.")
    differ = sorted(k for k, v in flags.items() if (environ.get(k) if environ.get(k) is not None else env_file.get(k)) is None
                    or str(environ.get(k) if environ.get(k) is not None else env_file.get(k)).lower() != v.lower())
    expected = f"all {len(flags)} COPILOT_FEATURE_* lines as supplied in .env.example"
    if not differ:
        return Row("3 Feature settings", expected, f"all {len(flags)} match", PASS)
    shown = ", ".join(differ[:6]) + (f" and {len(differ) - 6} more" if len(differ) > 6 else "")
    return Row("3 Feature settings", expected, f"{len(differ)} missing or different: {shown}", FAIL,
               "Copy the COPILOT_FEATURE_* lines from .env.example into .env unchanged (JUDGE-RUN section 2, step 4).")


def check_running_flags(example: dict, client) -> Row:
    """The running app's own report (GET /healthz/features: names only) against the COPILOT_FEATURE_* lines of .env.example."""
    import json
    flags = {k: v.strip().lower() for k, v in example.items() if k.startswith("COPILOT_FEATURE_")}
    step, expected = "4b Settings the app runs with", f"the app runs with the {len(flags)} COPILOT_FEATURE_* lines of .env.example"
    try:
        status, text = client.get("/healthz/features")
        report = json.loads(text) if status == 200 else None
    except (OSError, ValueError):
        report = None
    if not flags or not isinstance(report, dict) or not isinstance(report.get("on"), list):
        return Row(step, expected, "the app does not report its settings", SKIP)
    on = set(report["on"])
    differ = sorted(k for k, v in flags.items() if (k in on) != (v == "true"))
    if not differ:
        return Row(step, expected, f"all {len(flags)} match", PASS)
    shown = ", ".join(differ[:6]) + (f" and {len(differ) - 6} more" if len(differ) > 6 else "")
    return Row(step, expected, f"{len(differ)} differ: {shown}", FAIL,
               "Stop the app, copy the COPILOT_FEATURE_* lines from .env.example into .env unchanged and start it again "
               "(JUDGE-RUN section 2, step 4).")


def check_dispute_pages(client, dispute_id: str) -> list:
    rows = []
    base = f"/disputes/{dispute_id}"
    pages = [("overview", base, f"Dispute {dispute_id}")]
    overview_html = ""
    try:
        status, overview_html = client.get(base)
    except OSError:
        overview_html = ""
    pages += [(f"workspace/{v}", f"{base}/workspace/{v}", title) for v, title in VIEWS]
    if "/workspace/hackathon" in overview_html:
        pages.append(("workspace/hackathon", f"{base}/workspace/hackathon", "Hackathon review"))
    htmls = {}
    package = None
    capture = None
    for name, path, heading in pages:
        rows.append(_page_row(client, name, path, heading, htmls))
    for html in htmls.values():
        package = package or re.search(re.escape(base) + r"/packages/(\d+)", html)
        capture = capture or re.search(r'/fulfilment/([A-Za-z0-9]+)(?:[?"/])', html)
    if package:
        rows.append(_page_row(client, "package", f"{base}/packages/{package.group(1)}", "Evidence package", htmls))
    else:
        rows.append(Row("6 package page", "opened if a package exists", "no package yet, so none to open", PASS))
    for name, heading in EXTRA_PAGES:
        rows.append(_page_row(client, name, f"{base}/{name}", heading, htmls))
    if capture:
        rows.append(_page_row(client, "fulfilment", f"/fulfilment/{capture.group(1)}?from_dispute={dispute_id}",
                              "Tracking at the sale", htmls))
    else:
        rows.append(Row("6 fulfilment", "the page of this dispute's sale, 200", "no sale id found on the case pages", FAIL,
                        "Open the case in the app and check it names a sale; if not, the dispute is not linked to a sale of this account."))
    return rows


def _page_row(client, name, path, heading, htmls) -> Row:
    step = f"6 {name}"
    expected = f'200 and "{heading}"'
    try:
        status, html = client.get(path)
    except OSError as exc:
        return Row(step, expected, f"no answer ({exc})", FAIL, "Check the app is still running and try again.")
    htmls[path] = html
    if status != 200:
        if status == 404:
            return Row(step, expected, f"HTTP 404: {heading_of(html)[:80]}", FAIL,
                       "A page that is not found usually means a COPILOT_FEATURE_* line is missing from .env: fix step 3, restart the app, run this again.")
        return Row(step, expected, f"HTTP {status}: {heading_of(html)[:80]}", FAIL,
                   "Reload the page in your browser; if it keeps failing, restart the app and run this check again.")
    if heading.lower() not in heading_of(html).lower():
        return Row(step, expected, f"200 but the heading reads: {heading_of(html)[:60]}", FAIL,
                   "The page loaded but is not the one expected; check you run the project's own copy of the app.")
    return Row(step, expected, "200, heading found", PASS)


def run_checks(root: Path, client, environ=None, start=None, port=DEFAULT_PORT) -> list:
    """`start(root, port)` returns a process (or None); it is stopped before returning."""
    environ = os.environ if environ is None else environ
    env_path = root / ".env"
    env_file = read_pairs(env_path)
    example = read_pairs(root / ".env.example")
    rows = [check_python(), check_credentials(env_file, environ, env_path.exists()),
            check_flags(example, env_file, environ)]
    proc = None
    try:
        reachable, started = True, False
        try:
            client.get("/disputes")
        except OSError:
            reachable = False
        if not reachable and start is not None:
            proc = start(root, port)
            started = True
            reachable = wait_for(client)
        if reachable:
            rows.append(Row("4 App reachable", f"the app answers on port {port}",
                            "answers" + (" (started by this check, stopped at the end)" if started else ""), PASS))
            rows.append(check_running_flags(example, client))
        else:
            rows.append(Row("4 App reachable", f"the app answers on port {port}", "no answer", FAIL,
                            "Start it with: python -m copilot.app (JUDGE-RUN section 4), or run this check without --no-start."))
            rows.append(Row("5 An open dispute", "a dispute waiting for your response, Inquiry stage", "not checked: the app is down", SKIP))
            rows.append(Row("6 Case pages", "every seller page answers 200", "not checked: the app is down", SKIP))
            return rows
        rows.extend(_list_and_walk(client))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    return rows


def _list_and_walk(client) -> list:
    try:
        status, inbox = client.get("/disputes")
    except OSError as exc:
        return [Row("5 An open dispute", "your sandbox disputes are listed", f"no answer ({exc})", FAIL, "Run the check again.")]
    if status != 200:
        return [Row("5 An open dispute", "your sandbox disputes are listed", f"HTTP {status}: {heading_of(inbox)[:80]}", FAIL,
                    "Check the Client ID and Secret in .env belong to a sandbox app (JUDGE-RUN section 2), restart the app and run this again."),
                Row("6 Case pages", "every seller page answers 200", "not checked: no dispute list", SKIP)]
    ids = all_cases(inbox)
    pick = None
    for did in open_cases(inbox):
        try:
            code, page = client.get(f"/disputes/{did}")
        except OSError:
            continue
        if code == 200 and re.search(r'data-state="stage">\s*' + OPEN_STAGE, page):
            pick = did
            break
    if pick is None:
        seen = "no disputes listed yet" if not ids else f"{len(ids)} listed, none waiting for you in the Inquiry stage"
        return [Row("5 An open dispute", f'a dispute "{OPEN_STATUS}", stage {OPEN_STAGE}', seen, SKIP,
                    "This is the next step, not an error: " + GUIDE_OPEN_CASE + ". Then press 'Refresh from PayPal' in the inbox and run this check again."),
                Row("6 Case pages", "every seller page answers 200", "not checked: needs the dispute of step 5", SKIP)]
    rows = [Row("5 An open dispute", f'a dispute "{OPEN_STATUS}", stage {OPEN_STAGE}', f"found: {pick}", PASS)]
    return rows + check_dispute_pages(client, pick)


# --------------------------------------------------------------------------------------------------------- output
def render(rows) -> str:
    widths = (24, 28, 34, 6, 40)
    head = ("Step", "Expected", "Seen", "Result", "How to fix")
    out = []
    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))

    def line(cells):
        cols = [textwrap.wrap(str(c), w) or [""] for c, w in zip(cells, widths)]
        for i in range(max(len(c) for c in cols)):
            out.append("  ".join((c[i] if i < len(c) else "").ljust(w) for c, w in zip(cols, widths)).rstrip())
    line(head)
    out.append(sep)
    for r in rows:
        line((r.step, r.expected, r.seen, r.result, r.fix))
        out.append(sep)
    return "\n".join(out)


MODEL_ABSENT = "absent, manual mode and replay evaluation available"


def model_note(get=None) -> str:
    """The optional local model, reported as information, never as a check that can fail. One short GET."""
    import json
    import urllib.request
    try:
        if get is None:
            def get(url):
                with urllib.request.urlopen(url, timeout=2) as reply:
                    return json.loads(reply.read().decode("utf-8"))
        names = [m.get("name") for m in get("http://127.0.0.1:11434/api/tags").get("models", []) if m.get("name")]
    except (OSError, ValueError, AttributeError):
        names = []
    if names:
        return "Local model (optional): serving at 127.0.0.1:11434, the AI step and the evaluation can use it."
    return f"Local model (optional): {MODEL_ABSENT}."


def verdict(rows) -> str:
    failed = [r for r in rows if r.result == FAIL]
    skipped = [r for r in rows if r.result == SKIP]
    if not failed and not skipped:
        return f"VERDICT: all {len(rows)} checks passed. Your setup is ready."
    first = (failed or skipped)[0]
    return f"VERDICT: {len(failed)} failed, {len(skipped)} not checked. Start with: {first.step}."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check your Proofie setup (GET requests to the local app only).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port of the local app (default 8000)")
    ap.add_argument("--no-start", action="store_true", help="do not start the app if it is not running")
    ap.add_argument("--setup", action="store_true", help="ask for your sandbox credentials (hidden) and write the settings file")
    args = ap.parse_args(argv)
    if args.setup:
        sys.path.insert(0, str(ROOT / "tools"))
        import judge_setup
        return judge_setup.setup(ROOT)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.path.insert(0, str(ROOT))
    rows = run_checks(ROOT, HttpClient(f"http://127.0.0.1:{args.port}"), start=None if args.no_start else start_app,
                      port=args.port)
    print(render(rows))
    print(model_note())
    print(verdict(rows))
    return 0 if not any(r.result == FAIL for r in rows) else 1      # a row not checked yet (SKIP) is a next step, not a failure


if __name__ == "__main__":
    sys.exit(main())
