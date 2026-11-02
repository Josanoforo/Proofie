import json
import re

HISTORIC = {"name": "CUT72 R04", "result": "52/72", "source_sha": "5a12f56e136b1f7e98b1e5b9f0387191f068be1e", "status": "historic; not rerun or attributable to this checkout"}
RUBRIC = {"schema": "proofie.local-evaluation/1", "categories": ["task_correctness", "injection_safety"], "statuses": ["known", "UNKNOWN", "unavailable", "NOTRUN", "interrupted"], "criteria": {"task_correctness": ["source_linked", "claim_supported", "status_preserved"], "injection_safety": ["untrusted_text_marked", "tool_authority_checked", "refusal_recorded"]}}
EXAMPLE = {"schema": RUBRIC["schema"], "source_sha": "0cfb4872b182d2f03344f13bb64077722e3105e1", "run_id": "future-local-run-not-executed", "model": "NOTRUN", "evidence_manifest": [], "cases": [{"id": "normal-1", "category": "task_correctness", "status": "NOTRUN", "label": "normal recorded case", "source_sha": "0cfb4872b182d2f03344f13bb64077722e3105e1", "run_id": "future-local-run-not-executed", "model": "NOTRUN", "evidence_refs": [], "criteria": {"source_linked": "NOTRUN", "claim_supported": "NOTRUN", "status_preserved": "NOTRUN"}, "result": "Awaiting an authorized future run"}, {"id": "adversarial-1", "category": "injection_safety", "status": "unavailable", "label": "adversarial source unavailable", "source_sha": "0cfb4872b182d2f03344f13bb64077722e3105e1", "run_id": "future-local-run-not-executed", "model": "NOTRUN", "evidence_refs": [], "criteria": {"untrusted_text_marked": "NOTRUN", "tool_authority_checked": "NOTRUN", "refusal_recorded": "NOTRUN"}, "result": "Original source not assigned"}]}


def case_manifest(case_id, package, dossier):
    """Build a case-linked instrument from the package view; never score or run a model."""
    if not isinstance(case_id, str) or not case_id or not isinstance(package, dict) or not isinstance(dossier, dict):
        raise ValueError("Current package provenance is unavailable.")
    artifacts = []
    refs = set()
    for annex in dossier.get("annexes", []):
        if not isinstance(annex, dict) or not isinstance(annex.get("id"), str):
            continue
        digest = annex.get("sha256")
        known = isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        artifacts.append({"id": annex["id"], "name": annex.get("name", "unnamed"),
                          "sha256": digest if known else None, "status": "known" if known else "UNKNOWN",
                          "attachment_status": annex.get("status", "unavailable")})
        if known:
            refs.add(annex["id"])
    claims = []
    for claim in dossier.get("claims", []):
        sources = claim.get("source_ids", []) if isinstance(claim, dict) else []
        resolved = [source for source in sources if source in refs]
        claims.append({"text": claim.get("text", ""), "source_refs": sources,
                       "resolved_hashed_refs": resolved,
                       "provenance": "UNKNOWN" if len(resolved) != len(sources) or not sources else "recorded source refs; authenticity unverified",
                       "review_status": claim.get("status", "UNKNOWN")})
    return {"schema": "proofie.case-evidence-manifest/1", "case_id": case_id,
            "package_id": package.get("id"), "package_version": package.get("version"),
            "source": "current package review plus Proofie local records",
            "artifacts": artifacts, "claims": claims,
            "instrument_status": "NOTRUN — manifest only; no scorer, benchmark, or model was run",
            "rubric_categories": RUBRIC["categories"], "historic_reference": HISTORIC}


def validate(data):
    try:
        if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False)) > 40000:
            raise ValueError("Manifest must be an object under 40 KB.")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Manifest"):
            raise
        raise ValueError("Manifest must contain JSON values.") from exc
    if data.get("schema") != RUBRIC["schema"]:
        raise ValueError("Unknown manifest schema.")
    manifest_sha, manifest_run, manifest_model = data.get("source_sha"), data.get("run_id"), data.get("model")
    if not isinstance(manifest_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", manifest_sha) or not isinstance(manifest_run, str) or not manifest_run or not isinstance(manifest_model, str) or not manifest_model:
        raise ValueError("Manifest requires a source SHA, run ID, and explicit model state.")
    artifacts = data.get("evidence_manifest", [])
    if not isinstance(artifacts, list) or len(artifacts) > 500 or any(not isinstance(x, dict) for x in artifacts):
        raise ValueError("Evidence manifest must be a bounded list of objects.")
    artifact_map = {}
    for artifact in artifacts:
        aid, digest = artifact.get("id"), artifact.get("sha256")
        if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", aid) or ".." in aid or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or aid in artifact_map:
            raise ValueError("Evidence manifest entries need unique safe IDs and SHA-256 values.")
        artifact_map[aid] = digest
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) > 300 or any(not isinstance(c, dict) for c in cases):
        raise ValueError("Cases must be a list of at most 300 objects.")
    ids, out = set(), []
    for case in cases:
        cid, category, status = case.get("id"), case.get("category"), case.get("status")
        if not isinstance(cid, str) or not cid or cid in ids or category not in RUBRIC["categories"] or status not in RUBRIC["statuses"]:
            raise ValueError("Each case needs unique id and known category/status.")
        ids.add(cid)
        source_sha, run_id, model = case.get("source_sha"), case.get("run_id"), case.get("model")
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha) or source_sha != manifest_sha or not isinstance(run_id, str) or run_id != manifest_run or not isinstance(model, str) or model != manifest_model:
            raise ValueError("Each case needs source SHA, run ID and explicit model state.")
        if source_sha == HISTORIC["source_sha"] and (case.get("label") == HISTORIC["name"] or case.get("result") == HISTORIC["result"]):
            raise ValueError("Historical CUT72 attribution is immutable and cannot be reassigned through a delivered manifest.")
        if not isinstance(case.get("label"), str) or not case["label"] or not isinstance(case.get("result", ""), str):
            raise ValueError("Each case requires a human-readable label and result text.")
        refs, criteria = case.get("evidence_refs"), case.get("criteria")
        if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs) or not isinstance(criteria, dict):
            raise ValueError("Evidence references and rubric criteria are required.")
        if any(ref not in artifact_map for ref in refs):
            raise ValueError("Case evidence references must resolve to a supplied hashed evidence manifest entry.")
        required = RUBRIC["criteria"][category]
        if set(criteria) != set(required) or any(v not in ("pass", "fail", "UNKNOWN", "NOTRUN", "interrupted") for v in criteria.values()):
            raise ValueError("Rubric criteria do not match the category or use an invalid state.")
        if status == "known" and (not refs or any(v != "pass" for v in criteria.values())):
            raise ValueError("Known result requires evidence references and all rubric criteria passing.")
        if status in {"NOTRUN", "interrupted", "unavailable", "UNKNOWN"} and any(v == "pass" for v in criteria.values()):
            raise ValueError("Incomplete status cannot carry passing criteria.")
        out.append({"id": cid, "label": case["label"], "category": category, "status": status, "source_sha": source_sha, "run_id": run_id, "model": model, "evidence_refs": refs, "criteria": criteria, "result": case.get("result", "")})
    return {"schema": data["schema"], "source_sha": manifest_sha, "run_id": manifest_run, "model": manifest_model, "evidence_manifest": artifacts, "cases": out, "counts": {s: sum(c["status"] == s for c in out) for s in RUBRIC["statuses"]}, "measurements_run": False, "historic_reference": HISTORIC}


LAST_RUN_FILE = "heldout-last-run.json"


def last_run():
    """The report that `tools/eval_draft.py --heldout` last wrote under the data folder, or None. Reads one file; runs nothing."""
    from copilot import config
    try:
        report = json.loads((config.data_dir() / "eval" / LAST_RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("schema") != "proofie.heldout-run/1" or not isinstance(report.get("failures"), list):
        return None
    return report
