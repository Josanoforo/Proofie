"""Single-case local preparation contract; no provider/model access."""
from __future__ import annotations

import sqlite3
from copilot.core import auto_prep
from copilot.db import repo
from copilot.features.local_autopilot.observations import human_guard, source_guard


def _human_package(db_path, case_id):
    packages = repo.list_packages(db_path, case_id)
    human = [p for p in packages if p.get("source") not in ("ai", "auto_ai", "auto_manual")
             or any(c.get("edited_by_human") for c in repo.list_claims(db_path, p["id"]))
             or repo.list_attachments(db_path, p["id"]) or repo.list_approvals(db_path, p["id"])]
    return max(human, key=lambda p: (p["version"], p["id"])) if human else None


def _after_work_changed(db_path, norm, observation, version, result):
    case_id = observation["case_id"]
    human_changed = human_guard(db_path, case_id) != observation.get("human_guard")
    sources_changed = source_guard(db_path, case_id) != (observation.get("source_guard"), True)
    if not human_changed and not sources_changed and auto_prep.compute_version_hash(db_path, norm) == version:
        return False
    package = _human_package(db_path, case_id) if human_changed else None
    if package:
        result.update(state="human_review", package_id=package["id"], package_version=package["version"],
                      human_href=f"/disputes/{case_id}/packages/{package['id']}",
                      reasons=["A human package changed during preparation; review it before using any new proposal."])
    else:
        result.update(state="stale", reasons=["Case data or package changed during preparation; reopen and review."])
    return True


def prepare_known_case(db_path, observation, *, expected_version=None, runner=None):
    """Use only server-held observations. runner is an explicit test/core seam, never a fake client."""
    if not isinstance(observation, dict) or not isinstance(observation.get("norm"), dict):
        return {"case_id": None, "state": "unavailable", "reasons": ["Observation unavailable; reopen the case."]}
    norm = observation["norm"]; cid = observation.get("case_id")
    result = {"case_id": cid, "state": "unavailable", "package_id": None, "package_version": None,
              "missing": [], "reasons": [], "observed_at": observation.get("observed_at"),
              "provenance": observation.get("provenance"), "human_href": None}
    if not isinstance(cid, str) or norm.get("dispute_id") != cid or observation.get("provenance") not in ("provider_read", "synthetic_fixture"):
        result["reasons"] = ["Case observation does not match; reopen the case."]; return result
    try:
        version = auto_prep.compute_version_hash(db_path, norm); result["version"] = version
        current_source_guard, originals_valid = source_guard(db_path, cid)
        if version != observation.get("version") or (expected_version is not None and expected_version != version) or human_guard(db_path, cid) != observation.get("human_guard") or current_source_guard != observation.get("source_guard"):
            result.update(state="stale", reasons=["Case data or package changed; reopen and review."]); return result
        if not originals_valid:
            result.update(state="needs_input", missing=["A case original is missing or changed; review or upload it again."], human_href=f"/disputes/{cid}/workspace/prepare#documents"); return result
        if not auto_prep.should_prepare(norm):
            result.update(state="not_eligible", reasons=["This case is not eligible for automatic preparation; continue manually."]); return result
        package = _human_package(db_path, cid)
        if package:
            result.update(state="human_review", package_id=package["id"], package_version=package["version"],
                          human_href=f"/disputes/{cid}/packages/{package['id']}",
                          reasons=["A human package is preserved; review it before preparing another proposal."]); return result
        saved = repo.get_auto_prep(db_path, cid)
        if saved and saved.get("version_hash") == version and saved.get("status") == "ready":
            package = repo.get_package(db_path, saved.get("package_id")) if saved.get("package_id") else None
            if package and package.get("dispute_id") == cid:
                result.update(state="ready", package_id=package["id"], package_version=package["version"],
                              human_href=f"/disputes/{cid}/packages/{package['id']}",
                              reasons=["Review the prepared package; a manual shell may have no statements."])
                return result
            result["reasons"] = ["Prepared package unavailable; continue manually."]; return result
        if not any(not d.get("excluded") for d in repo.list_documents(db_path, cid)) and not repo.get_manual_fields(db_path, cid):
            result.update(state="needs_input", missing=["Add a case document or enter a shipment detail."],
                          human_href=f"/disputes/{cid}/workspace/prepare#documents"); return result
        local_runner = runner if runner is not None else getattr(auto_prep, "prepare_dispute_local", None)
        if local_runner is None:
            result["reasons"] = ["Local preparation is unavailable; continue manually."]; return result
        token = repo.claim_auto_prep(db_path, cid, version)
        if not token:
            saved = repo.get_auto_prep(db_path, cid) or {}
            state = saved.get("status")
            if saved.get("version_hash") != version:
                result.update(state="stale", reasons=["Preparation changed; reopen the case."]); return result
            if state == "ready" and saved.get("package_id"):
                package = repo.get_package(db_path, saved["package_id"])
                if package and package.get("dispute_id") == cid:
                    result.update(state="ready", package_id=package["id"], package_version=package["version"], human_href=f"/disputes/{cid}/packages/{package['id']}", reasons=["Review the prepared package; a manual shell may have no statements."]); return result
            if state == "needs_documents":
                result.update(state="needs_input", missing=[saved.get("detail") or "Add a case document."]); return result
            result.update(state="in_progress" if state == "preparing" else "cooldown" if state == "failed" else "unavailable",
                          reasons=["Preparation already claimed or unavailable; review its recorded state."])
            return result
        if auto_prep.compute_version_hash(db_path, norm) != version or human_guard(db_path, cid) != observation.get("human_guard") or source_guard(db_path, cid) != (observation.get("source_guard"), True):
            repo.set_auto_prep_result(db_path, cid, version, token, "failed", detail="Case or human package changed; reopen before retrying.")
            result.update(state="stale", reasons=["Case data or human package changed; reopen and review."])
            return result
        try:
            local_runner(db_path, norm, version, token)
        except Exception:
            repo.set_auto_prep_result(db_path, cid, version, token, "failed", detail="Local preparation failed; continue manually.")
            if _after_work_changed(db_path, norm, observation, version, result): return result
            result["reasons"] = ["Local preparation failed; continue manually."]
            return result
        if _after_work_changed(db_path, norm, observation, version, result): return result
        saved = repo.get_auto_prep(db_path, cid) or {}
        if saved.get("version_hash") != version or saved.get("claim_token") != token:
            result.update(state="stale", reasons=["Preparation was superseded; review current data."]); return result
        if saved.get("status") == "ready":
            package = repo.get_package(db_path, saved.get("package_id")) if saved.get("package_id") else None
            if package and package.get("dispute_id") == cid:
                result.update(state="ready", package_id=package["id"], package_version=package["version"], human_href=f"/disputes/{cid}/packages/{package['id']}", reasons=["Review the prepared package; a manual shell may have no statements."])
                return result
        if saved.get("status") == "needs_documents":
            result.update(state="needs_input", missing=[saved.get("detail") or "Add a case document."]); return result
        result["reasons"] = ["Preparation did not complete; continue manually."]
        return result
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        result["reasons"] = ["Preparation data could not be read; continue manually."]
        return result
