"""Offline, structural tests for the case set and freeze mechanism. No model, no network:
these only check the case files and the freeze/verify bookkeeping, never run a case through Ollama.
"""
import json

import pytest

import eval_cases as ec

# D-2026-10-04-02: 6 additional heldout cases for the close-out evaluation, frozen separately
# (FROZEN_ROUND2.json) so they don't interact with the original 16-case round-1 invariants below.
# Not run until then - see pilot/eval/EXPOSURE.md and docs/DECISIONS.md.
ROUND2_IDS = {"sufficient_coherent_heldout2", "incomplete_heldout2", "no_tracking_heldout2",
             "contradictory_heldout2", "injection_heldout2", "unsupported_claim_heldout2"}
FROZEN_ROUND2 = ec.CASES_DIR / "FROZEN_ROUND2.json"


def _round1(cases):
    return [c for c in cases if c["case_id"] not in ROUND2_IDS]


def test_case_set_has_no_structural_problems():
    assert ec.check_case_set_shape() == []


def test_sixteen_plus_six_cases_covering_eight_types():
    cases = ec.load_cases()
    assert len(cases) == 22, "16 round-1 cases + 6 round-2 heldout (D-2026-10-04-02)"
    round1 = _round1(cases)
    assert len(round1) == 16
    by_type = {}
    for c in round1:
        by_type.setdefault(c["type"], []).append(c["set"])
    assert set(by_type) == ec.REQUIRED_TYPES
    for t, sets in by_type.items():
        assert sorted(sets) == ["dev", "heldout"], (t, sets)
    assert {c["case_id"] for c in cases if c["case_id"] in ROUND2_IDS} == ROUND2_IDS
    assert all(c["set"] == "heldout" for c in cases if c["case_id"] in ROUND2_IDS)


def test_case_ids_are_unique_and_match_their_filename():
    cases = ec.load_cases()
    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert ec.case_file(c["case_id"]).exists()


def test_frozen_json_exists_and_matches_every_round1_heldout_case():
    assert ec.FROZEN_PATH.exists(), "run `python eval_cases.py freeze` before closing this gate"
    round1_heldout_ids = {c["case_id"] for c in _round1(ec.load_cases()) if c["set"] == "heldout"}
    assert ec.verify_frozen(case_ids=round1_heldout_ids) == []


def test_frozen_round2_exists_and_matches_the_six_new_heldout_cases():
    assert FROZEN_ROUND2.exists(), "run `python eval_cases.py freeze --frozen-file ... --ids ...` for round 2"
    assert ec.verify_frozen(frozen_path=FROZEN_ROUND2, case_ids=ROUND2_IDS) == []
    frozen = json.loads(FROZEN_ROUND2.read_text(encoding="utf-8"))
    assert set(frozen) == ROUND2_IDS, "round-2 freeze file must contain exactly the 6 new cases, nothing else"


def test_freeze_refuses_to_silently_overwrite_a_changed_heldout_case(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "CASES_DIR", tmp_path)
    monkeypatch.setattr(ec, "FROZEN_PATH", tmp_path / "FROZEN.json")
    case = {"case_id": "x", "type": "sufficient_coherent", "set": "heldout", "dispute": {}, "documents": {},
            "document_names": {}, "expected": {}}
    (tmp_path / "x.json").write_text(json.dumps(case), encoding="utf-8")
    ec.freeze()
    assert ec.verify_frozen() == []
    (tmp_path / "x.json").write_text(json.dumps({**case, "documents": {"doc_a": "tampered"}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed after being frozen"):
        ec.freeze()
    assert ec.verify_frozen() != []          # the tamper is now detectable, not silently accepted
    ec.freeze(allow_overwrite_ids=frozenset({"x"}))   # explicit allow re-freezes it
    assert ec.verify_frozen() == []


def test_verify_frozen_flags_a_heldout_case_missing_from_frozen_json(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "CASES_DIR", tmp_path)
    monkeypatch.setattr(ec, "FROZEN_PATH", tmp_path / "FROZEN.json")
    case = {"case_id": "y", "type": "incomplete", "set": "heldout", "dispute": {}, "documents": {},
            "document_names": {}, "expected": {}}
    (tmp_path / "y.json").write_text(json.dumps(case), encoding="utf-8")
    (tmp_path / "FROZEN.json").write_text("{}", encoding="utf-8")
    problems = ec.verify_frozen()
    assert problems and "not in FROZEN.json" in problems[0]


def test_run_case_cli_refuses_heldout_without_explicit_flag(capsys):
    rc = ec.main(["run", "--case", "sufficient_coherent_heldout"])
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().out


def test_dev_and_heldout_twins_use_different_document_content():
    """Sanity: a heldout case is not just a byte-identical copy of its dev twin (that would defeat the
    point of a held-out check)."""
    for base in sorted({c["type"] for c in ec.load_cases()}):
        dev = ec.load_case(f"{base}_dev")
        heldout = ec.load_case(f"{base}_heldout")
        assert dev["documents"] != heldout["documents"], base


# ---- the frozen hash does not depend on the checkout's line endings --------------------------------------
def test_frozen_hash_is_the_same_for_an_lf_copy_and_a_crlf_copy(tmp_path):
    text = b'{\n  "case_id": "x",\n  "doc": "line one\nline two"\n}\n'      # the \n inside the string is not a line end
    lf, crlf = tmp_path / "lf.json", tmp_path / "crlf.json"
    lf.write_bytes(text)
    crlf.write_bytes(text.replace(b"\n", b"\r\n"))
    assert ec.sha256_file(lf) == ec.sha256_file(crlf)


def test_frozen_hash_still_changes_with_a_one_character_change(tmp_path):
    base, changed = tmp_path / "a.json", tmp_path / "b.json"
    base.write_bytes(b'{"doc": "tracking 123"}\n')
    changed.write_bytes(b'{"doc": "tracking 124"}\n')
    assert ec.sha256_file(base) != ec.sha256_file(changed)


def test_a_lone_carriage_return_is_content_not_a_line_ending(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"one\rtwo\n")
    b.write_bytes(b"one\ntwo\n")
    assert ec.sha256_file(a) != ec.sha256_file(b)
