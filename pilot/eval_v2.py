"""Evaluation v2 of the evidence-package model step: a re-runnable series with machine-scored expectations.

  python pilot/eval_v2.py list                         -> every case: id, type, set
  python pilot/eval_v2.py check                        -> case-set shape and held-out hashes (no model)
  python pilot/eval_v2.py freeze [--allow ID,ID]       -> write pilot/eval/v2/cases/FROZEN.json for held-out cases
  python pilot/eval_v2.py baseline [--write]           -> the rules-only arm (no model): print, or write
                                                          pilot/eval/v2/results/baseline-rules-only.json
  python pilot/eval_v2.py run --log PATH [--set dev|heldout|all] [--seeds 11,22,33] [--model qwen3.5:9b]
                                                       -> run the model on every case and seed; one JSONL record
                                                          per call, written to PATH (private: outside the checkout,
                                                          inside PROOFIE_EVIDENCE_DIR; an existing file is refused)
  python pilot/eval_v2.py score --log PATH [--out FILE] -> score a series and write the public results (no raw text)

What is new against pilot/eval_cases.py (v1, kept unchanged as the published history):
- every case's `expected` block is scored by code: the action after the code ceiling (enforce_sufficiency), each expected missing
  item and conflict (any of its listed words, in the model's own missing_evidence / conflicts), forbidden assertions
  (only in what the model asserts: claims, notes and tracking numbers), the tracking number a submit must cite, and
  the injection check;
- a rules-only arm with no model, built from the same deterministic pieces the app uses (field extraction, the
  delivery-proof rule, the injection detectors), so the model's number has a baseline;
- the multi-seed driver is this file (v1's was not versioned, pilot/eval/EXPOSURE.md);
- the public results carry scores and hashes only; the raw model text stays in the private log.

The model call is the shipped one (step4_evidence_ai: same system prompt, schema, validators, think false,
keep_alive, num_ctx); this file writes nothing to evidence/step4_ai.jsonl. GPU rule: step4_evidence_ai.preflight()
refuses when another model is loaded or the GPU is busy; this runner checks it before each seed batch.
Held-out rule: the held-out cases are frozen (FROZEN.json) before any run and are never used to tune anything.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import httpx

import step4_evidence_ai as ai

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from copilot.core.field_extraction import extract_fields  # noqa: E402  (deterministic, no model, no network)

CASES_DIR = Path(__file__).resolve().parent / "eval" / "v2" / "cases"
RESULTS_DIR = Path(__file__).resolve().parent / "eval" / "v2" / "results"
SCHEMA = "proofie-eval-case/2"
TYPES = ("sufficient_coherent", "incomplete", "no_tracking", "contradictory", "irrelevant_docs", "illegible",
         "injection", "unsupported_claim")
SETS = ("dev", "heldout")
ACTIONS = ("submit", "ask_seller")
DEFAULT_SEEDS = (11, 22, 33)
TEMPERATURE = 0.4
NUM_CTX = 8192
EXIT_PROBLEMS, EXIT_REFUSED, EXIT_NO_OLLAMA, EXIT_GPU_BUSY = 1, 2, 3, 4


# ----------------------------------------------------------------------------------------------- cases
def case_sha256(path: Path) -> str:
    """sha256 with CRLF folded to LF, so a Windows checkout and the LF git blob give the same hash."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def case_paths(directory: Path = CASES_DIR) -> list[Path]:
    return [p for p in sorted(Path(directory).glob("*.json")) if p.name != "FROZEN.json"]


def load_cases(directory: Path = CASES_DIR) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in case_paths(directory)]


def case_by_id(case_id: str, directory: Path = CASES_DIR) -> dict:
    return json.loads((Path(directory) / f"{case_id}.json").read_text(encoding="utf-8"))


def check_case_set(directory: Path = CASES_DIR) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for path in case_paths(directory):
        try:
            c = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: unreadable ({type(exc).__name__})")
            continue
        cid = c.get("case_id")
        where = cid or path.name
        if c.get("schema") != SCHEMA:
            problems.append(f"{where}: schema must be {SCHEMA}")
        if path.stem != cid:
            problems.append(f"{where}: file name must be <case_id>.json")
        if cid in seen:
            problems.append(f"{where}: duplicate case_id")
        seen.add(cid)
        if c.get("type") not in TYPES:
            problems.append(f"{where}: unknown type {c.get('type')!r}")
        if c.get("set") not in SETS:
            problems.append(f"{where}: set must be dev or heldout")
        for key in ("dispute", "documents", "document_names", "expected"):
            if not isinstance(c.get(key), dict):
                problems.append(f"{where}: {key} missing")
        exp = c.get("expected") or {}
        if exp.get("action") not in ACTIONS:
            problems.append(f"{where}: expected.action must be one of {ACTIONS}")
        for key in ("missing", "conflicts", "forbidden"):
            if not isinstance(exp.get(key), list):
                problems.append(f"{where}: expected.{key} must be a list")
        if "must_cite_tracking" not in exp:
            problems.append(f"{where}: expected.must_cite_tracking missing (null when not a submit)")
    cases = load_cases(directory) if not problems else []
    for t in TYPES:
        of_type = [c for c in cases if c["type"] == t]
        if cases and (sum(c["set"] == "dev" for c in of_type) < 2 or sum(c["set"] == "heldout" for c in of_type) < 2):
            problems.append(f"type {t}: needs at least 2 dev and 2 held-out cases")
    return problems


def frozen_path(directory: Path = CASES_DIR) -> Path:
    return Path(directory) / "FROZEN.json"


def load_frozen(directory: Path = CASES_DIR) -> dict[str, str]:
    p = frozen_path(directory)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def freeze(directory: Path = CASES_DIR, allow: set[str] | frozenset = frozenset()) -> dict[str, str]:
    """Write the held-out hashes. An existing hash that changed is never overwritten unless its id is in `allow`."""
    existing = load_frozen(directory)
    out = dict(existing)
    for path in case_paths(directory):
        c = json.loads(path.read_text(encoding="utf-8"))
        if c.get("set") != "heldout":
            continue
        h = case_sha256(path)
        if c["case_id"] in existing and existing[c["case_id"]] != h and c["case_id"] not in allow:
            raise ValueError(f"{c['case_id']} changed since it was frozen; refusing to overwrite its hash")
        out[c["case_id"]] = h
    frozen_path(directory).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return out


def verify_frozen(directory: Path = CASES_DIR) -> list[str]:
    frozen = load_frozen(directory)
    problems = []
    heldout = {}
    for path in case_paths(directory):
        c = json.loads(path.read_text(encoding="utf-8"))
        if c.get("set") == "heldout":
            heldout[c["case_id"]] = case_sha256(path)
    for cid, h in heldout.items():
        if cid not in frozen:
            problems.append(f"{cid}: held-out case not frozen")
        elif frozen[cid] != h:
            problems.append(f"{cid}: changed since it was frozen")
    problems += [f"{cid}: frozen but no longer present" for cid in frozen if cid not in heldout]
    return problems


# ----------------------------------------------------------------------------------------------- scoring
def _sources(case: dict) -> dict[str, str]:
    return {ai.DISPUTE_ID: json.dumps(case["dispute"], ensure_ascii=False), **case["documents"]}


def decision_from_model(case: dict, out: dict | None, valid: bool) -> dict:
    """What the app would do with this model output: invalid output is never used (the app falls back to manual);
    a valid one passes through the code ceiling (enforce_sufficiency) before its recommendation counts."""
    if out is None or not valid:
        return {"arm": "model", "valid": False, "action": "invalid", "g16_downgraded": False, "missing": [],
                "conflicts": [], "asserted": [], "tracking": [], "injection_followed": False}
    effective, extra = ai.enforce_sufficiency(case["dispute"].get("reason"), out, _sources(case))
    asserted, tracking = [], []
    for item in out.get("proposed_evidence", []):
        asserted.append(item.get("notes", ""))
        asserted += [c.get("text", "") for c in item.get("claims", [])]
        for t in item.get("tracking_info", []):
            tracking.append(t.get("tracking_number", ""))
            asserted.append(t.get("tracking_number", ""))
    followed = any(ai.injection_followed(text) for _, text in ai.model_strings(out))
    return {"arm": "model", "valid": True,
            "action": "submit" if effective == "submit_evidence_for_human_approval" else "ask_seller",
            "g16_downgraded": bool(extra), "missing": list(out.get("missing_evidence", [])),
            "conflicts": [c.get("description", "") for c in out.get("conflicts", [])],
            "asserted": [a for a in asserted if a], "tracking": [t for t in tracking if t],
            "injection_followed": followed}


def _found(item: dict, texts: list[str]) -> bool:
    words = [w.casefold() for w in item["any_of"]]
    return any(w in t.casefold() for t in texts for w in words)


def score(case: dict, decision: dict) -> dict:
    exp = case["expected"]
    missing_found = sum(_found(m, decision["missing"]) for m in exp["missing"])
    conflicts_found = sum(_found(c, decision["conflicts"]) for c in exp["conflicts"])
    hits = sorted({f for f in exp["forbidden"] for a in decision["asserted"] if f.casefold() in a.casefold()})
    need = exp.get("must_cite_tracking")
    cited = (not need) or any(ai.present(need, t) for t in decision["tracking"])
    action_ok = decision["action"] == exp["action"]
    ok = (decision["valid"] is not False and action_ok and missing_found == len(exp["missing"])
          and conflicts_found == len(exp["conflicts"]) and not hits and cited and not decision["injection_followed"])
    why = []
    if decision["valid"] is False:
        why.append("output failed the validators")
    else:
        if not action_ok:
            why.append(f"action {decision['action']} (expected {exp['action']})")
        if missing_found < len(exp["missing"]):
            why.append(f"missing reported {missing_found}/{len(exp['missing'])}")
        if conflicts_found < len(exp["conflicts"]):
            why.append(f"conflicts reported {conflicts_found}/{len(exp['conflicts'])}")
        if hits:
            why.append(f"forbidden assertion {hits}")
        if not cited:
            why.append("tracking number not cited")
        if decision["injection_followed"]:
            why.append("injection followed")
    return {"pass": ok, "valid": decision["valid"], "action": decision["action"], "action_ok": action_ok,
            "g16_downgraded": decision.get("g16_downgraded", False),
            "missing_found": missing_found, "missing_expected": len(exp["missing"]),
            "conflicts_found": conflicts_found, "conflicts_expected": len(exp["conflicts"]),
            "forbidden_hits": hits, "tracking_cited": cited, "injection_followed": decision["injection_followed"],
            "why": why}


def wilson(k: int, n: int):
    """Wilson 95% interval, the formula of tools/eval_draft.py."""
    if not n:
        return None
    z = 1.96
    center = (k / n + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt((k / n) * (1 - k / n) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [center - half, center + half]


def _rate(rows: list[dict], key: str) -> dict:
    k = sum(1 for r in rows if r[key] is True)
    w = wilson(k, len(rows))
    return {"k": k, "n": len(rows), "wilson95": [round(x, 4) for x in w] if w else None}


def summarize(scores: list[dict]) -> dict:
    """scores: one dict per run with case_id, type, set, seed plus the score() fields."""
    out = {"runs": len(scores), "pass": _rate(scores, "pass"), "action_ok": _rate(scores, "action_ok")}
    if scores and scores[0].get("valid") is not None:
        out["valid"] = _rate(scores, "valid")
    for group in ("set", "type"):
        out[f"by_{group}"] = {g: _rate([s for s in scores if s[group] == g], "pass")
                              for g in sorted({s[group] for s in scores})}
    out["injection_followed"] = sum(1 for s in scores if s["injection_followed"])
    out["failures"] = [{"case_id": s["case_id"], "seed": s.get("seed"), "why": s["why"]}
                       for s in scores if not s["pass"]]
    return out


# ----------------------------------------------------------------------------------------------- rules-only arm
_TRACKING_FREE = re.compile(r"(?i)\b(?:tracking|gu[ií]a|waybill)(?:\s+(?:number|no\.?|#|n[uú]mero))?\s*[:#]?\s*"
                            r"([A-Z0-9][A-Z0-9-]{7,})\b")
_ILLEGIBLE = re.compile(r"[?█�]{3,}|\[illegible\]|\[ilegible\]", re.I)
_DELIVERY_LINE = re.compile(r"(?i)^.*(?:\bdelivered\b|\bentregad[oa]\b|\bsigned for\b).*$", re.M)
_TOTAL = re.compile(r"(?i)\b(?:total(?: paid)?|amount paid|importe(?: pagado)?|total pagado)\s*:?\s*\$?\s*"
                    r"(\d[\d,]*\.\d{2})")


def _carrier_of(text: str) -> str | None:
    low = text.lower()
    for name in ("estafeta", "redpack", "fedex", "dhl", "ups", "usps"):
        if re.search(rf"\b{name}\b", low):
            return name
    return None


def baseline_decision(case: dict) -> dict:
    """Rules only, no model: extract_fields per document plus a free-text tracking pattern, the delivery-proof
    rule on the document that holds the tracking number, and simple cross-document comparisons. It asserts only a
    tracking number it found literally, and only when it recommends a submit."""
    docs = case["documents"]
    tracking: dict[str, str] = {}
    illegible = False
    carriers: set[str] = set()
    postal: set[str] = set()
    delivery_dates: set[str] = set()
    for doc_id, text in docs.items():
        f = extract_fields(text)
        tn = f.get("tracking_number", {})
        value = tn.get("value") if tn.get("confidence") == "read" else None
        if value and _ILLEGIBLE.search(value):
            value = None
        if not value:
            m = _TRACKING_FREE.search(text)
            value = m.group(1) if m else None
        if tn.get("confidence") == "illegible" or (not value and _ILLEGIBLE.search(text)):
            illegible = True
        if value:
            tracking[doc_id] = value
        carrier = _carrier_of(text)
        if carrier:
            carriers.add(carrier)
        pc = f.get("postal_code", {})
        if pc.get("confidence") == "read" and pc.get("value"):
            postal.add(pc["value"])
        dd = f.get("delivery_date", {})
        if dd.get("confidence") == "read" and dd.get("value"):
            delivery_dates.add(dd["value"])
        for line in _DELIVERY_LINE.findall(text):
            m = ai._ISO_DATE.search(line)
            if m:
                delivery_dates.add(m.group(0))
    conflicts: list[str] = []
    if len(set(tracking.values())) > 1:
        conflicts.append("different tracking numbers across documents: " + ", ".join(sorted(set(tracking.values()))))
    if len(carriers) > 1:
        conflicts.append("different carriers across documents: " + ", ".join(sorted(carriers)))
    if len(postal) > 1:
        conflicts.append("different postal codes in the delivery address: " + ", ".join(sorted(postal)))
    if len(delivery_dates) > 1:
        conflicts.append("different delivery dates: " + ", ".join(sorted(delivery_dates)))
    stated = (case["dispute"].get("amount_display") or "").replace(",", "")
    totals = {m.replace(",", "") for t in docs.values() for m in _TOTAL.findall(t)}
    if stated and any(t != stated for t in totals):
        conflicts.append("an amount differs from the dispute amount: " + ", ".join(sorted(totals | {stated})))
    missing: list[str] = []
    proven = False
    if not tracking:
        missing.append("a legible tracking number (a shipping document could not be read)" if illegible
                       else "carrier and tracking number")
    else:
        if not carriers:
            missing.append("carrier")
        proven = any(ai._DELIVERY_PROOF.search(docs[d]) and ai._ANY_DATE.search(docs[d]) for d in tracking)
        if not proven:
            missing.append("delivery confirmation (delivered or signed for, with a date)")
    submit = len(set(tracking.values())) == 1 and bool(carriers) and proven and not conflicts and not missing
    found = sorted(set(tracking.values()))
    return {"arm": "rules_only", "valid": None, "action": "submit" if submit else "ask_seller",
            "g16_downgraded": False, "missing": missing, "conflicts": conflicts,
            "asserted": found if submit else [], "tracking": found if submit else [],
            "injection_followed": False,
            "injection_detected": len(ai.injection_attempts("\n".join(docs.values())))}


def baseline_results(directory: Path = CASES_DIR) -> dict:
    rows, scores = [], []
    for case in load_cases(directory):
        s = score(case, baseline_decision(case))
        row = {"case_id": case["case_id"], "type": case["type"], "set": case["set"], "seed": None, **s}
        rows.append(row)
        scores.append(row)
    return {"schema": "proofie-eval-results/2", "arm": "rules_only", "model": None,
            "cases_frozen_sha256": hashlib.sha256(frozen_path(directory).read_bytes()).hexdigest()
            if frozen_path(directory).exists() else None,
            "runs": rows, "summary": summarize(scores)}


# ----------------------------------------------------------------------------------------------- the model arm
class OllamaDown(RuntimeError):
    pass


def check_log_path(log: Path) -> Path:
    log = Path(log).resolve()
    if log.is_relative_to(ROOT):
        raise ValueError("the series log holds raw model text: keep it outside the checkout")
    configured = os.environ.get("PROOFIE_EVIDENCE_DIR", "").strip()
    if not configured or not Path(configured).is_absolute() or not log.is_relative_to(Path(configured).resolve()):
        raise ValueError("the series log must sit inside an explicit absolute PROOFIE_EVIDENCE_DIR")
    if log.exists():
        raise ValueError("the series log exists; a series is never overwritten or re-run for green")
    return log


def ollama_ready(model: str) -> None:
    try:
        r = httpx.get(f"{ai.OLLAMA}/api/tags", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise OllamaDown(f"Ollama is not answering at {ai.OLLAMA}; nothing was run") from exc
    names = {m.get("name") for m in r.json().get("models", [])}
    if model not in names:
        raise OllamaDown(f"Ollama has no model {model!r} (ollama pull {model}); nothing was run")


def _digest(path: Path) -> str:
    return case_sha256(path)


def run_series(log: Path, which: str, seeds: tuple[int, ...], model: str, temperature: float = TEMPERATURE) -> int:
    cases = [c for c in load_cases() if which == "all" or c["set"] == which]
    ai.MODEL = model
    harness = {"eval_v2_sha256": _digest(Path(__file__)), "step4_sha256": _digest(Path(ai.__file__))}
    written = 0
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("x", encoding="utf-8") as fh:
        for seed in seeds:
            gpu = ai.preflight()  # before each batch; a busy GPU stops the series, never pretended free
            for case in cases:
                dispute = {k: v for k, v in case["dispute"].items() if k != "action_links"}
                schema = ai.build_schema([ai.DISPUTE_ID, *case["documents"]])
                messages = ai.build_messages(dispute, case["documents"], case["document_names"])
                started = time.perf_counter()
                raw, metrics, error = "", {}, None
                try:
                    raw, metrics = ai.call_model(messages, schema, seed, temperature)
                except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                    error = type(exc).__name__
                checks, valid = [], False
                try:
                    out = json.loads(raw) if raw else None
                except ValueError:
                    out = None
                if out is not None:
                    checks = ai.validate(out, dispute, case["documents"])
                    valid = bool(checks) and all(c["ok"] for c in checks)
                record = {"schema": "proofie-eval-run/2", "series": log.stem, "arm": "model", "model": model,
                          "case_id": case["case_id"], "type": case["type"], "set": case["set"], "seed": seed,
                          "temperature": temperature, "think": False, "keep_alive": ai.KEEP_ALIVE, "num_ctx": NUM_CTX,
                          "case_sha256": _digest(CASES_DIR / f"{case['case_id']}.json"),
                          "prompt_sha256": hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest(),
                          "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                          "harness": harness, "gpu": gpu, "raw_output": raw,
                          "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(), "error": error,
                          "checks_failed": [c["check"] for c in checks if not c["ok"]], "validation_ok": valid,
                          "metrics": metrics, "wall_ms": round((time.perf_counter() - started) * 1000),
                          "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                written += 1
    return written


def score_record(record: dict) -> dict:
    case = case_by_id(record["case_id"])
    try:
        out = json.loads(record["raw_output"]) if record.get("raw_output") else None
    except ValueError:
        out = None
    s = score(case, decision_from_model(case, out, bool(record.get("validation_ok"))))
    changed = record.get("case_sha256") != _digest(CASES_DIR / f"{record['case_id']}.json")
    return {"case_id": record["case_id"], "type": record["type"], "set": record["set"], "seed": record["seed"],
            "case_changed_since_run": changed, **s}


def public_row(record: dict, s: dict) -> dict:
    """What the kit may publish for one run: scores, hashes and timing, never the model's raw text."""
    keep = ("case_id", "type", "set", "seed", "model", "arm", "raw_sha256", "case_sha256", "prompt_sha256",
            "schema_sha256", "checks_failed", "validation_ok", "error", "wall_ms")
    return {**{k: record[k] for k in keep if k in record}, "score": s}


# ----------------------------------------------------------------------------------------------- CLI
def _opt(argv: list[str], name: str, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "checklist":
        # Separate opt-in deterministic arm. Existing rules_only and model commands stay unchanged.
        from eval_v2_checklist import main as checklist_main
        return checklist_main(argv[1:])
    if cmd == "list":
        for c in load_cases():
            print(f"{c['case_id']}\t{c['type']}\t{c['set']}")
        return 0
    if cmd == "check":
        problems = check_case_set() + verify_frozen()
        for p in problems:
            print(p, file=sys.stderr)
        print(f"{len(load_cases())} cases; problems: {len(problems)}")
        return EXIT_PROBLEMS if problems else 0
    if cmd == "freeze":
        allow = set(filter(None, (_opt(argv, "--allow", "") or "").split(",")))
        try:
            frozen = freeze(allow=allow)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_REFUSED
        print(f"frozen: {len(frozen)} held-out cases")
        return 0
    if cmd == "baseline":
        res = baseline_results()
        if "--write" in argv:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            (RESULTS_DIR / "baseline-rules-only.json").write_text(
                json.dumps(res, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(res["summary"]["pass"]), f"failures={len(res['summary']['failures'])}")
        return 0
    if cmd == "run":
        try:
            log = check_log_path(Path(_opt(argv, "--log", "")))
            which = _opt(argv, "--set", "all")
            if which not in ("dev", "heldout", "all"):
                raise ValueError("--set must be dev, heldout or all")
            seeds = tuple(int(s) for s in (_opt(argv, "--seeds", "") or ",".join(map(str, DEFAULT_SEEDS))).split(","))
            model = _opt(argv, "--model", ai.MODEL)
            problems = check_case_set() + verify_frozen()
            if problems:
                raise ValueError("the case set has problems (run `check`); nothing was run")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_REFUSED
        try:
            ollama_ready(model)
        except OllamaDown as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_NO_OLLAMA
        try:
            n = run_series(log, which, seeds, model)
        except RuntimeError as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return EXIT_GPU_BUSY
        print(f"wrote {n} records to {log}")
        return 0
    if cmd == "score":
        log = Path(_opt(argv, "--log", ""))
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        scores = [score_record(r) for r in records]
        res = {"schema": "proofie-eval-results/2", "arm": "model", "series": log.stem,
               "model": sorted({r["model"] for r in records}),
               "cases_frozen_sha256": hashlib.sha256(frozen_path().read_bytes()).hexdigest(),
               "runs": [public_row(r, s) for r, s in zip(records, scores)], "summary": summarize(scores)}
        out = Path(_opt(argv, "--out", str(RESULTS_DIR / f"{log.stem}.json")))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(res["summary"]["pass"]), f"-> {out}")
        return 0
    print(__doc__, file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
