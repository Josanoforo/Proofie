"""For the later dispute-page link ("file prepared at the sale on <date>"): not wired into the dispute page here."""
from __future__ import annotations

import json

from copilot.features.fulfilment import store


def file_for_transaction(capture_id: str) -> dict | None:
    """A small summary of the latest pre-dispute file of a transaction, or None. Reads the local database only."""
    row = store.get_file(capture_id)
    if row is None:
        return None
    return {"capture_id": row["capture_id"], "created_at": row["created_at"], "status": row["status"],
            "file_sha256": row["file_sha256"], "request_sha256": row["request_sha256"],
            "tracking_number": json.loads(row["request_json"])["trackers"][0]["tracking_number"]}
