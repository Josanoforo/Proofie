"""PayPal dates only; core normalization owns remaining days and missing-date wording."""
from copilot.core.disputes import fetch_all_disputes, normalize, humanize_deadline, inbox_sort_key
from copilot.features.events.rate import ReadClient
from copilot.web import status, timefmt

WARNING_DAYS = 3  # warn strictly below this count of whole days; no invented deadline


def urgency(norm, now=None):
    """How close PayPal's own due date is: 'passed' | 'soon' | 'ok' | 'none' (PayPal gave no due date that parses).
    Uses the hours normalize() computed from that date; 'soon' is strictly under WARNING_DAYS whole days (so exactly 72
    hours is 'ok'). Display only: no decision and no write depends on it."""
    hours = norm.get("remaining_hours")
    if hours is None:
        return "none"
    if norm.get("overdue"):
        return "passed"
    return "soon" if hours < WARNING_DAYS * 24 else "ok"


def prepare(items, now):
    """One row per dispute, in the inbox order (core.disputes.inbox_sort_key). `waiting` rows are the ones PayPal waits
    on the seller for; only those get a tier, a due text and a tag."""
    rows = []
    for raw in items:
        norm = normalize(raw, now)
        view = status.dispute_status(norm, raw)
        waiting = view.needs_seller
        rows.append({"norm": norm, "raw": raw, "deadline_text": humanize_deadline(norm), "status": view,
                     "waiting": waiting, "urgency": urgency(norm, now) if waiting else None,
                     "due": timefmt.due_display(norm, now) if waiting else None,
                     "warning": norm["remaining_days"] is not None and norm["remaining_days"] < WARNING_DAYS})
    return sorted(rows, key=lambda r: inbox_sort_key(r["norm"], r["raw"]))


def read(client, now, budget=None):
    items, pages = fetch_all_disputes(ReadClient(client, budget))
    return prepare(items, now), pages
