"""What was missing when the file was sent, what is missing now, and what could be added. No new rule, no guess.

* missing_now: copilot.core.checklist.evaluate over the same context and documents the dispute page uses
  (fetch_context, extracted_docs_for, cached_rules), filtered to the states `missing` and `conflict`.
* missing_at_send: only when the last package's stored review flags carry check ids, written as
  "check:<check id>:<missing|conflict>". The app writes no such flag today, so the usual answer is "not recorded at
  send time" and the page shows the current evaluation only.
* addable: the checklist's own seller message for each missing item, plus (when those features are importable and
  hold nothing for this dispute) a tracker registered before the dispute and an offer or a message, each with the page
  that adds it."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from copilot.core.checklist import UnknownCountryError, evaluate
from copilot.core.checklist_input import cached_rules, extracted_docs_for
from copilot.core.context import SRC_DISPUTE, Limitation, fetch_context
from copilot.core.own_tracker import own_tracker_client
from copilot.db import repo

FLAG_RE = re.compile(r"^check:([\w:]+):(missing|conflict)$")
NOT_RECORDED = "not recorded at send time"


@dataclass(frozen=True)
class Gaps:
    at_send_recorded: bool
    at_send_note: str
    missing_at_send: tuple = ()
    missing_now: tuple = ()          # dicts: id, label, state, message
    addable: tuple = ()              # dicts: text, href
    limitations: tuple = ()


def missing_then_and_now(client, db_path, dispute_id: str, norm: dict | None) -> Gaps:
    packages = repo.list_packages(db_path, dispute_id)
    if not packages:
        recorded, note, at_send = False, "No package was created for this dispute.", ()
    else:
        at_send = tuple({"id": m.group(1), "state": m.group(2)} for f in packages[-1].get("review_flags") or []
                        if (m := FLAG_RE.match(str(f))))
        recorded, note = (True, "") if at_send else (False, NOT_RECORDED)
    if norm is None:
        return Gaps(recorded, note, at_send, (), (), (Limitation(SRC_DISPUTE, "The dispute could not be read, so "
                                                                              "what is missing now was not computed."),))
    ctx = fetch_context(own_tracker_client(client, db_path, dispute_id), norm)
    limits = list(ctx.limitations)
    try:
        checks = evaluate(ctx, extracted_docs_for(db_path, dispute_id), cached_rules())
    except UnknownCountryError as exc:
        return Gaps(recorded, note, at_send, (), (), tuple(limits) + (Limitation("rules", str(exc)),))
    missing = tuple({"id": c.id, "label": c.label_for_seller, "state": c.state, "message": c.seller_message,
                     "action": c.action} for c in checks if c.state in ("missing", "conflict"))
    docs_href = f"/disputes/{dispute_id}"
    addable = [{"text": f"{m['label']}: {m['message']}", "href": docs_href} for m in missing if m["action"] != "none"]
    capture = (norm.get("seller_transaction_ids") or [None])[0]
    try:
        from copilot.features.fulfilment.lookup import file_for_transaction
    except ImportError:
        pass
    else:
        if capture and file_for_transaction(capture) is None:
            addable.append({"text": "A tracker registered before the dispute", "href": f"/fulfilment/{capture}"})
    try:
        from copilot.features.resolution.store import list_actions
    except ImportError:
        pass
    else:
        if not list_actions(dispute_id):
            addable.append({"text": "An offer or a message to the buyer", "href": f"/disputes/{dispute_id}/resolution"})
    return Gaps(recorded, note, at_send, missing, tuple(addable), tuple(limits))
