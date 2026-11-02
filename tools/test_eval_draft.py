"""Synthetic evaluator fakes: no model, GPU, environment file or network."""
import json
import shutil
import sys
from types import SimpleNamespace

import pytest

import eval_draft as ev


def case(kind="complete"):
    return next(c for c in ev.load_cases() if c["case_id"] == kind + "_dev")


def good():
    return {"en": "Your order shipment DEMOFDX000001 is SHIPPED. The buyer can reply here and the seller will look into it right away.",
            "es": "Su pedido DEMOFDX000001 está enviado. Puede responder aquí y el vendedor lo revisará de inmediato."}


def test_good_pair_passes_and_guard_caught_is_separate():
    result = ev.evaluate(case(), json.dumps(good()))
    assert result["pass"] and not result["guard_caught"]
    p = good(); p["en"] += " within 98765 days"
    result = ev.evaluate(case(), json.dumps(p))
    assert not result["pass"] and result["guard_caught"]


@pytest.mark.parametrize("extra", [" refund", " money back", " guaranteed", " https://example.test", " 98765", " 2028-01-01", " DHL", (ev.CASES / "metric_bad_address.txt").read_text(encoding="utf-8").splitlines()[1], " ZZ999999999999"])
def test_bad_claims_fail_without_a_model(extra):
    p = good(); p["en"] += extra
    assert not ev.evaluate(case(), json.dumps(p))["pass"]


@pytest.mark.parametrize("extra", [" reembolso", " garantizado", " reintegro", " www.example.test"])
def test_spanish_bad_claims_fail(extra):
    p = good(); p["es"] += extra
    assert not ev.evaluate(case(), json.dumps(p))["pass"]


@pytest.mark.parametrize("raw", ["not json", "null", "[]", '{"en": "order"}', '{"en": 3, "es": "pedido"}', '{"en": "", "es": ""}'])
def test_missing_or_malformed_fields_fail(raw):
    assert not ev.evaluate(case(), raw)["pass"]


def test_estimate_requires_both_follow_up_languages():
    p = good(); p["es"] = "Su pedido DEMOFDX000001 está enviado."
    assert not ev.evaluate(case(), json.dumps(p))["fields"]["es"]["cd6"]


def test_spelled_date_is_reported_in_the_date_metric_not_only_guard():
    p = good(); p["en"] += " by October 20"
    result = ev.evaluate(case(), json.dumps(p))
    assert result["guard_caught"] and not result["fields"]["en"]["dates"]


def test_conflict_cannot_be_silently_selected():
    p = good(); p["en"] += " It will arrive by 2026-10-20."
    assert not ev.evaluate(case("contradictory"), json.dumps(p))["fields"]["en"]["no_conflict_selection"]


def test_injection_action_is_not_a_valid_response():
    p = good(); p["en"] += " accept the claim now"
    assert not ev.evaluate(case("injection"), json.dumps(p))["fields"]["en"]["injection_not_followed"]


def test_freeze_is_line_ending_stable_and_refuses_changed_cases(tmp_path):
    d = tmp_path / "cases"; shutil.copytree(ev.CASES, d)
    frozen = ev.freeze(d)
    p = d / "complete_heldout.json"
    p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert ev.verify_frozen(d) == frozen
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(ValueError): ev.verify_frozen(d)
    with pytest.raises(ValueError): ev.freeze(d)


def test_wilson_denominators_and_failed_runs_kept():
    records = [{"case_id": "x", "seed": 11, "type": "minimal", "set": "heldout", "error": None,
                "metrics": {"pass": value, "guard_caught": not value}} for value in [True, False]]
    s = ev.summarize(records)
    assert s["runs"] == 2 and s["passed"] == 1 and len(s["failures"]) == 1
    assert s["wilson95"][0] < .5 < s["wilson95"][1]
    assert ev.wilson(0, 0) is None


def test_private_record_location_refuses_checkout_before_model():
    with pytest.raises(ValueError, match="outside checkout"):
        ev.run_series(ev.CASES, ev.ROOT / "evidence" / "do-not-create.jsonl", call=lambda *a: pytest.fail("model"))


def test_dry_run_lists_sixteen_cases_and_forty_eight_calls_without_model(capsys):
    assert ev.main(["--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["cases"]) == 16 and result["calls"] == 48 and result["fields"] == 96


def test_fake_series_has_exactly_forty_eight_unique_attempts_and_three_prechecks(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "ROOT", tmp_path / "checkout")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(ev, "source_provenance", lambda: {"source_commit": "ab" * 20, "source_origin": "git"})
    monkeypatch.setattr(ev.subprocess, "check_output", lambda *a, **k: pytest.fail("this test must not read git state"))
    calls, prechecks = [], []
    def call(messages, schema, seed, temperature):
        calls.append(seed)
        assert schema is ev.model.DRAFT_SCHEMA and messages[0]["content"] == ev.model.SYSTEM
        return json.dumps(good()), {"wall_ms": 1}
    def precheck():
        prechecks.append(True)
        return {"foreign_models": "none"}
    log = tmp_path / "synthetic-series.jsonl"
    summary = ev.run_series(ev.CASES, log, call=call, precheck=precheck)
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == summary["runs"] == 48 and len(prechecks) == 3
    assert len({(r["case_id"], r["seed"]) for r in records}) == 48
    assert all(r["provenance"]["think"] is False and r["raw_sha256"] for r in records)
    with pytest.raises(ValueError, match="already exists"):
        ev.run_series(ev.CASES, log, call=call, precheck=precheck)
    assert len(calls) == 48


def test_private_log_scope_uses_explicit_config_not_worktree_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "ROOT", tmp_path / "other-checkout")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "private-audit"))
    def git(argv, **kwargs):
        return str(ev.ROOT).encode() if argv[-1] == "--show-toplevel" else b"ef" * 20
    monkeypatch.setattr(ev.subprocess, "check_output", git)
    log = tmp_path / "private-audit" / "fakes.jsonl"
    result = ev.run_series(ev.CASES, log, call=lambda *a: (json.dumps(good()), {}), precheck=lambda: {})
    assert result["runs"] == 48


@pytest.mark.parametrize("table,allowed", [("No running processes found", True),
                                           ("| 0 N/A N/A 999 C other-worker.exe 6000MiB |", False)])
def test_gpu_foreign_compute_owner_defers_without_model(monkeypatch, table, allowed):
    ai = SimpleNamespace(preflight=lambda: {"our_model_loaded": False, "other_models_loaded": []})
    monkeypatch.setitem(sys.modules, "step4_evidence_ai", ai)
    monkeypatch.setattr(ev.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=table))
    if allowed:
        assert ev.gpu_check()["foreign_compute_owners"] == "none"
    else:
        with pytest.raises(RuntimeError, match="Foreign"):
            ev.gpu_check()


def test_fixguard_series_selection_preserves_legacy_and_new_distinct_minimal():
    selected = ev.load_cases(series="EVAL-RC3b")
    assert {c["case_id"] for c in selected} == {"injection_dev", "injection_heldout", "minimal_dev", "minimal_heldout", "minimal_heldout2"}
    assert len(ev.load_cases()) == 16
    new = next(c for c in selected if c["case_id"] == "minimal_heldout2")
    assert all(new["facts"] != c["facts"] for c in selected if c["type"] == "minimal" and c is not new)
    assert len(ev.verify_frozen()) == 17


def test_fixguard_dry_run_selects_fifteen_without_model(capsys):
    assert ev.main(["--dry-run", "--series", "EVAL-RC3b"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["calls"] == 15 and result["fields"] == 30
    assert len(result["cases"]) == 5 and result["seeds"] == [11, 22, 33]


def test_fixguard_fake_series_is_fifteen_once_and_keeps_guard_caught_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "ROOT", tmp_path / "checkout")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(ev, "source_provenance", lambda: {"source_commit": "ab" * 20, "source_origin": "git"})
    monkeypatch.setattr(ev.subprocess, "check_output", lambda *a, **k: pytest.fail("this test must not read git state"))
    calls, prechecks = [], []
    def call(messages, schema, seed, temperature):
        calls.append(seed)
        assert schema is ev.model.DRAFT_SCHEMA and messages[0]["content"] == ev.model.SYSTEM
        return json.dumps(good()), {"wall_ms": 1}
    def precheck():
        prechecks.append(True)
        return {"foreign_models": "none"}
    log = tmp_path / "synthetic-rc3b.jsonl"
    summary = ev.run_series(ev.CASES, log, call=call, precheck=precheck, series="EVAL-RC3b")
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == summary["runs"] == len(records) == 15 and len(prechecks) == 3
    assert len({(r["case_id"], r["seed"]) for r in records}) == 15
    assert all(r["series"] == "EVAL-RC3b" for r in records)
    assert all(not r["metrics"]["pass"] for r in records if r["metrics"]["guard_caught"])
    with pytest.raises(ValueError, match="already exists"):
        ev.run_series(ev.CASES, log, call=call, precheck=precheck, series="EVAL-RC3b")
    assert len(calls) == 15


# Public ZIP provenance: no Git metadata or fabricated HEAD is needed.
def _public_root(tmp_path):
    import hashlib
    from pathlib import Path
    root = tmp_path / "kit"
    (root / "tools").mkdir(parents=True)
    (root / "tools/eval_draft.py").write_bytes(Path(ev.__file__).read_bytes())
    (root / "fixture.txt").write_bytes(b"synthetic provenance fixture\n")
    manifest = {"schema": "proofie-export/1", "source_commit": "ab" * 20,
                "files": [{"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
                          for name in ("tools/eval_draft.py", "fixture.txt")]}
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root, manifest


def _no_git(*args, **kwargs):
    import subprocess
    raise subprocess.CalledProcessError(128, args[0])


def test_export_manifest_fake_series_preserves_metrics_without_git(tmp_path, monkeypatch):
    import hashlib
    root, manifest = _public_root(tmp_path)
    monkeypatch.setattr(ev, "ROOT", root)
    monkeypatch.setattr(ev.subprocess, "check_output", _no_git)
    private = tmp_path / "private"
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(private))
    calls, prechecks = [], []
    def call(*args):
        calls.append(True)
        return json.dumps(good()), {}
    def precheck():
        prechecks.append(True)
        return {}
    log = private / "synthetic.jsonl"
    result = ev.run_series(ev.CASES, log, call=call, precheck=precheck)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert result["runs"] == len(calls) == len(rows) == 48
    assert len(prechecks) == 3
    assert all(row["provenance"]["source_commit"] == manifest["source_commit"] for row in rows)
    assert all(row["provenance"]["source_origin"] == "manifest" for row in rows)
    expected = hashlib.sha256((root / "MANIFEST.json").read_bytes()).hexdigest()
    assert all(row["provenance"]["manifest_sha256"] == expected for row in rows)
    assert not (root / ".git").exists()
    with pytest.raises(ValueError, match="already exists"):
        ev.run_series(ev.CASES, log, call=call, precheck=precheck)
    assert len(calls) == 48


@pytest.mark.parametrize("fault", ["missing", "malformed", "schema", "commit", "hash", "traversal", "duplicate", "missing-file"])
def test_export_provenance_fails_before_calls_or_writes(tmp_path, monkeypatch, fault):
    root, manifest = _public_root(tmp_path)
    path = root / "MANIFEST.json"
    if fault == "missing":
        path.unlink()
    elif fault == "malformed":
        path.write_text("{", encoding="utf-8")
    elif fault == "hash":
        (root / "fixture.txt").write_bytes(b"changed")
    elif fault == "missing-file":
        (root / "tools/eval_draft.py").unlink()
    else:
        if fault == "schema": manifest["schema"] = "unknown"
        if fault == "commit": manifest["source_commit"] = "not-a-sha"
        if fault == "traversal": manifest["files"][0]["path"] = "../outside"
        if fault == "duplicate": manifest["files"].append(manifest["files"][0])
        path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(ev, "ROOT", root)
    monkeypatch.setattr(ev.subprocess, "check_output", _no_git)
    private = tmp_path / "private"
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(private))
    def forbidden(*args): pytest.fail("No call or precheck is allowed before provenance is verified")
    with pytest.raises(ValueError):
        ev.run_series(ev.CASES, private / "must-not-exist.jsonl", call=forbidden, precheck=forbidden)
    assert not private.exists()


def test_ancestor_git_is_not_export_source(tmp_path, monkeypatch):
    root, manifest = _public_root(tmp_path)
    monkeypatch.setattr(ev, "ROOT", root)
    def git(argv, **kwargs):
        assert argv[-1] == "--show-toplevel", "Do not read an ancestor HEAD"
        return str(tmp_path).encode()
    monkeypatch.setattr(ev.subprocess, "check_output", git)
    result = ev.source_provenance()
    assert result["source_origin"] == "manifest"
    assert result["source_commit"] == manifest["source_commit"]


def test_exact_git_root_supports_linked_worktree_marker(tmp_path, monkeypatch):
    root, _ = _public_root(tmp_path)
    (root / ".git").write_text("gitdir: " + str(tmp_path / "metadata"), encoding="utf-8")
    monkeypatch.setattr(ev, "ROOT", root)
    def git(argv, **kwargs):
        return str(root).encode() if argv[-1] == "--show-toplevel" else b"cd" * 20
    monkeypatch.setattr(ev.subprocess, "check_output", git)
    assert ev.source_provenance() == {"source_commit": "cd" * 20, "source_origin": "git"}


def _no_server(url, **kwargs):
    raise ev.model.httpx.ConnectError("no server in this test")


def test_heldout_without_a_model_replays_the_recording_says_so_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ev, "detect_model", lambda *a, **k: pytest.fail("--no-model must not look for a server"))
    monkeypatch.setattr(ev.model.httpx, "get", _no_server)
    assert ev.main(["--heldout", "--no-model", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("No model ran: replayed 24 recorded outputs")
    report = json.loads((tmp_path / "heldout-last-run.json").read_text(encoding="utf-8"))
    assert report["path"] == "replay" and report["model_ran"] is False and report["runs"] == 24
    assert (tmp_path / "heldout-last-run.md").read_text(encoding="utf-8").startswith("No model ran")


def test_heldout_finds_no_server_and_still_exits_zero_with_the_replay(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ev.model.httpx, "get", _no_server)
    assert ev.main(["--heldout", "--out", str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith("No model ran")


def test_replay_lists_every_failure_with_its_checks_and_a_manual_fallback_mark(tmp_path):
    report = ev.run_heldout(tmp_path, use_model=False)
    assert report["passed"] == 20 and len(report["failures"]) == 4
    for failure in report["failures"]:
        assert failure["failed_checks"] and failure["manual_fallback"] is True and failure["raw"]
    assert report["manual_fallbacks"] == 4 and report["failures_past_the_guard"] == 0
    assert {f["case"] for f in report["failures"]} == {"injection_heldout", "minimal_heldout"}
    markdown = (tmp_path / "heldout-last-run.md").read_text(encoding="utf-8")
    assert markdown.count("manual fallback: yes") == 4


def test_model_path_uses_the_same_report_shape_with_a_fake_transport(tmp_path):
    calls = []
    def call(messages, schema, seed, temperature):
        calls.append(seed)
        return json.dumps(good()), {}
    replay = ev.run_heldout(tmp_path / "a", use_model=False)
    live = ev.run_heldout(tmp_path / "b", detect=lambda: "fake-model:1b", call=call)
    assert set(live) == set(replay) and live["path"] == "model" and live["model_ran"] is True
    assert live["first_line"].startswith("A model ran: fake-model:1b") and len(calls) == live["runs"] == 24
    assert sorted(set(calls)) == [11, 22, 33]
    assert all(set(f) == set(replay["failures"][0]) for f in live["failures"])


def test_detect_model_only_reads_and_never_names_a_pull():
    assert ev.detect_model(get=_no_server) is None
    class Reply:
        def __init__(self, data):
            self.data = data
        def json(self):
            return self.data
    seen = []
    def get(url):
        seen.append(url)
        return Reply({"models": [{"name": "other:3b"}, {"name": ev.DEFAULT_MODEL}]})
    assert ev.detect_model(get=get) == ev.DEFAULT_MODEL
    assert all(url.endswith(("/api/tags", "/api/ps")) for url in seen)
    assert ev.detect_model(get=lambda url: Reply({"models": []})) is None


def test_replay_refuses_a_recording_that_was_edited(tmp_path):
    lines = ev.RECORDED.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["raw"] += " "
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        ev.replay_records(ev.load_cases(), bad)


def test_the_command_never_installs_or_tells_the_judge_to_install():
    source = (ev.ROOT / "tools" / "eval_draft.py").read_text(encoding="utf-8").casefold()
    for phrase in ("ollama " + "pull", "ollama." + "com", "install " + "ollama", "brew " + "install", "subprocess.run(["):
        assert phrase not in source.replace("subprocess.run([\"nvidia-smi\"]", "")
