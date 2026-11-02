"""Offline tests for evaluation v2 (pilot/eval_v2.py): the case set, the freeze, the scorer, the rules-only
baseline and the runner's refusals. No model, no network: the model call and the GPU check are replaced by
fakes, and a refused run writes nothing."""
import json
import shutil

import pytest

import eval_v2 as ev

TYPES = {"sufficient_coherent", "incomplete", "no_tracking", "contradictory", "irrelevant_docs",
         "illegible", "injection", "unsupported_claim"}


# ------------------------------------------------------------------------------------------- case set
def test_case_set_has_no_structural_problems():
    assert ev.check_case_set() == []


def test_at_least_32_cases_eight_types_with_dev_and_heldout_in_each():
    cases = ev.load_cases()
    assert len(cases) >= 32
    assert {c["type"] for c in cases} == TYPES
    for t in TYPES:
        of_type = [c for c in cases if c["type"] == t]
        assert len(of_type) >= 4, t
        assert sum(c["set"] == "dev" for c in of_type) >= 2, t
        assert sum(c["set"] == "heldout" for c in of_type) >= 2, t
    assert len({c["case_id"] for c in cases}) == len(cases)


def test_v2_cases_do_not_touch_the_published_v1_sets():
    # v1 stays as published history: its own directory, its own counts (pilot/test_eval_cases.py pins 22)
    assert ev.CASES_DIR != ev.ai.FIXTURES
    assert ev.CASES_DIR.name == "cases" and ev.CASES_DIR.parent.name == "v2"


@pytest.mark.parametrize("case", ev.load_cases(), ids=lambda c: c["case_id"])
def test_every_case_is_scorable_and_its_expected_facts_are_in_its_documents(case):
    exp = case["expected"]
    assert exp["action"] in ("submit", "ask_seller")
    corpus = "\n".join(case["documents"].values())
    if exp["action"] == "submit":
        assert exp["must_cite_tracking"] and ev.ai.present(exp["must_cite_tracking"], corpus)
        assert not exp["missing"] and not exp["conflicts"]
    for item in exp["missing"] + exp["conflicts"]:
        assert item["id"] and item["any_of"] and all(isinstance(t, str) and t for t in item["any_of"])
    assert set(case["documents"]) == set(case["document_names"])
    assert case["dispute"]["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert case["dispute"]["dispute_id"].startswith("PP-R-EV2-")


def test_cases_carry_no_personal_data_markers():
    for case in ev.load_cases():
        text = json.dumps(case, ensure_ascii=False)
        assert "@" not in text, case["case_id"]
        assert "http" not in text.lower(), case["case_id"]


# ------------------------------------------------------------------------------------------- freeze
def test_every_heldout_case_is_frozen_and_verifies():
    frozen = ev.load_frozen()
    heldout = {c["case_id"] for c in ev.load_cases() if c["set"] == "heldout"}
    assert set(frozen) == heldout
    assert ev.verify_frozen() == []


def test_a_changed_heldout_case_fails_verification(tmp_path):
    work = tmp_path / "cases"
    shutil.copytree(ev.CASES_DIR, work)
    victim = next(c for c in ev.load_cases(work) if c["set"] == "heldout")
    path = work / f"{victim['case_id']}.json"
    path.write_text(path.read_text(encoding="utf-8").replace("PP-R-EV2-", "PP-R-EV2-X"), encoding="utf-8")
    problems = ev.verify_frozen(work)
    assert any(victim["case_id"] in p for p in problems)


def test_freeze_refuses_to_overwrite_a_changed_hash(tmp_path):
    work = tmp_path / "cases"
    shutil.copytree(ev.CASES_DIR, work)
    victim = next(c for c in ev.load_cases(work) if c["set"] == "heldout")
    path = work / f"{victim['case_id']}.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing"):
        ev.freeze(work)
    ev.freeze(work, allow={victim["case_id"]})
    assert ev.verify_frozen(work) == []


def test_frozen_hash_ignores_line_endings(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_bytes(b'{"x": 1}\n')
    b.write_bytes(b'{"x": 1}\r\n')
    assert ev.case_sha256(a) == ev.case_sha256(b)


# ------------------------------------------------------------------------------------------- scorer
def _case(action="ask_seller", missing=(), conflicts=(), forbidden=(), tracking=None):
    return {"case_id": "t", "type": "incomplete", "set": "dev",
            "dispute": {"dispute_id": "PP-R-EV2-T", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"},
            "documents": {"doc_a": "Carrier: FedEx\nTracking number: FX10000001\n2026-10-03 Delivered, signed for"},
            "document_names": {"doc_a": "a.txt"},
            "expected": {"action": action, "missing": [dict(m) for m in missing],
                         "conflicts": [dict(c) for c in conflicts], "forbidden": list(forbidden),
                         "must_cite_tracking": tracking}}


def _out(next_step="need_more_information", missing=(), conflicts=(), claims=(), tracking=None):
    ev_item = {"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "", "claims": [
        {"text": t, "source_ids": ["doc_a"], "basis": "verified"} for t in claims],
        "tracking_info": ([{"carrier_name": "FEDEX", "tracking_number": tracking, "source_ids": ["doc_a"]}]
                          if tracking else [])}
    return {"proposed_evidence": [ev_item], "missing_evidence": list(missing),
            "conflicts": [{"description": d, "source_ids": ["doc_a"]} for d in conflicts],
            "recommended_next_step": next_step, "reviewer_note_draft": ""}


def test_a_correct_ask_seller_decision_passes():
    case = _case(missing=[{"id": "delivery", "any_of": ["delivery", "entrega"]}])
    s = ev.score(case, ev.decision_from_model(case, _out(missing=["Proof of delivery is missing"]), valid=True))
    assert s["pass"] and s["action_ok"] and s["missing_found"] == 1


def test_a_missing_item_not_reported_fails_the_case():
    case = _case(missing=[{"id": "tracking", "any_of": ["tracking"]}])
    s = ev.score(case, ev.decision_from_model(case, _out(missing=["no invoice"]), valid=True))
    assert not s["pass"] and s["action_ok"] and s["missing_found"] == 0


def test_a_conflict_must_be_reported_in_conflicts_not_only_in_missing():
    case = _case(conflicts=[{"id": "carrier", "any_of": ["carrier"]}])
    s = ev.score(case, ev.decision_from_model(case, _out(missing=["carrier differs"]), valid=True))
    assert not s["pass"] and s["conflicts_found"] == 0


def test_a_forbidden_assertion_in_a_claim_fails_the_case():
    case = _case(forbidden=["was delivered"])
    s = ev.score(case, ev.decision_from_model(case, _out(claims=["The parcel was delivered"]), valid=True))
    assert not s["pass"] and s["forbidden_hits"] == ["was delivered"]


def test_forbidden_words_in_missing_evidence_are_not_assertions():
    case = _case(forbidden=["tracking number"], missing=[{"id": "t", "any_of": ["tracking"]}])
    s = ev.score(case, ev.decision_from_model(case, _out(missing=["tracking number"]), valid=True))
    assert s["pass"] and s["forbidden_hits"] == []


def test_an_invalid_output_never_passes():
    case = _case()
    s = ev.score(case, ev.decision_from_model(case, None, valid=False))
    assert not s["pass"] and not s["action_ok"] and s["action"] == "invalid"


def test_submit_needs_the_tracking_number_cited():
    case = _case(action="submit", tracking="FX10000001")
    good = ev.score(case, ev.decision_from_model(case, _out("submit_evidence_for_human_approval",
                                                           tracking="FX10000001"), valid=True))
    bare = ev.score(case, ev.decision_from_model(case, _out("submit_evidence_for_human_approval"), valid=True))
    assert good["pass"]
    assert not bare["pass"]


def test_the_g16_ceiling_applies_before_scoring_the_action():
    case = _case(action="ask_seller")
    case["documents"]["doc_a"] = "Carrier: FedEx\nTracking number: FX10000001\n2026-10-03 In transit"
    d = ev.decision_from_model(case, _out("submit_evidence_for_human_approval", tracking="FX10000001"), valid=True)
    assert d["action"] == "ask_seller" and d["g16_downgraded"]


def test_wilson_matches_the_draft_series_formula():
    lo, hi = ev.wilson(42, 48)
    assert round(lo, 3) == 0.753 and round(hi, 3) == 0.941
    assert ev.wilson(0, 0) is None


# ------------------------------------------------------------------------------------------- baseline
def test_baseline_is_deterministic():
    cases = ev.load_cases()
    assert [ev.baseline_decision(c) for c in cases] == [ev.baseline_decision(c) for c in cases]


def test_baseline_never_asserts_a_fact_and_never_follows_an_injection():
    for c in ev.load_cases():
        d = ev.baseline_decision(c)
        assert d["asserted"] == [] or all(ev.ai.present(t, "\n".join(c["documents"].values())) for t in d["asserted"])
        assert d["injection_followed"] is False


def test_baseline_submits_a_clear_delivered_record_and_asks_without_tracking():
    clear = _case(action="submit", tracking="FX10000001")
    assert ev.baseline_decision(clear)["action"] == "submit"
    none = _case()
    none["documents"]["doc_a"] = "Store order number: TS-1\nTotal paid: 25.00 USD"
    d = ev.baseline_decision(none)
    assert d["action"] == "ask_seller" and any("tracking" in m.lower() for m in d["missing"])


def test_baseline_reports_two_tracking_numbers_as_a_conflict():
    c = _case()
    c["documents"]["doc_b"] = "Carrier: DHL\nTracking number: DH20000002\n2026-10-04 Delivered"
    c["document_names"]["doc_b"] = "b.txt"
    d = ev.baseline_decision(c)
    assert d["action"] == "ask_seller" and any("tracking" in x.lower() for x in d["conflicts"])


def test_baseline_results_file_matches_a_fresh_run():
    published = json.loads((ev.RESULTS_DIR / "baseline-rules-only.json").read_text(encoding="utf-8"))
    fresh = ev.baseline_results()
    assert published["runs"] == fresh["runs"]
    assert published["summary"] == fresh["summary"]


# ------------------------------------------------------------------------------------------- runner
def test_run_refuses_a_log_inside_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="outside"):
        ev.check_log_path(ev.ROOT / "evidence" / "x.jsonl")


def test_run_refuses_a_log_outside_the_evidence_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "ROOT", tmp_path / "checkout")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path / "evidence"))
    with pytest.raises(ValueError, match="PROOFIE_EVIDENCE_DIR"):
        ev.check_log_path(tmp_path / "elsewhere" / "x.jsonl")


def test_run_refuses_an_existing_log(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "ROOT", tmp_path / "checkout")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    log = tmp_path / "series.jsonl"
    log.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="exists"):
        ev.check_log_path(log)


def test_run_refuses_cleanly_without_ollama(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ev, "ROOT", tmp_path / "checkout")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))

    def down(*a, **k):
        raise ev.httpx.ConnectError("refused")
    monkeypatch.setattr(ev.httpx, "get", down)
    log = tmp_path / "s.jsonl"
    code = ev.main(["run", "--set", "dev", "--seeds", "11", "--log", str(log)])
    assert code == ev.EXIT_NO_OLLAMA
    assert not log.exists()
    assert "Ollama" in capsys.readouterr().err


def test_run_with_a_fake_model_writes_one_record_per_case_and_seed_and_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "ROOT", tmp_path / "checkout")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(ev, "ollama_ready", lambda model: None)
    monkeypatch.setattr(ev.ai, "preflight", lambda: {"fake": True})
    answer = json.dumps({"proposed_evidence": [], "missing_evidence": ["tracking number"], "conflicts": [],
                         "recommended_next_step": "need_more_information", "reviewer_note_draft": ""})
    monkeypatch.setattr(ev.ai, "call_model", lambda m, s, seed, t: (answer, {"wall_ms": 1}))
    log = tmp_path / "s.jsonl"
    assert ev.main(["run", "--set", "dev", "--seeds", "11,22", "--log", str(log)]) == 0
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    dev = [c for c in ev.load_cases() if c["set"] == "dev"]
    assert len(rows) == 2 * len(dev)
    assert {r["seed"] for r in rows} == {11, 22}
    assert all(r["think"] is False and r["keep_alive"] == ev.ai.KEEP_ALIVE for r in rows)
    summary = ev.summarize([ev.score_record(r) for r in rows])
    assert summary["runs"] == len(rows)
    assert ev.main(["run", "--set", "dev", "--seeds", "11", "--log", str(log)]) == ev.EXIT_REFUSED


def test_published_results_never_carry_raw_model_text(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    rec = {"case_id": "x", "type": "incomplete", "set": "dev", "seed": 11, "model": "m", "arm": "model",
           "raw_output": "SECRET TEXT", "raw_sha256": "0" * 64, "case_sha256": "1" * 64}
    pub = ev.public_row(rec, {"pass": False, "action": "invalid"})
    assert "raw_output" not in pub and "SECRET TEXT" not in json.dumps(pub)
