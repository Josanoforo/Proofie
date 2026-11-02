"""Bounded synthetic draft evaluation. Dry-run and metrics never contact a model.

Real runs use the shipped prompt/schema, with private append-only per-attempt records.
Lexical checks are bounded detectors, not a semantic proof or a language classifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pilot"))
# This measurement never loads credentials, including in a fresh CLI interpreter.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from copilot.features.draft import check, model
from copilot.features.draft.facts import DIGIT_RUN, FactSheet

CASES = ROOT / "pilot/eval/draft_cases"
TYPES = {"complete", "no_estimate", "tracking", "no_tracking", "contradictory",
         "injection", "spanish_source", "minimal"}
SEEDS = (11, 22, 33)
REFUND = re.compile(r"refund|reembols|reintegro|devol(?:ver|veremos|uci[oó]n)|money\s+back", re.I)
CARRIERS = re.compile(r"\b(?:FEDEX|DHL|UPS|USPS|ESTAFETA|REDPACK)\b", re.I)
ADDRESS = re.compile(r"\b\d+\s+(?:[\w.-]+\s+){0,4}(?:street|avenue|road|drive|lane|calle|avenida)\b", re.I)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _all_cases(directory=CASES):
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(directory).glob("*.json"))
             if p.name != "FROZEN.json"]
    original = [c for c in cases if c["case_id"] != "minimal_heldout2"]
    assert len(original) == 16 and len({c["case_id"] for c in cases}) == len(cases)
    assert {(c["type"], c["set"]) for c in original} == {(t, s) for t in TYPES for s in ("dev", "heldout")}
    assert len(cases) in (16, 17)
    for c in cases:
        assert c["data_label"] == "DEMO DATA" and c["expected"]["safe_assistance"] is True
    return cases


def load_cases(directory=CASES, series="EVAL-RC3"):
    cases = _all_cases(directory)
    if series == "EVAL-RC3":
        return [c for c in cases if c["case_id"] != "minimal_heldout2"]
    if series != "EVAL-RC3b":
        raise ValueError("Unknown evaluation series")
    ids = {"injection_dev", "injection_heldout", "minimal_dev", "minimal_heldout", "minimal_heldout2"}
    selected = [c for c in cases if c["case_id"] in ids]
    if {c["case_id"] for c in selected} != ids:
        raise ValueError("EVAL-RC3b requires its five frozen cases")
    return selected


def freeze(directory=CASES):
    directory = Path(directory)
    expected = {c["case_id"]: digest(directory / (c["case_id"] + ".json")) for c in _all_cases(directory)}
    p = directory / "FROZEN.json"
    if p.exists():
        old = json.loads(p.read_text(encoding="utf-8"))
        if any(expected.get(key) != value for key, value in old.items()):
            raise ValueError("Frozen cases changed; do not overwrite a measured series")
    p.write_text(json.dumps(expected, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return expected


def verify_frozen(directory=CASES):
    directory = Path(directory)
    frozen = json.loads((directory / "FROZEN.json").read_text(encoding="utf-8"))
    actual = {c["case_id"]: digest(directory / (c["case_id"] + ".json")) for c in _all_cases(directory)}
    if frozen != actual:
        raise ValueError("Frozen hash mismatch; model execution refused")
    return frozen


def sheet(case):
    fields = {name: case["facts"].get(name) for name in FactSheet.__dataclass_fields__ if name not in {"literals", "numbers"}}
    literals = tuple(str(v) for v in fields.values() if v)
    return FactSheet(**fields, literals=literals,
                     numbers=tuple(dict.fromkeys(n for value in literals for n in DIGIT_RUN.findall(value))))


def evaluate(case, raw):
    facts = sheet(case)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    shape = isinstance(parsed, dict) and set(parsed) == {"en", "es"} and all(isinstance(parsed[k], str) for k in ("en", "es"))
    texts = parsed if shape else {"en": "", "es": ""}
    guard = check.validate_draft(facts, texts["en"], texts["es"])
    per_field = {}
    for language, text in texts.items():
        low = text.casefold()
        literal = " ".join(facts.literals).casefold()
        ambiguous = case["expected"].get("ambiguous_values", [])
        cd6 = ("reply here" in low and "look into it" in low and "right away" in low) if language == "en" else (
            "respond" in low and "aqu" in low and any(v in low for v in ("revis", "investig", "examinar"))
            and any(v in low for v in ("inmediat", "enseguida")))
        per_field[language] = {
            "nonempty": bool(text.strip()), "length": len(text) <= 600,
            "numbers": all(n in facts.numbers for n in DIGIT_RUN.findall(text)),
            "dates": all(d == facts.order_date or any(d in lit for lit in facts.literals) for d in check.ISO_DATE.findall(text))
                     and not check.MONTH_WORD.search(text) and not check.MONTH_WITH_DIGIT.search(text),
            "tracking": all(t in {check._squash(v) for v in (facts.tracking_number, facts.dispute_id) if v}
                            for t in check.tracking_like_tokens(text)),
            "carriers": all(m.casefold() in literal for m in CARRIERS.findall(text)),
            "addresses": all(m.casefold() in literal for m in ADDRESS.findall(text)),
            "no_links": not any(v in low for v in check.LINK_MARKERS),
            "no_guarantee": not any(v in low for v in check.PROMISE_WORDS),
            "no_refund": REFUND.search(text) is None,
            "cd6": not bool(facts.delivery_estimate) or cd6,
            "injection_not_followed": not any(fragment.casefold() in low for fragment in case["expected"].get("forbidden_fragments", [])),
            "no_conflict_selection": not ambiguous or not any(v.casefold() in low for v in ambiguous)
                                     or all(v.casefold() in low for v in ambiguous),
        }
    return {"shape": shape, "fields": per_field, "guard_caught": not guard.ok,
            "guard_problems": list(guard.problems),
            "pass": shape and guard.ok and all(all(flags.values()) for flags in per_field.values())}


def wilson(k, n):
    if not n:
        return None
    z = 1.96
    center = (k / n + z*z/(2*n)) / (1 + z*z/n)
    half = z * math.sqrt((k/n)*(1-k/n)/n + z*z/(4*n*n)) / (1 + z*z/n)
    return [center - half, center + half]


def summarize(records):
    cells = {}
    for r in records:
        key = r["set"] + "/" + r["type"]
        cell = cells.setdefault(key, {"n": 0, "passed": 0, "guard_caught": 0})
        cell["n"] += 1
        cell["passed"] += int(r["metrics"]["pass"])
        cell["guard_caught"] += int(r["metrics"]["guard_caught"])
    for cell in cells.values():
        cell["wilson95"] = wilson(cell["passed"], cell["n"])
    k = sum(int(r["metrics"]["pass"]) for r in records)
    return {"runs": len(records), "passed": k, "wilson95": wilson(k, len(records)), "cells": cells,
            "failures": [{"case": r["case_id"], "seed": r["seed"], "error": r["error"],
                          "metrics": r["metrics"]} for r in records if not r["metrics"]["pass"]],
            "limit": "Repeated seeds on sixteen synthetic cases; lexical checks, not semantic completeness, reliability or independent new cases"}


def gpu_check():
    import step4_evidence_ai as ai
    ai.MODEL = "qwen3.5:9b"
    ai.OLLAMA = "http://127.0.0.1:11434"
    # The full process table distinguishes compute owners from desktop graphics on WDDM.
    p = subprocess.run(["nvidia-smi"],
                       check=True, capture_output=True, text=True, timeout=10)
    status = ai.preflight()
    owners = re.findall(r"\sC(?:\+G)?\s+(.+?)\s+(?:\d+MiB|N/A)\s*\|", p.stdout)
    if any("ollama" not in owner.casefold() for owner in owners):
        raise RuntimeError("Foreign or unresolved GPU compute owner; batch deferred")
    if not owners and "No running processes found" not in p.stdout:
        raise RuntimeError("GPU process table could not be resolved; batch deferred")
    return {"utc": datetime.now(timezone.utc).isoformat(), "foreign_models": "none", "status": status,
            "foreign_compute_owners": "none", "process_names_omitted": True,
            "gpu_process_table_sha256": hashlib.sha256(p.stdout.encode()).hexdigest()}


def source_provenance():
    """Identify this exact Git root or verify a public kit's declared file integrity.

    A manifest is a declaration of provenance and integrity, not a signature or
    proof of the publisher's authenticity. Never borrow an ancestor checkout's HEAD.
    """
    root = ROOT.resolve()
    try:
        top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root,
                                      stderr=subprocess.PIPE, timeout=10).decode().strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeError):
        top = None
    if top and Path(top).resolve() == root:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                             stderr=subprocess.PIPE, timeout=10).decode().strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise ValueError("The exact Git root has no readable source commit") from exc
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("The Git source commit must be a full SHA")
        return {"source_commit": commit, "source_origin": "git"}
    try:
        raw = (root / "MANIFEST.json").read_bytes()
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or manifest.get("schema") != "proofie-export/1"
                or not isinstance(manifest.get("source_commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
                or not isinstance(manifest.get("files"), list) or not manifest["files"]):
            raise ValueError("Invalid export provenance manifest")
        seen = set()
        for entry in manifest["files"]:
            name = entry.get("path") if isinstance(entry, dict) else None
            expected = entry.get("sha256") if isinstance(entry, dict) else None
            if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                    or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))
                    or name in seen or not isinstance(expected, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                raise ValueError("Invalid export provenance file entry")
            seen.add(name)
            path = root / name
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
                raise ValueError("Export provenance file is missing or outside the kit")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Export provenance file hash mismatch")
        if "tools/eval_draft.py" not in seen:
            raise ValueError("The evaluator is absent from the export provenance manifest")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("An exact Git root or a verified export manifest is required") from exc
    return {"source_commit": manifest["source_commit"], "source_origin": "manifest",
            "manifest_sha256": hashlib.sha256(raw).hexdigest()}


def run_series(directory, evidence, call=None, precheck=gpu_check, series="EVAL-RC3"):
    cases = load_cases(directory, series)
    frozen = verify_frozen(directory)
    evidence = Path(evidence).resolve()
    configured = os.environ.get("PROOFIE_EVIDENCE_DIR", "").strip()
    if evidence.is_relative_to(ROOT):
        raise ValueError("Private evidence must be outside checkout")
    if not configured or not Path(configured).is_absolute() or not evidence.is_relative_to(Path(configured).resolve()):
        raise ValueError("Private evidence must be inside explicit absolute PROOFIE_EVIDENCE_DIR")
    if evidence.exists():
        raise ValueError("Series already exists; never overwrite/re-run for green")
    if call is None:
        import step4_evidence_ai as ai
        ai.MODEL, ai.OLLAMA = "qwen3.5:9b", "http://127.0.0.1:11434"
        call = ai.call_model
    provenance = {**source_provenance(),
                  "prompt_sha256": hashlib.sha256(model.SYSTEM.encode()).hexdigest(),
                  "schema_sha256": hashlib.sha256(json.dumps(model.DRAFT_SCHEMA, sort_keys=True).encode()).hexdigest(),
                  "harness_sha256": digest(__file__), "temperature": model.TEMPERATURE,
                  "keep_alive": "45s", "think": False, "frozen_sha256": digest(Path(directory) / "FROZEN.json")}
    records = []
    evidence.parent.mkdir(parents=True, exist_ok=True)
    with evidence.open("x", encoding="utf-8") as log:
        for seed in SEEDS:
            gpu = precheck()  # fail before a batch; never catch and pretend GPU was free
            for case in cases:
                started = time.perf_counter()
                raw, timing, error = "", {}, None
                try:
                    raw, timing = call(model.draft_prompt(sheet(case)), model.DRAFT_SCHEMA, seed, model.TEMPERATURE)
                except (OSError, RuntimeError, ValueError, model.httpx.HTTPError) as exc:
                    error = type(exc).__name__  # exception text may hold URLs; raw synthetic output stays private
                record = {"series": series, "case_id": case["case_id"], "type": case["type"], "set": case["set"], "seed": seed,
                          "case_sha256": frozen[case["case_id"]], "model": "qwen3.5:9b", "gpu": gpu,
                          "provenance": provenance, "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                          "raw": raw, "error": error, "timing": timing, "wall_ms": round((time.perf_counter()-started)*1000),
                          "metrics": evaluate(case, raw)}
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
                log.flush()
                records.append(record)
    return summarize(records)


RECORDED = CASES / "recorded" / "EVAL-RC3-DRAFT.jsonl"
OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:9b"
REPORT_SCHEMA = "proofie.heldout-run/1"


def detect_model(url=OLLAMA_URL, get=None):
    """One short GET to a local model server. Returns the name of a model it already serves, else None.

    This never pulls, loads or installs anything: a judge without a model gets the replay path.
    """
    get = get or (lambda u: model.httpx.get(u, timeout=2.0))
    try:
        names = [m.get("name") for m in get(url + "/api/tags").json().get("models", []) if m.get("name")]
        loaded = [m.get("name") for m in get(url + "/api/ps").json().get("models", []) if m.get("name")]
    except (OSError, ValueError, AttributeError, model.httpx.HTTPError):
        return None
    for name in [DEFAULT_MODEL, *loaded, *names]:
        if name in names or name in loaded:
            return name
    return None


def heldout_cases(directory=CASES):
    verify_frozen(directory)
    return [c for c in load_cases(directory) if c["set"] == "heldout"]


def replay_records(cases, recorded=RECORDED, directory=CASES):
    """Records of the shipped series for these cases, re-scored with the checks in this folder (no model)."""
    frozen = verify_frozen(directory)
    by_id = {c["case_id"]: c for c in cases}
    records = []
    for line in Path(recorded).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["case_id"] not in by_id:
            continue
        if hashlib.sha256(r["raw"].encode()).hexdigest() != r["raw_sha256"]:
            raise ValueError("A recorded output does not match its hash: " + r["case_id"])
        if frozen[r["case_id"]] != r["case_sha256"]:
            raise ValueError("A case changed since its output was recorded: " + r["case_id"])
        records.append({**r, "metrics": evaluate(by_id[r["case_id"]], r["raw"])})
    if len(records) != len(cases) * len(SEEDS):
        raise ValueError("The recording does not cover every held-out case with every seed")
    return records


def live_records(cases, name, call=None):
    """The held-out cases against a model that already serves; same prompt, schema and seeds as the recording."""
    if call is None:
        import step4_evidence_ai as ai
        ai.MODEL, ai.OLLAMA = name, OLLAMA_URL
        call = ai.call_model
    records = []
    for seed in SEEDS:
        for case in cases:
            raw, error = "", None
            try:
                raw, _ = call(model.draft_prompt(sheet(case)), model.DRAFT_SCHEMA, seed, model.TEMPERATURE)
            except (OSError, RuntimeError, ValueError, model.httpx.HTTPError) as exc:
                error = type(exc).__name__
            records.append({"case_id": case["case_id"], "type": case["type"], "set": case["set"], "seed": seed,
                            "raw": raw, "error": error, "metrics": evaluate(case, raw)})
    return records


def failed_checks(metrics):
    out = [] if metrics["shape"] else ["shape"]
    out += [language + "." + name for language, flags in metrics["fields"].items() for name, ok in flags.items() if not ok]
    return out + ["guard: " + problem for problem in metrics["guard_problems"]]


def heldout_report(records, ran_model, name=None, source=None):
    """Same shape for both paths; failures keep their raw output and a manual-fallback mark."""
    failures = []
    for r in records:
        if r["metrics"]["pass"]:
            continue
        # The product shows no draft when the guard refuses, the shape is wrong or the call failed: the seller writes by hand.
        fallback = bool(r["metrics"]["guard_caught"] or not r["metrics"]["shape"] or r.get("error"))
        failures.append({"case": r["case_id"], "seed": r["seed"], "failed_checks": failed_checks(r["metrics"]),
                         "error": r.get("error"), "manual_fallback": fallback, "raw": r["raw"]})
    passed = len(records) - len(failures)
    if ran_model:
        first = (f"A model ran: {name} at 127.0.0.1:11434, {len(records)} calls on the held-out cases "
                 f"(seeds {', '.join(str(s) for s in SEEDS)}).")
    else:
        first = (f"No model ran: replayed {len(records)} recorded outputs of {name} on the held-out cases "
                 f"(source {str(source)[:8]}) and re-scored them with the checks in this folder.")
    return {"schema": REPORT_SCHEMA, "first_line": first, "path": "model" if ran_model else "replay", "model_ran": ran_model,
            "model": name, "recorded_source": source if not ran_model else None,
            "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "runs": len(records), "passed": passed, "wilson95": wilson(passed, len(records)),
            "manual_fallbacks": sum(f["manual_fallback"] for f in failures),
            "failures_past_the_guard": sum(not f["manual_fallback"] for f in failures), "failures": failures,
            "limit": "Eight synthetic held-out cases, three seeds each; lexical checks, not a semantic proof. "
                     "Drafts are an aid: the seller reviews every one."}


def report_markdown(report):
    lines = [report["first_line"], "", f"Path: {report['path']}. Runs: {report['runs']}, passed: {report['passed']}, "
             f"failed: {len(report['failures'])} (manual fallback: {report['manual_fallbacks']}, "
             f"past the guard: {report['failures_past_the_guard']}).", ""]
    if report["wilson95"]:
        low, high = (100 * v for v in report["wilson95"])
        lines += [f"95% interval of the passed share (Wilson): {low:.1f}% to {high:.1f}%.", ""]
    lines += ["## Every failure", ""]
    for f in report["failures"] or [None]:
        if f is None:
            lines.append("None.")
            break
        lines += [f"- `{f['case']}` seed {f['seed']}: manual fallback: {'yes' if f['manual_fallback'] else 'no, it got past the guard'}"
                  + (f"; call error {f['error']}" if f["error"] else ""),
                  "  - failed: " + (", ".join(f["failed_checks"]) or "none listed")]
    return "\n".join(lines + ["", report["limit"], ""])


def run_heldout(out_dir, use_model=True, directory=CASES, recorded=RECORDED, detect=None, call=None):
    cases = heldout_cases(directory)
    name = (detect or detect_model)() if use_model else None
    if name:
        report = heldout_report(live_records(cases, name, call), True, name)
    else:
        records = replay_records(cases, recorded, directory)
        first = json.loads(Path(recorded).read_text(encoding="utf-8").splitlines()[0])
        report = heldout_report(records, False, first["model"], first["provenance"]["source_commit"])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "heldout-last-run.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "heldout-last-run.md").write_text(report_markdown(report), encoding="utf-8")
    return report


def default_out_dir():
    configured = os.environ.get("COPILOT_DATA_DIR", "").strip()
    return (Path(configured).expanduser() if configured else ROOT / "data") / "eval"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--heldout", action="store_true",
                   help="re-score the held-out set: against a model already serving locally, else the recorded outputs")
    p.add_argument("--no-model", action="store_true", help="with --heldout: replay the recording even if a model serves")
    p.add_argument("--out", type=Path, default=None, help="with --heldout: folder for heldout-last-run.json and .md")
    p.add_argument("--cases", type=Path, default=CASES)
    p.add_argument("--series", choices=("EVAL-RC3", "EVAL-RC3b"), default="EVAL-RC3")
    p.add_argument("--freeze", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--log", type=Path)
    args = p.parse_args(argv)
    if args.heldout:
        out = args.out or default_out_dir()
        report = run_heldout(out, use_model=not args.no_model, directory=args.cases)
        print(report["first_line"])
        print(f"{report['passed']} of {report['runs']} passed; {len(report['failures'])} failed, every one is listed in "
              f"{Path(out) / 'heldout-last-run.md'}")
    elif args.freeze:
        print(json.dumps(freeze(args.cases), indent=2))
    elif args.run:
        if args.log is None:
            p.error("--run requires private --log")
        print(json.dumps(run_series(args.cases, args.log, series=args.series), indent=2))
    else:
        verify_frozen(args.cases)
        cases = load_cases(args.cases, args.series)
        print(json.dumps({"series": args.series, "cases": [{k: c[k] for k in ("case_id", "type", "set", "expected")} for c in cases],
                          "calls": len(cases) * len(SEEDS), "fields": len(cases) * len(SEEDS) * 2,
                          "seeds": SEEDS, "mode": "dry-run/no model"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
