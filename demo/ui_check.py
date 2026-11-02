"""Validate the UI in a real browser (Playwright/Chromium) at 1280 and 375 px, light and dark.

Run explicitly:  .venv\\Scripts\\python.exe demo\\ui_check.py
It is NOT part of the default pytest run (it needs a browser and takes a few seconds per page).

What it does and does not touch:
- starts the real app on 127.0.0.1 (random port) with a SCRIPTED CLIENT in place of PayPal: the PayPal answers are
  the hand-made synthetic fixtures in pilot/fixtures/synthetic/ (same JSON shape as real sandbox answers) and the demo documents are the synthetic
  DEMO DATA PDFs in demo/documents/. No request leaves this machine; nothing reaches PayPal (sandbox or live).
- uses a throw-away database and uploads folder in a temp directory, never data/copilot.db.
- the model is never called: the dispute is excluded from automatic preparation and the package is started manually.

What it checks, per page and per configuration (viewport x colour scheme):
- no horizontal page scroll; the colour scheme is really applied (computed page background);
- every control is at least 44 x 44 px;
- every Tab stop shows a focus ring (solid, >= 3 px) in the focus colour of the theme;
and once, in a real browser: the send button locks on submit and unlocks on `pageshow` (persisted), and a double
click on "Send to PayPal" produces exactly ONE request from the browser and ONE provide_evidence call.
Screenshots go to data/ui-captures/d2/ (git-ignored); the machine-readable report to the same folder.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

# Synthetic browser checks must never load a local credential file.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _key in tuple(os.environ):
    if _key.startswith(("PAYPAL_", "OLLAMA_")):
        del os.environ[_key]

def _capture_network_guard(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("UI check blocks external socket connections")

sys.addaudithook(_capture_network_guard)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DID = "PP-R-DEMO-0000001"
os.environ["COPILOT_AUTOPREP_EXCLUDE"] = DID       # never start a background (model) preparation here

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from copilot import config  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.core import send_recovery  # noqa: E402
from copilot.web.routes_disputes import get_client  # noqa: E402

REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
OUT = config.data_dir() / "ui-captures" / "d2"

VIEWPORTS = {"1280": {"width": 1280, "height": 900}, "375": {"width": 375, "height": 800}}
SCHEMES = ("light", "dark")
PAGE_BG = {"light": "rgb(245, 244, 240)", "dark": "rgb(18, 21, 24)"}
HEADER_BG = {"light": "rgb(11, 47, 107)", "dark": "rgb(13, 37, 84)"}
BRAND_RULE = {"light": "rgb(232, 89, 12)", "dark": "rgb(244, 122, 42)"}
FOOTER_TEXT = "Independent project. Not affiliated with or endorsed by PayPal."
FOCUS = {"light": "rgb(11, 74, 168)", "dark": "rgb(138, 184, 255)"}
HEADER_FOCUS = "rgb(255, 255, 255)"      # inside the navy header the ring is white (--header-focus)


class ScriptedClient:
    """Stands in for SandboxClient: replays recordings, never opens a socket. The send POST is slow on purpose
    (so a second click lands while the first is in flight) and PayPal's own GET never shows the evidence, which
    is exactly the 'result uncertain' state the UX has to get right."""
    def __init__(self):
        self.provide_evidence_calls = 0
        self.ops = []

    def call(self, method, path, op, **kw):
        self.ops.append(op)
        if op == "list_disputes":
            return LIST_PAGE
        if op in ("show_dispute", "pre_send_get", "recovery_get", "post_send_get"):
            return WAITING if op != "post_send_get" else {"evidences": []}
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        if op == "provide_evidence":
            self.provide_evidence_calls += 1
            time.sleep(1.2)
            return {}
        raise AssertionError(f"unexpected op {op}")


def click_and_wait(page, selector: str) -> None:
    """Click and wait for the navigation the click causes (a bare wait_for_load_state() right after a click can
    return for the page we are leaving)."""
    with page.expect_navigation():
        page.click(selector)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


JS_METRICS = """
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const sel = 'button, input:not([type=hidden]), select, textarea, summary, a.btn, .row-actions a, .actions a, .brand, .back-link, .checked-at a, td a';
  const small = [];
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue;
    let target = el;
    if (el.type === 'checkbox') target = el.closest('label') || el;
    const r = target.getBoundingClientRect();
    if (r.height < 43.5 || r.width < 43.5) {
      small.push({tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || el.name || '').trim().slice(0, 40),
                  w: Math.round(r.width), h: Math.round(r.height)});
    }
  }
  return {
    scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth,
    bodyBg: getComputedStyle(document.body).backgroundColor, small,
  };
}
"""

JS_STYLE = """
() => {
  const rgb = (el, prop) => getComputedStyle(el)[prop];
  const out = {secondary: {}, brandOutsideHeader: [], header: {}};
  for (const fam of ['info', 'ok', 'warn', 'error']) {
    const wrap = document.createElement('div');
    wrap.className = 'banner banner-' + fam;
    wrap.innerHTML = '<button class="btn btn-secondary" type="button">x</button><a class="btn btn-secondary" href="#">y</a>';
    document.body.appendChild(wrap);
    const b = wrap.querySelector('button'), a = wrap.querySelector('a');
    out.secondary[fam] = {bg: rgb(b, 'backgroundColor'), color: rgb(b, 'color'), border: rgb(b, 'borderTopColor'),
                          wrapColor: rgb(wrap, 'color'), aBg: rgb(a, 'backgroundColor'), aColor: rgb(a, 'color'),
                          aBorder: rgb(a, 'borderTopColor')};
    wrap.remove();
  }
  const header = document.querySelector('.site-header');
  out.header = {bg: rgb(header, 'backgroundColor'), color: rgb(header, 'color'), rule: rgb(header, 'borderBottomColor'),
                ruleWidth: rgb(header, 'borderBottomWidth'),
                mark: rgb(document.querySelector('.brand-mark'), 'backgroundColor')};
  const orange = new Set([out.header.rule, out.header.mark]);
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('.site-header')) continue;
    for (const prop of ['backgroundColor', 'color', 'borderTopColor', 'borderLeftColor', 'borderBottomColor', 'borderRightColor']) {
      if (orange.has(rgb(el, prop))) {
        out.brandOutsideHeader.push({tag: el.tagName.toLowerCase(), cls: el.className, prop});
      }
    }
  }
  const footer = document.querySelector('footer.site-footer');
  out.footer = footer ? footer.innerText.trim() : null;
  const brand = document.querySelector('.brand');
  out.brandText = brand ? brand.innerText.trim() : null;
  return out;
}
"""

JS_FOCUS = """
() => { const el = document.activeElement; if (!el || el === document.body) return null;
        const cs = getComputedStyle(el);
        return {tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || el.name || '').trim().slice(0, 30),
                inHeader: !!el.closest('.site-header'), style: cs.outlineStyle, width: parseFloat(cs.outlineWidth), color: cs.outlineColor}; }
"""


def audit_page(page, label, scheme, failures, report):
    m = page.evaluate(JS_METRICS)
    entry = {"page": label, "scrollWidth": m["scrollWidth"], "innerWidth": m["innerWidth"], "bodyBg": m["bodyBg"],
             "smallControls": m["small"]}
    if m["scrollWidth"] > m["innerWidth"]:
        failures.append(f"{label}: horizontal scroll ({m['scrollWidth']} > {m['innerWidth']})")
    if m["bodyBg"] != PAGE_BG[scheme]:
        failures.append(f"{label}: page background {m['bodyBg']} is not the {scheme} token {PAGE_BG[scheme]}")
    for item in m["small"]:
        failures.append(f"{label}: control under 44 px: {item}")
    st = page.evaluate(JS_STYLE)
    entry["style"] = st
    for fam, v in st["secondary"].items():
        # A secondary button is secondary in every banner family: transparent fill, the banner's own text colour,
        # an outline in that same colour - never the primary fill.
        for kind, bg, color, border in (("button", v["bg"], v["color"], v["border"]), ("link", v["aBg"], v["aColor"], v["aBorder"])):
            if bg != "rgba(0, 0, 0, 0)" or color != v["wrapColor"] or border != v["wrapColor"]:
                failures.append(f"{label}: secondary {kind} in banner-{fam} is not secondary: {v}")
    if st["header"]["bg"] != HEADER_BG[scheme]:
        failures.append(f"{label}: header background {st['header']['bg']} is not the {scheme} navy {HEADER_BG[scheme]}")
    if st["header"]["rule"] != BRAND_RULE[scheme] or st["header"]["ruleWidth"] != "4px":
        failures.append(f"{label}: header rule is {st['header']['rule']} {st['header']['ruleWidth']}")
    if st["brandOutsideHeader"]:
        failures.append(f"{label}: the orange appears outside the header: {st['brandOutsideHeader'][:3]}")
    if st["brandText"] != "Proofie":
        failures.append(f"{label}: the header brand reads {st['brandText']!r}, not 'Proofie'")
    if st["footer"] != FOOTER_TEXT:
        failures.append(f"{label}: footer note is {st['footer']!r}")
    # Tab through the page: every stop must show the ring.
    page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
    stops, bad = 0, []
    for _ in range(60):
        page.keyboard.press("Tab")
        info = page.evaluate(JS_FOCUS)
        if info is None:
            break
        stops += 1
        expected = HEADER_FOCUS if info["inHeader"] else FOCUS[scheme]
        if info["style"] != "solid" or info["width"] < 3 or info["color"] != expected:
            bad.append(info)
    entry["tabStops"], entry["focusRingProblems"] = stops, bad
    for info in bad:
        failures.append(f"{label}: focus ring missing/wrong on {info}")
    if stops == 0:
        failures.append(f"{label}: no keyboard stop found")
    report.append(entry)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="copilot-ui-"))
    config.set_db_path_override(tmp / "ui.db")
    config.set_uploads_dir_override(tmp / "uploads")
    send_recovery.time = types.SimpleNamespace(sleep=lambda s: None)   # the post-send check delays (5/15/30 s) never wait here

    fake = ScriptedClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    failures: list[str] = []
    report: list[dict] = []
    once: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ---------------------------------------------------------- once: the interactive flow (1280, light)
        ctx = browser.new_context(viewport=VIEWPORTS["1280"], color_scheme="light")
        page = ctx.new_page()
        page.goto(f"{base}/disputes/{DID}")
        page.set_input_files("#upload-file", str(ROOT / "demo" / "documents" / "01_coherent_delivery.pdf"))
        click_and_wait(page, "button:has-text('Upload')")
        click_and_wait(page, "button:has-text('Start manually instead')")
        review_url = page.url
        click_and_wait(page, "button:has-text('Attach to the response')")
        page.fill("input[name=sufficiency_override]", "Synthetic demo run: tracking comes from the demo PDF.")
        click_and_wait(page, "button:has-text('Approve this package')")
        once["review_url"] = review_url

        # pageshow restore, in a real browser: a real click on the send button with the form's own submission
        # cancelled by a capturing listener added here (so nothing is posted and the page is not left), then a
        # persisted pageshow; the button must lock on the click and unlock again with its original text.
        posts_seen = []
        page.on("request", lambda r: posts_seen.append(r.url) if r.method == "POST" and r.url.endswith("/send") else None)
        page.evaluate("() => { window.__blockSubmit = true; "
                      "document.addEventListener('submit', (e) => { if (window.__blockSubmit) e.preventDefault(); }, true); }")
        send_btn = page.locator("form[action$='/send'] button")
        send_btn.click()
        page.wait_for_timeout(300)
        locked = send_btn.is_disabled() and "Sending" in send_btn.inner_text()
        page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        unlocked = (not send_btn.is_disabled()) and "Send to PayPal" in send_btn.inner_text()
        page.evaluate("() => { window.__blockSubmit = false; }")
        once["pageshow_locked_after_submit"], once["pageshow_unlocked_after_restore"] = locked, unlocked
        once["posts_during_lock_test"] = len(posts_seen)
        if not locked:
            failures.append("send button did not lock on submit")
        if not unlocked:
            failures.append("send button did not unlock on pageshow (persisted)")
        if posts_seen or fake.provide_evidence_calls != 0:
            failures.append("the lock test itself posted something")
        posts_seen.clear()

        # double click: ONE browser request, ONE provide_evidence call
        with page.expect_navigation():
            page.locator("form[action$='/send'] button").dblclick()
        page.wait_for_timeout(500)
        once["send_requests_from_browser"] = len(posts_seen)
        once["provide_evidence_calls"] = fake.provide_evidence_calls
        once["result_page_state"] = page.locator("[data-state]").first.get_attribute("data-state")
        if len(posts_seen) != 1:
            failures.append(f"double click produced {len(posts_seen)} browser requests (expected 1)")
        if fake.provide_evidence_calls != 1:
            failures.append(f"double click produced {fake.provide_evidence_calls} provide_evidence calls (expected 1)")
        ctx.close()

        # --------------------------------------------------- every configuration: audit + screenshots
        for vp_name, viewport in VIEWPORTS.items():
            for scheme in SCHEMES:
                cfg = f"{vp_name}-{scheme}"
                ctx = browser.new_context(viewport=viewport, color_scheme=scheme)
                page = ctx.new_page()
                shots = [("inbox", f"{base}/disputes"), ("dispute", f"{base}/disputes/{DID}"),
                         ("review", once["review_url"])]
                for name, url in shots:
                    page.goto(url)
                    page.wait_for_load_state()
                    page.screenshot(path=str(OUT / f"{cfg}_{name}.png"), full_page=True)
                    audit_page(page, f"{cfg}/{name}", scheme, failures, report)
                # the result page: Check again from the review page (GET-only at PayPal)
                page.goto(once["review_url"])
                with page.expect_navigation():
                    page.locator("form[action$='/check'] button").first.click()
                page.screenshot(path=str(OUT / f"{cfg}_result-uncertain.png"), full_page=True)
                audit_page(page, f"{cfg}/result-uncertain", scheme, failures, report)
                ctx.close()
        browser.close()

    server.should_exit = True
    thread.join(timeout=5)
    once["fake_paypal_ops"] = sorted(set(fake.ops))
    result = {"base": "local fake client (no PayPal)", "once": once, "pages": report, "failures": failures}
    (OUT / "report.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(once, indent=1, ensure_ascii=False))
    print(f"pages audited: {len(report)}; captures in {OUT}")
    for f in failures:
        print("FAIL:", f)
    print("failures:", len(failures))
    return 1 if failures else 0




CAPTURE_NAMESPACES = (
    "account_cliff", "active_approval", "claims", "conformance", "deadlines", "draft",
    "events", "fulfilment", "ledger", "outcome", "resolution", "triage", "webhooks",
    "carrier_required", "package_print", "delivery_facts",
)


def capture_set(flags, output, tier2_pages=False, source_sha=None):
    """Capture three synthetic pages; no approval, send, model or external transport."""
    import hashlib
    from urllib.parse import urlsplit
    from copilot.db import repo

    selected = frozenset(filter(None, (part.strip() for part in flags.split(","))))
    unknown = selected - set(CAPTURE_NAMESPACES)
    if unknown:
        raise ValueError("Unknown feature namespace: " + ", ".join(sorted(unknown)))
    for key in tuple(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            del os.environ[key]
    for name in selected:
        os.environ["COPILOT_FEATURE_" + name.upper()] = "true"
    os.environ["COPILOT_HOSTED_DEMO"] = "false"
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "INDEX.md").exists():
        raise ValueError("Capture output already contains an index; preserve previous evidence")
    temporary = Path(tempfile.mkdtemp(prefix="proofie-capture-"))
    db = temporary / "capture.db"
    config.set_db_path_override(db)
    config.set_uploads_dir_override(temporary / "uploads")
    package = repo.insert_package(db, DID, "human_edit", True, [])
    repo.insert_claims(db, package, [{"evidence_index": 0, "claim_index": 0,
        "text": "DEMO: synthetic delivery document awaiting human review.",
        "source_ids": [], "basis": "document", "edited_by_human": True}])

    capture_id = "DEMO1234567890123"
    if tier2_pages and "package_print" in selected:
        from copilot.core.raw_storage import write_raw
        data = b"DEMO DATA: Synthetic shipment proof."
        sha = hashlib.sha256(data).hexdigest()
        document = repo.insert_document(db, DID, "demo.txt", "text/plain", len(data), sha,
                                        data.decode(), True)
        write_raw(config.uploads_dir(), sha, data)
        repo.attach_document(db, package, document)
    if tier2_pages and "delivery_facts" in selected:
        import sqlite3
        request_body = {"trackers": [{"transaction_id": capture_id,
            "tracking_number": "DEMO1234567890", "carrier": "FEDEX", "status": "SHIPPED"}]}
        readback = {"tracking_number": "DEMO1234567890", "carrier": "FEDEX", "status": "SHIPPED",
                    "shipment_date": "2026-10-07"}
        checks = {"address_comparison": {"result": "match"}}
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE fulfilment_files(id INTEGER PRIMARY KEY,capture_id TEXT,"
                "created_at TEXT,request_json TEXT,request_sha256 TEXT,approval_sha256 TEXT,"
                "tracker_readback_json TEXT,checks_json TEXT,file_sha256 TEXT,status TEXT)")
            conn.execute("INSERT INTO fulfilment_files VALUES(?,?,?,?,?,?,?,?,?,?)", (None, capture_id,
                "2026-10-08T12:00:00+0000", json.dumps(request_body), digest(request_body), digest(request_body),
                json.dumps(readback), json.dumps(checks), digest({"request": request_body,
                "tracker_readback": readback, "checks": checks}), "registered"))
    capture_pages = [("inbox", "/disputes"), ("dispute", f"/disputes/{DID}"),
                     ("review", f"/disputes/{DID}/packages/{package}")]
    if tier2_pages and "package_print" in selected:
        capture_pages.append(("package_print", f"/disputes/{DID}/packages/{package}/print"))
    if tier2_pages and "delivery_facts" in selected:
        capture_pages.append(("delivery_facts", f"/delivery-facts/{capture_id}"))

    class CaptureClient(ScriptedClient):
        def call(self, method, path, op, **kwargs):
            if method != "GET":
                raise AssertionError("Capture transport permits GET only")
            if op == "account_cliff_search":
                self.ops.append(op)
                return {"transaction_details": [], "total_pages": 1}
            return super().call(method, path, op, **kwargs)

    # Optional read-only views use a separate coherent rollback-mode synthetic
    # snapshot. The shared review page opens the existing send-attempt WAL store;
    # print/facts deliberately reject that unsupported mode, not a capture failure
    # to hide. Preserve the separate database provenance in the evidence index.
    readonly_db = temporary / "readonly-capture.db"
    if tier2_pages:
        import sqlite3
        source_conn = sqlite3.connect(db)
        target_conn = sqlite3.connect(readonly_db)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()

    fake = CaptureClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    entries = []
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        if not server.started:
            raise RuntimeError("Capture server did not start")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for viewport_name, viewport in VIEWPORTS.items():
                    for scheme in SCHEMES:
                        context = browser.new_context(viewport=viewport, color_scheme=scheme)
                        context.route("**/*", lambda route: route.continue_()
                            if urlsplit(route.request.url).netloc == f"127.0.0.1:{port}"
                            else route.abort())
                        try:
                            page = context.new_page()
                            for name, path in capture_pages:
                                config.set_db_path_override(readonly_db if name in ("package_print", "delivery_facts") else db)
                                response = page.goto(base + path, wait_until="networkidle")
                                if response is None or response.status != 200:
                                    raise AssertionError(f"{name}: expected 200, received {response.status if response else None}")
                                filename = f"{viewport_name}-{scheme}_{name}.png"
                                page.screenshot(path=str(output / filename), full_page=True)
                                if name == "package_print" and viewport_name == "1280" and scheme == "light":
                                    page.pdf(path=str(output / "sample.pdf"), format="A4", print_background=True)
                                if name == "delivery_facts":
                                    for language, element_id in (("english", "english"), ("spanish", "spanish")):
                                        element = page.locator("#" + element_id)
                                        if element.count() != 1 or not element.input_value().strip():
                                            raise AssertionError("Missing usable " + language + " delivery facts")
                                visible = page.locator("body").inner_text()
                                text_file = filename.replace(".png", ".txt")
                                (output / text_file).write_text(visible, encoding="utf-8")
                                entries.append({"file": filename,
                                    "sha256": hashlib.sha256((output / filename).read_bytes()).hexdigest(),
                                    "flags": sorted(selected), "viewport": viewport, "scheme": scheme,
                                    "visible_text": text_file})
                        finally:
                            context.close()
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("Owned capture server did not stop")
    if fake.provide_evidence_calls:
        raise AssertionError("Capture unexpectedly invoked provide_evidence")
    index = "# Synthetic UI capture index\n\nSource SHA: " + (source_sha or "not supplied") + "\n\n| File | SHA256 | Flagset | Viewport | Scheme |\n|---|---|---|---|---|\n"
    for entry in entries:
        index += f"| {entry['file']} | {entry['sha256']} | {','.join(entry['flags']) or 'off'} | {entry['viewport']['width']} x {entry['viewport']['height']} | {entry['scheme']} |\n"
    if (output / "sample.pdf").exists():
        index += "| sample.pdf | " + hashlib.sha256((output / "sample.pdf").read_bytes()).hexdigest() + " | " + ",".join(sorted(selected)) + " | A4 | print |\n"
    index += "\nVisible text companions support factual comparison. Optional print and bilingual delivery-facts pages are synthetic; no approval or send was performed.\n"
    (output / "INDEX.md").write_text(index, encoding="utf-8")
    (output / "captures.json").write_text(json.dumps({"entries": entries, "fake_operations": sorted(set(fake.ops)),
        "provide_evidence_calls": 0, "synthetic": True, "source_sha": source_sha, "readonly_fixture": "separate rollback snapshot" if tier2_pages else None}, indent=2), encoding="utf-8")
    print(f"Captured {len(entries)} synthetic pages with flags: {','.join(sorted(selected)) or 'off'}")
    return 0


def cli():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flags", default="", help="Comma-separated feature namespaces; default none")
    parser.add_argument("--out", type=Path, help="Synthetic three-page capture directory")
    parser.add_argument("--tier2-pages", action="store_true", help="Capture optional print/PDF and bilingual facts")
    parser.add_argument("--source-sha", help="Declared runtime Git source for evidence index")
    args = parser.parse_args()
    if args.out is not None:
        return capture_set(args.flags, args.out, args.tier2_pages, args.source_sha)
    if args.flags:
        parser.error("--flags requires --out")
    return main()


if __name__ == "__main__":
    sys.exit(cli())
