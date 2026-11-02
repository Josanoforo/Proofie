"""The case workspace: the left task navigation and the focused task views of /disputes/<id>.

Display only. Every state word comes from the task rows, package status and PayPal read the dispute page already
builds (routes_disputes._case_tasks and friends); nothing here decides a state, grants a send or calls PayPal."""
from copilot import config

ORDER = {"prepare": 1, "review": 2, "approve": 3, "send": 4, "records": 5}      # the order of the work, shown in the navigation
VIEWS = ("prepare", "review", "approve", "send", "records", "other", "sale", "hackathon")

VIEW_TITLES = {"overview": "Case overview", "prepare": "Prepare", "review": "Review claims", "approve": "Approve package",
               "send": "Send and check", "records": "Case records", "other": "Other responses", "sale": "Sale records",
               "hackathon": "Hackathon review"}

# Judge-only pages (not for sellers). The Hackathon review view and its nav entry exist only while one of these is on.
JUDGE_FLAGS = ("judge_walkthrough", "local_demo_hub", "mcp_local_contracts", "local_autopilot", "evaluation_workbench",
               "judge_as_buyer", "evidence_dossier", "appeal_workspace", "replay", "presentation_mode", "conformance")


def view_url(dispute_id: str, view: str) -> str:
    return f"/disputes/{dispute_id}" if view == "overview" else f"/disputes/{dispute_id}/workspace/{view}"


def judge_pages_on() -> bool:
    # literal names, so the flag inventory (tests/local/test_feature_global_and_anchors.py) can see them
    return bool(config.feature_enabled("judge_walkthrough") or config.feature_enabled("local_demo_hub")
                or config.feature_enabled("mcp_local_contracts") or config.feature_enabled("local_autopilot")
                or config.feature_enabled("evaluation_workbench") or config.feature_enabled("judge_as_buyer")
                or config.feature_enabled("evidence_dossier") or config.feature_enabled("appeal_workspace")
                or config.feature_enabled("replay") or config.feature_enabled("presentation_mode")
                or config.feature_enabled("conformance"))


def _task(tasks, key):
    return next((t for t in tasks if t["key"] == key), None)


def _state_words(row, fallback):
    if row is None:
        return fallback
    return row["label"]


def nav_items(dispute_id, current, tasks, latest, capture):
    """[(group, [item, ...])]: one item per task view with its one-line state. `current` is a view key."""
    upload, prepare = _task(tasks, "upload"), _task(tasks, "prepare")
    approve, send = _task(tasks, "approve"), _task(tasks, "send")
    review_state = "No package yet"
    if latest is not None:
        view = latest.get("status_view")
        review_state = f"Package v{latest['version']}" + (f": {view.label}" if view is not None else "")
    items = [
        ("overview", "Next task and task list"),
        ("prepare", _state_words(upload, "Documents and shipment details")),
        ("review", review_state),
        ("approve", _state_words(approve, "Not started")),
        ("send", _state_words(send, "Not started")),
        ("records", "History, PayPal's timeline and why"),
        ("other", "Other things you can do"),
    ]
    related = []
    if capture or config.feature_enabled("fulfilment"):
        related.append(("sale", "Sale checklist and tracking" if capture else "No sale linked to this case"))
    if judge_pages_on():
        related.append(("hackathon", "Judge pages and local checks"))

    def build(rows):
        return [{"key": key, "label": VIEW_TITLES[key], "state": state, "href": view_url(dispute_id, key),
                 "current": key == current, "n": ORDER.get(key), "done": state.startswith("Done")} for key, state in rows]
    groups = [("Case", build(items))]
    if related:
        groups.append(("Related", build(related)))
    return groups


# ---- sale readiness in the seller's words (the service keeps its field names and statuses; this only words them)
_FIELD_WORDS = {"transaction_ref": "the transaction reference", "buyer_ref": "the buyer reference",
                "description": "the item description", "variant": "the item variant",
                "expected_date": "the expected delivery date",
                "valid_expected_date": "a valid expected delivery date (year-month-day)",
                "returns": "the returns policy", "contact": "your contact details",
                "terms_version": "which version of your terms the buyer saw",
                "acceptance_ref": "how the buyer accepted your terms"}
_PROOF_WORDS = {"order": "proof of the order", "tracking": "proof of tracking", "delivery": "proof of delivery (comes later)",
                "purchase": "proof of the purchase", "access": "proof the buyer got access", "agreement": "the agreement",
                "acceptance": "proof the buyer accepted", "performance": "proof the service was done"}
_STATUS_WORDS = {"ready for human review": "Ready for you to review.",
                 "tracking preparation ready; proof of delivery is a later-stage record":
                     "Tracking can be prepared. Proof of delivery comes later.",
                 "incomplete; resolve listed gaps": "Not ready yet. What is missing is listed below."}


def _gap_words(gap: str) -> str:
    if gap in _FIELD_WORDS:
        return f"Missing: {_FIELD_WORDS[gap]}."
    kind, _, rest = gap.partition(":")
    if kind == "evidence":
        return f"Missing: {_PROOF_WORDS.get(rest, 'a proof document')}."
    if kind == "evidence_unlinked":
        return "A document is not tied to a kind of proof yet."
    if kind == "tracking":
        return f"Tracking needs: {rest}."
    return gap if " " in gap else f"Missing: {gap.replace('_', ' ')}."


def sale_readiness_words(view):
    """The sale-readiness view from the service, as plain sentences. None when there is no view."""
    if not view:
        return None
    result = view.get("result")
    price = None
    if result and type(result.get("price_cents")) is int and config.feature_enabled("sale_readiness"):
        from copilot.features.draft.facts import money_text      # the view only exists with this flag on
        price = money_text(result["price_cents"], result.get("currency"))
    source = ("Built from PayPal's read of the sale, the documents on this case and the sale terms you recorded. "
              "Proofie has not checked that the terms are true." if result else
              "Built from the documents on this case. You have not recorded the sale terms yet.")
    return {"status": _STATUS_WORDS.get(result.get("status"), result.get("status")) if result else None,
            "price": price, "source": source, "missing": [_gap_words(g) for g in view.get("gaps") or []]}
