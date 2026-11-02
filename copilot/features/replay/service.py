"""The hosted demo's replay: a recorded run of the local model, read from a committed synthetic fixture.

Read only and static: this module opens one JSON file under copilot/fixtures/replay/ and shapes it for the page. It never
calls a model, PayPal or the database, and nothing it returns can enter a package or a send. The case id is checked against
PayPal's id shape before any path is built.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from copilot.web.security import DISPUTE_ID_RE, MAX_DISPUTE_ID_LEN

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "replay"
SOURCE_DISPUTE = "paypal_dispute"
DISPUTE_NAME = "PayPal's dispute record (synthetic)"

DATA_ONLY_CASE = "dr4-dataonly"
DATA_ONLY_SCHEMA = "proofie-dr4-data-only/1"
DATA_ONLY_SHA256 = "b491a593993d779cc7c291f8aadabe3e3bb67b84d34c8fdb4ab479f83be2787d"
DATA_ONLY_FILENAME = "dr4-dataonly-b491a593993d779cc7c291f8aadabe3e3bb67b84d34c8fdb4ab479f83be2787d.json"
DATA_ONLY_SOURCE_HASHES = {
    "RC44-B-DR4-LOCAL-RECEIPT-20261021.md": "c8c17c28728d0b637e475d2b4c0c93d7928cd20c10f8d26f2159854e35d510e6",
    "approval-receipt.json": "d1cfc3ada3e9fb4647039c5a94585ea32980847308c06d6db7677e66b579f472",
    "case-file.zip": "16071bede6c09dbc47e9b2fb4824ae5a8a9b51e12f95fcea8a513acca88f64d1",
    "case-file-verification.txt": "9ec0d649f9b2afbc7be72e4f0f225ec859d5200e66f773f4c36732c9c27191c4",
}

BASIS_WORDS = {"verified": "backed by a source", "seller_statement": "the seller's own statement",
               "buyer_statement": "the buyer's statement", "model_interpretation": "the model's own reading"}
NEXT_STEP_WORDS = {"submit_evidence_for_human_approval": "Submit the evidence, after a person approves it",
                   "need_more_information": "More information is needed"}
CHECK_WORDS = {
    "json_parse": "The answer is valid JSON",
    "json_schema": "The answer has the required shape",
    "source_ids_exist": "Every cited source exists",
    "claims_grounded_in_cited_sources": "Every statement is supported by the source it cites",
    "evidence_item_has_non_interpretation_support": "Each evidence item rests on more than the model's own reading",
    "text_facts_literal_in_sources": "Every date, number and identifier in the text appears in a source",
    "tracking_grounded": "The tracking number and carrier appear in a source",
    "evidence_type_valid_for_reason": "The evidence type fits the reason for the dispute",
    "injection_not_followed": "No instruction hidden in a document was followed",
}


def _load_data_only() -> dict | None:
    """Load only the pinned, allowlisted local DR4 recut; never read external data."""
    path = FIXTURE_DIR / DATA_ONLY_FILENAME
    try:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != DATA_ONLY_SHA256 or path.name != f"dr4-dataonly-{digest}.json":
            return None
        data = json.loads(payload.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {
        "schema", "scope", "observed_date", "source_commit", "coverage_status", "verification_result", "privacy", "run_observations",
        "claim_scope", "sources", "stages", "supporting_artifacts", "dispute_status", "admission", "not_running_now",
    }:
        return None
    if data.get("schema") != DATA_ONLY_SCHEMA or data.get("scope") != "synthetic_local_data_only":
        return None
    if data.get("observed_date") != "2026-10-21" or data.get("source_commit") != "672f12356ce286c7d9197e58b475f24d4efbd161":
        return None
    if data.get("coverage_status") != "c11_dr4_local_partial" or data.get("verification_result") != {
        "checked": 4, "ok": 4, "fail": 0, "native_exit": 0,
    }:
        return None
    if data.get("privacy") != {
        "allowlist_only": True, "identifiers_included": False, "bodies_included": False,
        "source_payload_redaction_verified": False,
        "notice": "Identifiers and document bodies are omitted from this recut; source ZIP redaction is not verified.",
    }:
        return None
    if data.get("run_observations") != {
        "synthetic_fixture_gets": 15, "fake_writes": 0, "actual_external_hosts": 0, "active_feature_flags": 2,
    } or data.get("claim_scope") != {"observed": "title_only", "delivery_status": "missing"}:
        return None
    sources = data.get("sources")
    if not isinstance(sources, list) or any(not isinstance(s, dict) or set(s) != {"basename", "sha256"} for s in sources):
        return None
    source_map = {s["basename"]: s["sha256"] for s in sources}
    if source_map != DATA_ONLY_SOURCE_HASHES or len(sources) != len(DATA_ONLY_SOURCE_HASHES):
        return None
    expected_stages = [
        ("Stage1", "document_attachment", "observed", "A document was ingested locally and attached through the local APIs; no upload route or browser upload is evidenced.", ["RC44-B-DR4-LOCAL-RECEIPT-20261021.md"]),
        ("Stage2", "review", "observed", "Synthetic local review stage observed in DR4 evidence.", ["RC44-B-DR4-LOCAL-RECEIPT-20261021.md"]),
        ("Stage3", "local_approval", "observed", "Local approval observed; no PayPal send is asserted.", ["RC44-B-DR4-LOCAL-RECEIPT-20261021.md", "approval-receipt.json"]),
        ("Stage4", "send", "not_run", "Case-file manifest records send=null.", ["case-file.zip"]),
        ("Stage5", "read_back", "not_run", "No PayPal call occurred; actual external hosts were 0.", ["RC44-B-DR4-LOCAL-RECEIPT-20261021.md"]),
    ]
    stages = data.get("stages")
    if not isinstance(stages, list) or len(stages) != len(expected_stages):
        return None
    for stage, expected in zip(stages, expected_stages):
        if not isinstance(stage, dict) or set(stage) != {
            "stage_id", "activity", "status", "candidate_code_commit", "reason", "receipt_refs",
        }:
            return None
        if (stage["stage_id"], stage["activity"], stage["status"], stage["reason"], stage["receipt_refs"]) != expected:
            return None
        if stage["candidate_code_commit"] != data["source_commit"] or any(ref not in source_map for ref in stage["receipt_refs"]):
            return None
    expected_artifacts = [
        {"kind": "exported_case", "basename": "case-file.zip", "sha256": DATA_ONLY_SOURCE_HASHES["case-file.zip"]},
        {"kind": "offline_verification", "basename": "case-file-verification.txt", "sha256": DATA_ONLY_SOURCE_HASHES["case-file-verification.txt"]},
        {"kind": "approval_receipt", "basename": "approval-receipt.json", "sha256": DATA_ONLY_SOURCE_HASHES["approval-receipt.json"]},
    ]
    if data.get("supporting_artifacts") != expected_artifacts:
        return None
    if data.get("dispute_status") != {
        "eligible_sandbox_dispute": "unobserved", "no_click_preparation": "not_evidenced",
        "human_step": "From the buyer account, open the agreed synthetic-sale dispute in PayPal Sandbox Resolution Center, choose Item not received, complete the final confirmation, and share only the dispute ID privately with EV. Do not share credentials or PayPal bodies.",
    }:
        return None
    if data.get("admission") != {
        "status": "off_no_eligible_dispute_evidenced",
        "reason": "No eligible sandbox dispute is evidenced in this recut; this is an evidence-scope status, not a claim of global ineligibility. Final W3-4 admission remains OFF; the full five-stage recording is not observed and is inactive.",
    } or data.get("not_running_now") is not True:
        return None
    return {**data, "_loaded_candidate_sha256": digest}


def _view_data_only(data: dict) -> dict:
    return {
        "kind": "data_only", "candidate_sha256": data["_loaded_candidate_sha256"],
        "evidence_date": data["observed_date"], "source_commit": data["source_commit"],
        "privacy": data["privacy"], "run_observations": data["run_observations"],
        "claim_scope": data["claim_scope"], "coverage_status": data["coverage_status"],
        "verification_result": data["verification_result"], "stages": data["stages"],
        "sources": data["sources"], "supporting_artifacts": data["supporting_artifacts"],
        "dispute_status": data["dispute_status"], "admission": data["admission"], "not_running_now": data["not_running_now"],
    }

def load(case: str) -> dict | None:
    """The fixture for `case`, or None for a malformed id or a case with no recording."""
    if case == DATA_ONLY_CASE:
        return _load_data_only()
    if not case or len(case) > MAX_DISPUTE_ID_LEN or not DISPUTE_ID_RE.fullmatch(case):
        return None
    path = FIXTURE_DIR / f"{case}.json"
    try:
        payload = path.read_bytes()
        data = json.loads(payload.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != "proofie-replay/1" or data.get("case") != case:
        return None
    # W3-4 preparation: identify the fixture loaded without claiming provenance for a future recording.
    return {**data, "fixture_sha256": hashlib.sha256(payload).hexdigest()}


def view(data: dict) -> dict:
    """What the page shows, from the fixture only."""
    if data.get("schema") == DATA_ONLY_SCHEMA:
        return _view_data_only(data)
    names = {SOURCE_DISPUTE: DISPUTE_NAME, **(data.get("documents") or {})}
    try:
        out = json.loads(data["raw_output"])
    except (KeyError, ValueError, TypeError):
        out = {}
    claims = [{"text": c.get("text", ""), "basis": c.get("basis", ""), "basis_words": BASIS_WORDS.get(c.get("basis"), "unclassified"),
               "sources": [names.get(s, s) for s in c.get("source_ids", [])]}
              for evidence in out.get("proposed_evidence", []) for c in evidence.get("claims", [])]
    checks = [{"words": CHECK_WORDS.get(c.get("check"), str(c.get("check", "")).replace("_", " ")), "ok": bool(c.get("ok")),
               "detail": [str(d) for d in c.get("detail", [])]} for c in data.get("checks", [])]
    recommended = out.get("recommended_next_step")
    recorded_at = data.get("recorded_at")
    recording_date = recorded_at.strip() if isinstance(recorded_at, str) and recorded_at.strip() else None
    return {"case": data["case"], "model": data.get("model", ""), "recording_date": recording_date,
            "candidate_sha256": str(data.get("fixture_sha256", "")),
            "provenance_readiness": "unavailable", "privacy_readiness": "unavailable",
            "documents": [names.get(k, k) for k in sorted(data.get("documents") or {})], "claims": claims,
            "missing": [str(m) for m in out.get("missing_evidence", [])],
            "conflicts": [str(c.get("description", "")) for c in out.get("conflicts", []) if isinstance(c, dict)],
            "recommended": NEXT_STEP_WORDS.get(recommended, str(recommended or "")),
            "note": str(out.get("reviewer_note_draft", "")),
            "checks": checks, "validation_ok": bool(data.get("validation_ok")),
            "failed": [c for c in checks if not c["ok"]], "raw": str(data.get("raw_output", ""))}
