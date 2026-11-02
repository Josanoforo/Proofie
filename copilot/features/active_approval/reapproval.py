"""The status of a package whose approval an edit cleared: read from the approval rows and the audit log, never new data.

`earlier(...)` is None unless the newest approval row of the package was cleared by an edit (the standard
`invalidate_approval_after_edit` audit entry) and no valid approval followed it. The review page then draws ONE callout
next to the controls: blocked while a statement claims delivery that no cited source shows, otherwise a reminder that the
earlier approval is not restored. Nothing here permits or refuses an approval; the review service stays the only guard.
"""
from copilot.db import repo

EDIT_ACTION = "invalidate_approval_after_edit"


def earlier(db_path, dispute_id: str, package_id: int) -> dict | None:
    rows = repo.list_approvals(db_path, package_id)
    if not rows or not rows[-1].get("invalidated_at"):
        return None
    last = rows[-1]
    cleared_by_edit = any(e["action"] == EDIT_ACTION and (e.get("detail") or {}).get("approval_id") == last["id"]
                          for e in repo.list_audit(db_path, dispute_id))
    if not cleared_by_edit:
        return None
    return {"approval_id": last["id"], "approved_at": last["approved_at"], "cleared_at": last["invalidated_at"]}
