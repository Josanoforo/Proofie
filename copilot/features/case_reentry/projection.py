"""Build a safe case-reentry view from rows already loaded by the normal case request.

This module deliberately has no repository, filesystem, provider, or model access.
"""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit


_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LOCAL_ACTIONS = {
    "delivery_incident_response": "Seller-recorded delivery incident response",
    "upload_document": "Document saved to this case",
    "include_document": "Document included in this case",
    "exclude_document": "Document excluded from this case",
    "start_manual_package": "Manual package work started",
    "start_manual_package_no_documents": "Manual package work started",
    "generate_package": "Package version saved",
    "set_manual_fields": "Case details saved",
    "add_claim": "Package claim saved",
    "edit_claim": "Package claim edited",
    "remove_claim": "Package claim removed",
    "attach_document": "Document attached to a package",
    "detach_document": "Document detached from a package",
    "approve_package": "Package approval recorded",
    "approve_despite_conflict": "Package approval recorded with a conflict noted",
    "invalidate_approval_after_edit": "Package approval cleared after an edit",
    "invalidate_active_approval": "Package approval cleared after a source change",
    "buyer_note_response": "Seller-recorded buyer-note response",
}
_ACTIONABLE = {"not_started", "in_progress", "blocked", "todo", "info", "na"}


def _positive_id(value):
    if type(value) is int and 0 < value <= 9223372036854775807:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        parsed = int(value)
        return parsed if parsed <= 9223372036854775807 else None
    return None


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _same_case_href(value, case_id, package_ids):
    if not isinstance(value, str) or not value.startswith(f"/disputes/{case_id}/"):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path != value.split("?", 1)[0].split("#", 1)[0]:
        return None
    match = re.fullmatch(rf"/disputes/{re.escape(case_id)}/packages/([1-9][0-9]{{0,18}})(?:#[A-Za-z0-9_-]+)?", value)
    if match and int(match.group(1)) not in package_ids:
        return None
    if not re.fullmatch(rf"/disputes/{re.escape(case_id)}/(?:workspace/(?:prepare|records)(?:#[A-Za-z0-9_-]+)?|history|packages/[1-9][0-9]{{0,18}}(?:#[A-Za-z0-9_-]+)?)", value):
        return None
    return value


def build(*, case_id, tasks, packages, audit_rows, package_states=None):
    """Return a serializable, read-only reentry projection.

    Inputs must be scoped to one case by the caller. Rows with an explicit foreign
    dispute ID or a package reference outside the supplied package list are ignored.
    ``package_states`` is optional and may contain only already-computed states.
    """
    if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
        return {"available": False, "reason": "Case context is unavailable."}
    if not isinstance(tasks, (list, tuple)) or not isinstance(packages, (list, tuple)) or not isinstance(audit_rows, (list, tuple)):
        return {"available": False, "reason": "Saved case context is unavailable."}
    if package_states is None:
        package_states = {}
    if not isinstance(package_states, dict):
        package_states = {}

    package_by_id = {}
    for row in packages:
        if not isinstance(row, dict):
            continue
        package_id = _positive_id(row.get("id"))
        version = row.get("version")
        if package_id is None or type(version) is not int or version < 1:
            continue
        if row.get("dispute_id") not in (None, case_id):
            continue
        if package_id in package_by_id:
            package_by_id.pop(package_id, None)
            continue
        package_by_id[package_id] = row

    package_ids = set(package_by_id)
    package_views = []
    for package_id, row in sorted(package_by_id.items(), key=lambda item: (item[1]["version"], item[0])):
        href = f"/disputes/{case_id}/packages/{package_id}"
        state = package_states.get(package_id, package_states.get(str(package_id)))
        approval_valid = state.get("approval_valid") if isinstance(state, dict) else None
        if approval_valid is True:
            approval_note = "Approval is recorded as current for this package version."
        elif approval_valid is False:
            approval_note = "No current approval is recorded for this package version."
        else:
            approval_note = "Approval validity is unavailable for this package version."
        package_views.append({"id": package_id, "version": row["version"],
                              "created_at": row.get("created_at") if isinstance(row.get("created_at"), str) else None,
                              "href": href, "approval_note": approval_note})

    next_task = None
    for row in tasks:
        if not isinstance(row, dict) or not isinstance(row.get("status"), str) or row["status"] not in _ACTIONABLE:
            continue
        title = row.get("title")
        label = row.get("label")
        if not isinstance(title, str) or not title.strip():
            continue
        href = _same_case_href(row.get("href"), case_id, package_ids)
        next_task = {"title": title.strip(), "label": label.strip() if isinstance(label, str) else "",
                     "status": row["status"], "href": href}
        break

    saved_candidates = []
    for row in package_by_id.values():
        ts = _timestamp(row.get("created_at"))
        if ts:
            saved_candidates.append((ts, {"label": f"Package version v{row['version']} saved",
                                          "ts": row["created_at"], "source": "Local package record",
                                          "package_id": row["id"], "href": f"/disputes/{case_id}/packages/{row['id']}"}))
    for row in audit_rows:
        if not isinstance(row, dict) or row.get("dispute_id") not in (None, case_id):
            continue
        action = row.get("action")
        if not isinstance(action, str):
            continue
        label = _LOCAL_ACTIONS.get(action)
        ts = _timestamp(row.get("ts"))
        if not label or not ts:
            continue
        detail = row.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        package_id = _positive_id(detail.get("package_id"))
        if package_id is not None and package_id not in package_ids:
            continue
        if action == "delivery_incident_response":
            if row.get("actor") != "vendor" or detail.get("source") != "seller_recorded":
                continue
            if not isinstance(detail.get("capture_id"), str) or not detail.get("capture_id"):
                continue
        href = f"/disputes/{case_id}/packages/{package_id}" if package_id else None
        saved_candidates.append((ts, {"label": label, "ts": row["ts"], "source": "Local case audit",
                                      "package_id": package_id, "href": href,
                                      "attribution": "Recorded by the seller" if row.get("actor") == "vendor" else "Local system record"}))

    latest_saved_work = max(saved_candidates, key=lambda item: item[0])[1] if saved_candidates else None
    return {
        "available": True,
        "case_id": case_id,
        "next_task": next_task,
        "packages": package_views,
        "latest_saved_work": latest_saved_work,
        "history_href": f"/disputes/{case_id}/history",
        "limitations": [
            "This view summarizes saved local records; it does not record the last page you visited.",
            "An older approval does not approve another package version.",
            "Offline edits are available only in the same browser, case, package, and version when that editor has saved them.",
            "No prior review snapshot was supplied, so this view cannot say what changed since a review.",
        ],
    }
