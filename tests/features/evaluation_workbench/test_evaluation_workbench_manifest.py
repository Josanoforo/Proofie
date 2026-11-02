import pytest

from copilot.features.evaluation_workbench import service


def test_status_provenance_criteria_and_historic_sha_fixed():
    out = service.validate(service.EXAMPLE)
    assert out["counts"]["NOTRUN"] == 1 and out["counts"]["unavailable"] == 1 and not out["measurements_run"]
    assert out["historic_reference"]["source_sha"] == "5a12f56e136b1f7e98b1e5b9f0387191f068be1e"
    assert out["cases"][0]["source_sha"] != out["historic_reference"]["source_sha"]


def test_known_requires_provenance_evidence_and_all_rubric_criteria():
    case = {"id": "x", "category": "task_correctness", "status": "known", "label": "known task", "source_sha": "0" * 40, "run_id": "run-1", "model": "human supplied", "evidence_refs": ["case.json"], "criteria": {k: "pass" for k in service.RUBRIC["criteria"]["task_correctness"]}, "result": "observed"}
    base={"schema": service.RUBRIC["schema"],"source_sha":"0"*40,"run_id":"run-1","model":"human supplied","evidence_manifest":[{"id":"case.json","sha256":"a"*64}]}
    assert service.validate({**base,"cases": [case]})["cases"][0]["status"] == "known"
    with pytest.raises(ValueError): service.validate({**base,"cases": [{**case, "evidence_refs": ["missing.json"]}]})
    with pytest.raises(ValueError): service.validate({**base,"cases": [{**case, "criteria": {"source_linked": "pass"}}]})


def test_historic_result_cannot_be_reassigned_and_incomplete_not_pass():
    case = service.EXAMPLE["cases"][0]
    with pytest.raises(ValueError, match="cannot be reassigned"):
        service.validate({"schema": service.RUBRIC["schema"], "source_sha": service.HISTORIC["source_sha"], "run_id": case["run_id"], "model": case["model"], "evidence_manifest": [], "cases": [{**case, "source_sha": service.HISTORIC["source_sha"], "label": service.HISTORIC["name"], "result": service.HISTORIC["result"]}]})
    with pytest.raises(ValueError):
        service.validate({"schema": service.RUBRIC["schema"], "cases": [{**case, "criteria": {k: "pass" for k in case["criteria"]}}]})
