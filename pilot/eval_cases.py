"""Case-based evaluation harness. Replaces the earlier "<=30 runs/scenario" measure as the main one;
the earlier thresholds stay as an engineering reference, applied per case.

  python eval_cases.py list                                  -> every case: id, type, set (dev/heldout)
  python eval_cases.py verify-frozen [--frozen-file PATH] [--ids a,b,c]
                                                                -> check heldout files vs FROZEN.json (no model)
  python eval_cases.py freeze [--frozen-file PATH] [--ids a,b,c]
                                                                -> write/refresh FROZEN.json for heldout cases
                                                                   (refuses to silently overwrite a changed one)
  python eval_cases.py run --set dev [--case ID] [--seed N] [--repeats N]
                                                                -> run dev case(s) through the model
  python eval_cases.py run --case ID --allow-heldout           -> run exactly one heldout case (explicit)

--frozen-file/--ids let a later "round" of heldout cases (e.g. the 6 from docs/DECISIONS.md D-2026-10-04-02)
freeze into their own file, independent of an earlier round's FROZEN.json.

GPU rule: check that the GPU is free, with `nvidia-smi`
and http://127.0.0.1:11434/api/ps first (ai.preflight() already does this and refuses if another
project using the same GPU holds it).

Each case file (pilot/eval/cases/<case_id>.json) is self-contained: dispute + inline documents + a
pre-registered `expected` block (written before any run). Running a case never edits its
file; results go to evidence/step4_ai.jsonl exactly like every other step4 run (case=<type>, label=<case_id>).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import step4_evidence_ai as ai

CASES_DIR = Path(__file__).resolve().parent / "eval" / "cases"
FROZEN_PATH = CASES_DIR / "FROZEN.json"
REQUIRED_TYPES = {"sufficient_coherent", "incomplete", "no_tracking", "contradictory", "irrelevant_docs",
                  "illegible", "injection", "unsupported_claim"}


def case_file(case_id: str) -> Path:
    return CASES_DIR / f"{case_id}.json"


def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))
           if not p.name.startswith("FROZEN")]


def load_case(case_id: str) -> dict:
    return json.loads(case_file(case_id).read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    """sha256 of the file with every CRLF turned into LF, so the frozen hash is the same in an LF checkout (git, the
    export, Linux, macOS) and in a CRLF one (Windows with autocrlf). The git blob of a case file is LF."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def check_case_set_shape(cases: list[dict] | None = None) -> list[str]:
    """Structural sanity, independent of the model: every required type has >=1 dev and >=1 heldout case,
    every case has the fields S:E requires pre-registered, and case_id matches its filename."""
    cases = cases if cases is not None else load_cases()
    problems = []
    by_type: dict[str, dict[str, int]] = {}
    for c in cases:
        for field in ("case_id", "type", "set", "dispute", "documents", "document_names", "expected"):
            if field not in c:
                problems.append(f"{c.get('case_id', '?')}: missing field '{field}'")
        if c.get("set") not in ("dev", "heldout"):
            problems.append(f"{c.get('case_id')}: set must be 'dev' or 'heldout', got {c.get('set')!r}")
        exp = c.get("expected", {})
        for field in ("allowed_facts", "forbidden_claims", "expected_conflicts", "expected_missing",
                      "expected_action"):
            if field not in exp:
                problems.append(f"{c.get('case_id')}: expected.{field} missing")
        if exp.get("expected_action") not in ("submit", "ask_seller", "block"):
            problems.append(f"{c.get('case_id')}: expected_action must be submit/ask_seller/block, "
                            f"got {exp.get('expected_action')!r}")
        by_type.setdefault(c.get("type"), {"dev": 0, "heldout": 0})
        if c.get("set") in ("dev", "heldout"):
            by_type[c["type"]][c["set"]] += 1
    missing_types = REQUIRED_TYPES - set(by_type)
    if missing_types:
        problems.append(f"missing required case types: {sorted(missing_types)}")
    for t, counts in by_type.items():
        if t in REQUIRED_TYPES and (counts["dev"] < 1 or counts["heldout"] < 1):
            problems.append(f"type {t!r} needs >=1 dev and >=1 heldout case, has {counts}")
    return problems


def freeze(allow_overwrite_ids: frozenset = frozenset(), frozen_path: Path | None = None,
          case_ids: set[str] | None = None) -> dict:
    """Record sha256 of each heldout case file, BEFORE the prompt/schema are touched again.
    Never silently overwrites an existing frozen hash for an id not explicitly listed in
    allow_overwrite_ids - that would defeat the point of freezing. `frozen_path`/`case_ids` let a later
    "round" (e.g. D-2026-10-04-02's 6 new heldout cases) freeze into its OWN file without touching or
    being limited by an earlier round's FROZEN.json. frozen_path defaults to the module's FROZEN_PATH,
    looked up at call time (not as a default-argument constant) so tests can monkeypatch it."""
    if frozen_path is None:
        frozen_path = FROZEN_PATH
    existing = json.loads(frozen_path.read_text(encoding="utf-8")) if frozen_path.exists() else {}
    for c in load_cases():
        if c["set"] != "heldout":
            continue
        if case_ids is not None and c["case_id"] not in case_ids:
            continue
        h = sha256_file(case_file(c["case_id"]))
        if c["case_id"] in existing and existing[c["case_id"]] != h and c["case_id"] not in allow_overwrite_ids:
            raise RuntimeError(f"{c['case_id']} changed after being frozen ({existing[c['case_id']]} -> {h}); "
                              f"not overwriting without explicit allow_overwrite_ids")
        existing[c["case_id"]] = h
    frozen_path.write_text(json.dumps(existing, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                           encoding="utf-8")
    return existing


def verify_frozen(frozen_path: Path | None = None, case_ids: set[str] | None = None) -> list[str]:
    """[] means every heldout case (or, with case_ids, every listed one) matches its frozen hash in
    frozen_path. Otherwise a list of problems: a heldout case missing from it, or one whose file changed.
    frozen_path defaults to the module's FROZEN_PATH, looked up at call time (see freeze())."""
    if frozen_path is None:
        frozen_path = FROZEN_PATH
    heldout = [c for c in load_cases() if c["set"] == "heldout"
              and (case_ids is None or c["case_id"] in case_ids)]
    if not frozen_path.exists():
        return [f"{frozen_path.name} does not exist but {len(heldout)} heldout case(s) are present"] if heldout else []
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    problems = []
    for c in heldout:
        if c["case_id"] not in frozen:
            problems.append(f"{c['case_id']}: heldout but not in {frozen_path.name}")
            continue
        h = sha256_file(case_file(c["case_id"]))
        if h != frozen[c["case_id"]]:
            problems.append(f"{c['case_id']}: file changed since freeze ({frozen[c['case_id']]} -> {h})")
    return problems


def run_case(c: dict, seed: int = 7, temperature: float = 0.4, out_path: str | None = None) -> dict:
    """One model run for one case. Does not interpret `expected` - that is a separate, later scoring step
    once real runs exist (there are none in this commit: GPU is off-limits until 2026-10-06 00:59 CDMX)."""
    return ai.run_with_docs(c["dispute"], c["documents"], c["document_names"], seed, temperature,
                            c["case_id"], c["type"], out_path)


def main(argv: list[str]) -> int:
    def opt(name: str, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    cmd = argv[0] if argv else ""
    if cmd == "list":
        for c in load_cases():
            print(f"{c['case_id']:<28} type={c['type']:<20} set={c['set']}")
        return 0
    frozen_path = Path(opt("--frozen-file")) if opt("--frozen-file") else FROZEN_PATH
    ids_opt = opt("--ids")
    case_ids = set(ids_opt.split(",")) if ids_opt else None
    if cmd == "verify-frozen":
        problems = verify_frozen(frozen_path, case_ids)
        for p in problems:
            print(p)
        print("OK" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    if cmd == "freeze":
        frozen = freeze(frozen_path=frozen_path, case_ids=case_ids)
        print(f"froze {len(frozen)} heldout case(s) into {frozen_path}")
        return 0
    if cmd == "run":
        case_id, set_ = opt("--case"), opt("--set")
        allow_heldout = "--allow-heldout" in argv
        if case_id:
            c = load_case(case_id)
            if c["set"] == "heldout" and not allow_heldout:
                print(f"REFUSED: {case_id} is a heldout case; pass --allow-heldout to run it deliberately")
                return 2
            cases = [c]
        elif set_ == "dev":
            cases = [c for c in load_cases() if c["set"] == "dev"]
        else:
            print(__doc__)
            return 2
        seed = int(opt("--seed", 7))
        for c in cases:
            rec = run_case(c, seed=seed)
            print(f"{c['case_id']:<28} validation={'PASS' if rec['validation_ok'] else 'FAIL'} "
                 f"review_flags={rec['review_flags']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
