"""The judge's setup self-check (tools/judge_check.py), against a fake app: no network, no PayPal.

Expected, written before the first run:
- everything in place: every row PASS, the verdict says all passed, only GET is sent;
- no dispute waiting: step 5 FAILs with the JUDGE-RUN step that opens one, the page rows are SKIP, nothing crashes;
- app down (nothing answers, no starter): step 4 FAILs and the rest are SKIP; with a starter the app is started and stopped;
- credentials missing or placeholders: step 2 FAILs naming the setting, never printing a value;
- one page answers 500: only that row FAILs, the others PASS;
- no secret value reaches the output.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("judge_check", Path(__file__).resolve().parents[2] / "tools" / "judge_check.py")
jc = importlib.util.module_from_spec(SPEC)
sys.modules["judge_check"] = jc
SPEC.loader.exec_module(jc)

DID = "PP-R-FAKE-0000001"
PROBE = "-".join(["marker", "zz9", "never", "print"])


def page(title, body=""):
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>"


def inbox(rows):
    cells = "".join(f'<tr data-dispute="{d}"><td>{label}</td></tr>' for d, label in rows)
    return page("All disputes", f"<table>{cells}</table>")


class FakeApp:
    def __init__(self, rows=((DID, "Waiting for your response"),), down=False, fail=(), package=False):
        self.down, self.fail, self.calls = down, set(fail), []
        self.pages = {"/disputes": (200, inbox(rows))}
        base = f"/disputes/{DID}"
        links = f'<a href="/fulfilment/CAP123456?from_dispute={DID}">sale</a>'
        if package:
            links += f'<a href="{base}/packages/1">p</a>'
        self.pages[base] = (200, page(f"Item not received Dispute {DID}",
                                      '<dd data-state="stage">Inquiry</dd><a href="/workspace/hackathon">h</a>'))
        for v, t in jc.VIEWS:
            self.pages[f"{base}/workspace/{v}"] = (200, page(f"{t}: dispute {DID} - Proofie", links))
        self.pages[f"{base}/workspace/hackathon"] = (200, page("Hackathon review"))
        for n, t in jc.EXTRA_PAGES:
            self.pages[f"{base}/{n}"] = (200, page(t))
        self.pages[f"/fulfilment/CAP123456?from_dispute={DID}"] = (200, page("Tracking at the sale: CAP123456"))
        self.pages[f"{base}/packages/1"] = (200, page("Evidence package v1"))
        self.pages["/healthz/features"] = (200, json.dumps({"on": ["COPILOT_FEATURE_A"], "off": ["COPILOT_FEATURE_B"]}))

    def get(self, path):
        self.calls.append(("GET", path))
        if self.down:
            raise OSError("ConnectError")
        for bad in self.fail:
            if path.endswith(bad):
                return 500, page("Something went wrong inside Proofie")
        return self.pages.get(path, (404, page("Not found")))


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(jc, "check_python", lambda *a, **k: jc.Row("1 Python", "x", "ok", jc.PASS))
    (tmp_path / ".env.example").write_text("PAYPAL_CLIENT_ID=your-sandbox-client-id-here\nPAYPAL_CLIENT_SECRET=your-sandbox-secret-here\n"
                                           "COPILOT_FEATURE_A=true\nCOPILOT_FEATURE_B=false\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"PAYPAL_CLIENT_ID=abc123\nPAYPAL_CLIENT_SECRET={PROBE}\nCOPILOT_FEATURE_A=true\n"
                                   "COPILOT_FEATURE_B=false\n", encoding="utf-8")
    return tmp_path


def results(rows):
    return {r.step: r.result for r in rows}


def test_everything_in_place_passes_every_row(root):
    app = FakeApp(package=True)
    rows = jc.run_checks(root, app, environ={})
    assert all(r.result == jc.PASS for r in rows), [(r.step, r.seen) for r in rows if r.result != jc.PASS]
    steps = " ".join(r.step for r in rows)
    for name in ("overview", "workspace/prepare", "workspace/sale", "workspace/hackathon", "package", "history", "ledger",
                 "outcome", "resolution", "draft", "triage", "fulfilment"):
        assert name in steps, name
    assert jc.verdict(rows).startswith("VERDICT: all")
    assert {m for m, _ in app.calls} == {"GET"}


def test_no_open_dispute_is_an_instruction_not_a_crash(root):
    rows = jc.run_checks(root, FakeApp(rows=()), environ={})
    r = results(rows)
    assert r["5 An open dispute"] == jc.FAIL and r["6 Case pages"] == jc.SKIP
    fix = next(x.fix for x in rows if x.step.startswith("5"))
    assert "JUDGE-RUN.md section 3" in fix and "not an error" in fix
    assert "Start with: 5" in jc.verdict(rows)


def test_a_dispute_that_is_not_waiting_for_you_is_not_picked(root):
    rows = jc.run_checks(root, FakeApp(rows=((DID, "Resolved"),)), environ={})
    assert results(rows)["5 An open dispute"] == jc.FAIL
    assert "1 listed" in next(x.seen for x in rows if x.step.startswith("5"))


def test_app_down_without_a_starter_fails_step_4_and_skips_the_rest(root):
    rows = jc.run_checks(root, FakeApp(down=True), environ={}, start=None)
    r = results(rows)
    assert r["4 App reachable"] == jc.FAIL and r["5 An open dispute"] == jc.SKIP and r["6 Case pages"] == jc.SKIP


def test_the_app_is_started_when_down_and_stopped_at_the_end(root):
    class Proc:
        stopped = False

        def terminate(self):
            Proc.stopped = True

        def wait(self, timeout=None):
            return 0

    app = FakeApp(down=True)

    def starter(root_, port):
        app.down = False
        return Proc()

    rows = jc.run_checks(root, app, environ={}, start=starter)
    assert results(rows)["4 App reachable"] == jc.PASS and Proc.stopped
    assert "started by this check" in next(x.seen for x in rows if x.step.startswith("4"))


def test_missing_credentials_fail_step_2_and_never_print_a_value(root):
    (root / ".env").write_text("PAYPAL_CLIENT_ID=your-sandbox-client-id-here\nPAYPAL_CLIENT_SECRET=\nCOPILOT_FEATURE_A=true\n"
                               "COPILOT_FEATURE_B=false\n", encoding="utf-8")
    rows = jc.run_checks(root, FakeApp(), environ={})
    row = next(x for x in rows if x.step.startswith("2"))
    assert row.result == jc.FAIL and "PAYPAL_CLIENT_ID" in row.seen and "PAYPAL_CLIENT_SECRET" in row.seen


def test_no_settings_file_is_named_plainly():
    row = jc.check_credentials({}, {}, False)
    assert row.result == jc.FAIL and "Copy .env.example" in row.fix


def test_flags_that_differ_from_the_example_fail_step_3(root):
    (root / ".env").write_text("PAYPAL_CLIENT_ID=abc\nPAYPAL_CLIENT_SECRET=def\nCOPILOT_FEATURE_A=false\n", encoding="utf-8")
    rows = jc.run_checks(root, FakeApp(), environ={})
    row = next(x for x in rows if x.step.startswith("3"))
    assert row.result == jc.FAIL and "COPILOT_FEATURE_A" in row.seen and "COPILOT_FEATURE_B" in row.seen


def test_one_page_with_a_500_fails_only_that_row(root):
    rows = jc.run_checks(root, FakeApp(fail=("/ledger",)), environ={})
    failed = [r for r in rows if r.result == jc.FAIL]
    assert [r.step for r in failed] == ["6 ledger"] and "HTTP 500" in failed[0].seen
    assert jc.verdict(rows).startswith("VERDICT: 1 failed")


def test_a_page_with_the_wrong_heading_fails(root):
    app = FakeApp()
    app.pages[f"/disputes/{DID}/triage"] = (200, page("Something else"))
    assert results(jc.run_checks(root, app, environ={}))["6 triage"] == jc.FAIL


def test_the_table_and_the_verdict_never_contain_a_secret(root):
    rows = jc.run_checks(root, FakeApp(), environ={})
    out = jc.render(rows) + jc.verdict(rows)
    assert PROBE not in out and "abc123" not in out
    assert "Step" in out and "How to fix" in out


def test_python_row():
    assert jc.check_python((3, 12, 4), "/v", "/base").result == jc.PASS
    assert jc.check_python((3, 11, 0), "/v", "/base").result == jc.FAIL
    assert jc.check_python((3, 12, 4), "/base", "/base").result == jc.FAIL


def test_the_running_app_reports_the_settings_it_runs_with_and_they_must_match_the_example(root):
    app = FakeApp()
    app.pages["/healthz/features"] = (200, json.dumps({"on": ["COPILOT_FEATURE_A", "COPILOT_FEATURE_B"], "off": []}))
    rows = jc.run_checks(root, app, environ={})
    row = next(x for x in rows if x.step.startswith("4b"))
    assert row.result == jc.FAIL and "1 differ: COPILOT_FEATURE_B" in row.seen and "start it again" in row.fix
    assert "COPILOT_FEATURE_A" not in row.seen
    assert next(x for x in rows if x.step.startswith("4b")).fix and jc.verdict(rows).startswith("VERDICT:")


def test_an_app_that_does_not_report_its_settings_is_a_skip_not_a_failure(root):
    app = FakeApp()
    del app.pages["/healthz/features"]
    rows = jc.run_checks(root, app, environ={})
    assert next(x for x in rows if x.step.startswith("4b")).result == jc.SKIP
    app.pages["/healthz/features"] = (200, "not json")
    assert next(x for x in jc.run_checks(root, app, environ={}) if x.step.startswith("4b")).result == jc.SKIP


def test_model_absent_is_information_not_a_failure():
    def down(url):
        raise OSError("no server")
    assert jc.model_note(get=down) == "Local model (optional): absent, manual mode and replay evaluation available."
    assert "absent" not in jc.model_note(get=lambda url: {"models": [{"name": "m:1b"}]})
    note = jc.model_note(get=lambda url: {"models": []})
    assert "replay evaluation available" in note and "FAIL" not in note and "install" not in note.lower()
