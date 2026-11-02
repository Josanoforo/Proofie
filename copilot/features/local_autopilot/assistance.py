"""Read-only bridge for case preparation and deterministic action guidance.

The inbox owns scheduling and its claim token; this module never schedules, approves or sends.
"""
from datetime import datetime, timezone
import sqlite3

from copilot import config
from copilot.core import auto_prep
from copilot.db import repo
from copilot.features.local_autopilot import service


def _base_context(db_path, norm, allowed_actions, *, now=None, model_output=None):
    """Return status and a conservative recommendation from a freshly read case.

    model_output is deliberately ignored: an unvalidated assistant result is never a fact.
    The caller must pass the current normalized case and freshly calculated allowed actions.
    """
    if not isinstance(norm, dict) or not isinstance(norm.get("dispute_id"), str):
        return {"preparation": "unavailable", "recommendation": None, "reason": "Current case data is unavailable.", "facts": []}
    case_id = norm["dispute_id"]
    try:
        version = auto_prep.compute_version_hash(db_path, norm)
        saved = repo.get_auto_prep(db_path, case_id)
        if not auto_prep.should_prepare(norm):
            prep = "not eligible for automatic preparation"
        elif not saved:
            prep = ("not started; prepare locally below" if config.feature_enabled("local_preparation")
                    else "not started; open the inbox to prepare")
        elif saved.get("version_hash") != version:
            prep = ("case data changed; prepare locally again" if config.feature_enabled("local_preparation")
                    else "case data changed; open the inbox to prepare again")
        else:
            prep = {"preparing": "preparing", "ready": "ready for human review", "needs_documents": "documents needed", "failed": "preparation failed; continue manually"}.get(saved.get("status"), "preparation status unavailable")
        package = repo.get_package(db_path, saved["package_id"]) if saved and saved.get("package_id") is not None and saved.get("version_hash") == version and saved.get("status") == "ready" else None
        if package and package.get("dispute_id") != case_id:
            package = None
        if saved and saved.get("version_hash") == version and saved.get("status") == "ready" and not package:
            prep = "prepared package unavailable; continue manually"
        documents = [d for d in repo.list_documents(db_path, case_id) if not d.get("excluded") and d.get("extraction_ok") and isinstance(d.get("extracted_text"), str) and d["extracted_text"].strip()]
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {"preparation": "unavailable", "recommendation": None, "reason": "Preparation data could not be read; continue manually.", "facts": []}
    facts = [{"id": f"doc_{d['id']}", "source": f"Seller-supplied document {d['filename']}"} for d in documents]
    if saved and saved.get("version_hash") != version:
        return {"preparation": prep, "recommendation": None, "reason": "The preparation is stale; review current data before preparing again.", "facts": facts, "version": version}
    actions = set(allowed_actions) if isinstance(allowed_actions, (list, tuple, set)) else set()
    deadline = norm.get("seller_response_due_date") if norm.get("deadline_parse_ok") else None
    try:
        due = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if due.tzinfo is None or current.tzinfo is None or due <= current:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        return {"preparation": prep, "recommendation": None, "reason": "Deadline is expired or unavailable; review the case manually.", "facts": facts, "version": version, "package_id": package["id"] if package else None,
                "dispute_amount_cents": norm.get("amount_cents"), "currency": norm.get("currency")}
    if norm.get("status") not in {"OPEN", "WAITING_FOR_SELLER_RESPONSE"} or not facts:
        reason = "Review the current case state and add a readable source before preparing a suggestion."
        action = None
    elif "message" in actions:
        action, reason = "message", "A message is available and asks for an update without proposing a payment. Review the recorded source first."
    elif "offer" in actions and type(norm.get("amount_cents")) is int and norm["amount_cents"] > 0 and norm.get("currency") in service.CENT_CURRENCIES:
        action, reason = "offer", "An offer is available; choose an amount within the disputed amount and policy after review."
    else:
        action, reason = None, "No supported action with known limits is available; continue manually."
    return {"preparation": prep, "recommendation": action, "reason": reason, "facts": facts, "version": version,
            "package_id": package["id"] if package else None,
            "dispute_amount_cents": norm.get("amount_cents"), "currency": norm.get("currency")}


def case_context(db_path, norm, allowed_actions, *, now=None, model_output=None, model_backend=None):
    """Build the current plan without modifying drafts; model_backend is explicit opt-in."""
    context = _base_context(db_path, norm, allowed_actions, now=now, model_output=model_output)
    from copilot.features.draft.facts import money_text
    currency = norm.get("currency") if isinstance(norm, dict) else None
    amount = norm.get("amount_cents") if isinstance(norm, dict) else None
    context["amount_display"] = money_text(amount, currency) if type(amount) is int and currency in (service.CENT_CURRENCIES | {"JPY", "HUF", "TWD"}) else None
    if not isinstance(norm, dict) or not isinstance(norm.get("dispute_id"), str) or context.get("preparation") == "unavailable":
        return context
    try:
        from copilot.features.local_autopilot import attention_plan
        documents = repo.list_documents(db_path, norm["dispute_id"])
        model_hint = None
        if model_backend is not None:
            from copilot.features.local_autopilot.model_adapter import review_sources
            usable = [d for d in documents if not d.get("excluded") and d.get("extraction_ok") and isinstance(d.get("extracted_text"), str) and d["extracted_text"].strip()]
            docs = {f"doc_{d['id']}": d["extracted_text"] for d in usable}
            names = {f"doc_{d['id']}": d["filename"] for d in usable}
            model_hint = review_sources(norm, docs, names, model_backend)
        actions = [a for a in allowed_actions if a in {"message", "offer"}] if isinstance(allowed_actions, (list, tuple, set)) else []
        context["attention_plan"] = attention_plan.build(norm, actions, documents, context["preparation"], context.get("recommendation"), model_hint)
        if config.feature_enabled("local_preparation"):
            for task in context["attention_plan"]["tasks"]:
                if task.get("id") == "preparation" and task.get("href") == "inbox":
                    task["text"] = "Prepare this case locally, then review the resulting draft."
                    task["href"] = "local_preparation"
        if model_backend is None and config.feature_enabled('attention_model'):
            import json
            from copilot.features.local_autopilot.routes import _hint_path
            try:
                path = _hint_path(norm['dispute_id'])
                saved_hint = json.loads(path.read_text(encoding='utf-8')) if path.is_file() and path.stat().st_size <= 4096 else {}
                ids = saved_hint.get('source_ids')
                known = {source['id'] for source in context['attention_plan']['sources']}
                if saved_hint.get('fingerprint') == context['attention_plan']['fingerprint'] and isinstance(ids, list) and ids and all(isinstance(x, str) and x in known for x in ids):
                    context['attention_plan']['model_hint'] = {'label': 'Model suggestion: review these cited source documents; human review required.', 'source_ids': ids}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        context["attention_plan"] = None
    return context
