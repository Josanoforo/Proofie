import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

CDMX = ZoneInfo("America/Mexico_City")
EXAMPLE = {"cases": [{"id": "CASE-NEAR", "status": "WAITING_FOR_SELLER_RESPONSE", "stage": "INQUIRY", "amount_cents": 12000, "currency": "USD", "deadline": "2026-10-24T20:00:00-06:00", "observed_at": "2026-10-22T20:00:00-06:00", "source": "synthetic sample", "missing": ["delivery proof"], "permitted_action": "Review proof"}, {"id": "CASE-UNKNOWN", "status": "WAITING_FOR_SELLER_RESPONSE", "amount_cents": 9000, "currency": "MXN", "deadline": None, "observed_at": "2026-10-22T20:00:00-06:00", "source": "synthetic sample"}, {"id": "CASE-DONE", "status": "RESOLVED", "deadline": "invalid", "observed_at": "2026-10-22T20:00:00-06:00", "source": "synthetic sample"}], "events": [{"id": "evt-1", "case_id": "CASE-NEAR", "observed_at": "2026-10-22T19:00:00-06:00", "source": "recorded import", "change": "stage opened"}, {"id": "evt-1", "case_id": "CASE-NEAR", "observed_at": "2026-10-22T19:00:00-06:00", "source": "recorded import", "change": "duplicate"}, {"id": "evt-late", "case_id": "CASE-NEAR", "observed_at": "2026-10-20T19:00:00-06:00", "source": "recorded import", "change": "older update"}]}


_DEADLINE_WORDS = {"unknown": "PayPal has not given a response deadline.", "invalid": "The response deadline could not be read.",
                   "passed": "The response deadline has passed.", "soon": "The response deadline is soon.",
                   "open": "The response deadline is open."}


def _gap_words(count):
    if not count:
        return "Nothing is missing from Proofie's records for this case."
    return f"Proofie is missing {count} record{'' if count == 1 else 's'} for this case."


def _aware(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp string or empty.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed if parsed.tzinfo else False


def build(data):
    try:
        encoded = json.dumps(data, ensure_ascii=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("Input must contain JSON values.") from exc
    if not isinstance(data, dict) or len(encoded) > 50000:
        raise ValueError("Input must be an object under 50 KB.")
    cases, events = data.get("cases", []), data.get("events", [])
    if not isinstance(cases, list) or not isinstance(events, list) or len(cases) > 200 or len(events) > 1000 or any(not isinstance(x, dict) for x in cases + events):
        raise ValueError("Cases and events must be bounded lists of objects.")
    latest_events = {}
    for event in events:
        for key in ("id", "case_id", "observed_at", "source", "change"):
            if key in event and not isinstance(event[key], str):
                raise ValueError(f"Event {key} must be text.")
        event_id, case_id = event.get("id"), event.get("case_id")
        if not event_id or not case_id or _aware(event.get("observed_at"), "event observed_at") in (None, False):
            raise ValueError("Every event requires id, case_id, and timezone-aware observed_at.")
        latest_events.setdefault(case_id, {})
        previous = latest_events[case_id].get(event_id)
        if previous is None or _aware(event["observed_at"], "event observed_at") > _aware(previous["observed_at"], "event observed_at"):
            latest_events[case_id][event_id] = event
    by_case = {}
    for case in cases:
        cid = case.get("id")
        observed = _aware(case.get("observed_at"), "observed_at")
        if not isinstance(cid, str) or not cid or observed in (None, False):
            raise ValueError("Every case requires an id and valid timezone-aware observed_at.")
        if "amount_cents" in case and (not isinstance(case["amount_cents"], int) or isinstance(case["amount_cents"], bool) or case["amount_cents"] < 0):
            raise ValueError("amount_cents must be a non-negative integer.")
        previous = by_case.get(cid)
        if previous is None or observed > previous[0]:
            by_case[cid] = (observed, case)
    rows = []
    now = datetime.now(timezone.utc)
    for cid, (observed, case) in by_case.items():
        due_raw = case.get("deadline")
        due = _aware(due_raw, "deadline") if due_raw else None
        remaining = (due.astimezone(timezone.utc) - now).total_seconds() if due not in (None, False) else None
        status = case.get("status", "unknown")
        if not isinstance(status, str) or not isinstance(case.get("stage", ""), str) or not isinstance(case.get("currency", ""), str) or not isinstance(case.get("source"), str) or not case["source"].strip():
            raise ValueError("Case status, stage and currency must be text; a recorded snapshot source is required.")
        missing = case.get("missing", [])
        if not isinstance(missing, list) or any(not isinstance(x, str) for x in missing):
            raise ValueError("missing must be a list of text values.")
        resolved = status.upper() in {"RESOLVED", "CLOSED", "DENIED", "ACCEPTED"}
        deadline_state = "unknown" if due_raw is None else "invalid" if due is False else "invalid" if due is None else "passed" if remaining < 0 else "soon" if remaining < 72 * 3600 else "open"
        case_events = sorted(latest_events.get(cid, {}).values(), key=lambda e: (_aware(e["observed_at"], "event observed_at"), e["id"]))
        latest_snapshot_time = observed
        timeline = [{"id": e["id"], "observed_at": e["observed_at"], "source": e.get("source", "source unstated"), "change": e.get("change", "change unstated"), "late": _aware(e["observed_at"], "event observed_at") < latest_snapshot_time} for e in case_events]
        rows.append({**case, "deadline_cdmx": due.astimezone(CDMX).strftime("%Y-%m-%d %H:%M %Z") if due not in (None, False) else None, "deadline_state": deadline_state, "remaining_seconds": remaining, "resolved": resolved, "next_action": "No response action: resolved" if resolved else case.get("permitted_action", "Review recorded case"), "priority_reason": "This case is resolved, so there is no response task." if resolved else f"{_DEADLINE_WORDS[deadline_state]} {_gap_words(len(missing))}", "timeline": timeline, "trust": case.get("trust") if isinstance(case.get("trust"), str) else "user-supplied snapshot/event metadata; verification not performed"})
    # Currencies are not comparable: sorting uses deadlines, with case ID as deterministic tie-breaker.
    rows.sort(key=lambda r: (r["resolved"], r["remaining_seconds"] is None, r["remaining_seconds"] if r["remaining_seconds"] is not None else 0, str(r.get("id"))))
    return rows


def explain_cases(items, observed_at, webhook_rows_by_case=None):
    """Annotate already-read Proofie cases. Caller retains the existing inbox order and action links."""
    webhook_rows_by_case = webhook_rows_by_case or {}
    exposure = exposure_context(items, observed_at)
    cases, events = [], []
    for item in items:
        norm = item.get("norm") or {}
        case_id = norm.get("dispute_id")
        if not isinstance(case_id, str) or not case_id:
            continue
        documents = item.get("documents") or []
        packages = item.get("packages") or []
        claims = item.get("claims") or []
        missing = []
        if not any(isinstance(d, dict) and not d.get("excluded") for d in documents):
            missing.append("No documents are stored in Proofie for this case yet.")
        if not packages:
            missing.append("No evidence package is stored in Proofie for this case yet.")
        elif not claims:
            missing.append("The latest evidence package in Proofie has no stored statements yet.")
        next_step = item.get("next_step") or {}
        action = next_step.get("label") or next_step.get("text") or "No response task"
        queue_status = item.get("queue_status")
        status = "RESOLVED" if queue_status in {"resolved", "buyer_closed"} else norm.get("status") or "unknown"
        status_labels = {"WAITING_FOR_SELLER_RESPONSE": "Waiting for your response",
                         "WAITING_FOR_BUYER_RESPONSE": "Waiting for the buyer", "UNDER_REVIEW": "Under review",
                         "RESOLVED": "Resolved", "OPEN": "Open"}
        case = {"id": case_id, "status": status,
                "status_label": item.get("status_label") or status_labels.get(status, "Status unknown"),
                "stage": norm.get("stage") or "unknown", "deadline": norm.get("seller_response_due_date"),
                "observed_at": observed_at, "source": "PayPal snapshot read in this Proofie request; gaps from the existing local case records",
                "trust": "Case details were read from PayPal just now. Missing records come from what Proofie has stored. Event notes list only verified PayPal notices. Proofie does not check that uploaded evidence is genuine.",
                "missing": missing, "permitted_action": action}
        if type(norm.get("amount_cents")) is int:
            case["amount_cents"] = norm["amount_cents"]
        case["currency"] = norm.get("currency") or ""
        cases.append(case)
        for event in webhook_rows_by_case.get(case_id, ()):
            if not isinstance(event, dict) or event.get("verification") != "verified":
                continue
            readback = event.get("readback_status")
            label = {"WAITING_FOR_SELLER_RESPONSE": "waiting for your response",
                     "WAITING_FOR_BUYER_RESPONSE": "waiting for the buyer", "UNDER_REVIEW": "under review",
                     "RESOLVED": "resolved", "OPEN": "open"}.get(readback)
            change = "Verified notice received; " + (f"Proofie's follow-up read said {label}." if label else
                                                      "no follow-up status was recorded.")
            events.append({"id": event.get("event_id"), "case_id": case_id,
                           "observed_at": event.get("received_at"), "source": "Proofie verified webhook store",
                           "change": change})
    if not cases:
        return {}
    rows = build({"cases": cases, "events": events})
    from copilot.features.draft.facts import money_text
    from copilot.web.timefmt import display
    for row in rows:   # house style: money by code from integer cents, dates in words (Mexico City time)
        row["amount_text"] = money_text(row.get("amount_cents"), row.get("currency"))
        row["deadline_text"] = display(row.get("deadline")) if row.get("deadline_cdmx") else None
    return {row["id"]: {**row, "exposure_context": exposure} for row in rows}


def exposure_context(items, observed_at):
    """Summarize the already-read inbox snapshot; performs no storage or provider access.

    Amounts are integer minor units grouped by currency. The denominator is the unresolved rows
    supplied by the caller, not all account sales or a historical period.
    """
    from collections import defaultdict
    import re

    if not isinstance(observed_at, str) or _aware(observed_at, "observed_at") in (None, False):
        raise ValueError("observed_at must be a timezone-aware ISO timestamp.")
    totals = defaultdict(lambda: {"amount_cents": 0, "known_cases": 0})
    active = incomplete = undated = duplicate_rows = unidentified_rows = 0
    seen_ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        norm = item.get("norm") or {}
        if not isinstance(norm, dict):
            continue
        case_id = norm.get("dispute_id")
        if not isinstance(case_id, str) or not case_id:
            unidentified_rows += 1
            continue
        if case_id in seen_ids:
            duplicate_rows += 1
            continue
        seen_ids.add(case_id)
        queue_status = item.get("queue_status")
        status = str(queue_status or norm.get("status") or "").lower()
        if status in {"resolved", "buyer_closed", "closed", "denied", "accepted"}:
            continue
        active += 1
        deadline = norm.get("seller_response_due_date")
        if not isinstance(deadline, str) or not deadline or _aware(deadline, "seller_response_due_date") in (None, False):
            undated += 1
        cents, currency = norm.get("amount_cents"), norm.get("currency")
        if type(cents) is not int or cents < 0 or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            incomplete += 1
            continue
        totals[currency]["amount_cents"] += cents
        totals[currency]["known_cases"] += 1
    from copilot.features.draft.facts import money_text
    currencies = [
        {"currency": code, **values, "amount_text": money_text(values["amount_cents"], code)}
        for code, values in sorted(totals.items())
    ]
    timestamp = _aware(observed_at, "observed_at").astimezone(CDMX)
    return {
        "observed_at": timestamp.strftime("%Y-%m-%d %H:%M %Z"),
        "period_label": "Point-in-time snapshot; no historical period or cash-flow forecast.",
        "denominator": active,
        "denominator_label": f"{active} unresolved dispute{'s' if active != 1 else ''} included in this snapshot",
        "source": "Amounts and statuses from the already-read PayPal dispute snapshot in this Proofie request.",
        "currencies": currencies,
        "incomplete_count": incomplete,
        "undated_count": undated,
        "complete": incomplete == 0 and unidentified_rows == 0,
        "completeness_label": "Amount coverage complete" if incomplete == 0 and unidentified_rows == 0 else "Amount coverage incomplete",
        "duplicate_rows": duplicate_rows,
        "unidentified_rows": unidentified_rows,
        "scope": "Disputed principal only; not predicted loss, fees, sales exposure, or a reconciliation.",
        "daily_plan": daily_plan_context(items, observed_at),
    }


def daily_plan_context(items, observed_at):
    """Group the inbox's existing next steps by real due/status facts and current local records.

    This is rebuilt from the current inbox rows on each request. It never stores a second task history,
    invents a deadline, changes inbox ordering, or chooses a new action.
    """
    observed = _aware(observed_at, "observed_at")
    if observed in (None, False):
        raise ValueError("observed_at must be a timezone-aware ISO timestamp.")
    observed_utc = observed.astimezone(timezone.utc)
    groups = {key: [] for key in ("overdue", "due_soon", "later", "no_deadline", "blocked")}
    seen, duplicate_rows, unidentified_rows = set(), 0, 0
    counts = {"cases": 0, "cases_with_documents": 0, "cases_with_package": 0, "cases_with_claims": 0,
              "documents": 0, "packages": 0, "claims": 0}
    closed_count = waiting_count = 0
    for item in items or ():
        if not isinstance(item, dict):
            continue
        norm = item.get("norm") if isinstance(item.get("norm"), dict) else {}
        case_id = norm.get("dispute_id")
        if not isinstance(case_id, str) or not case_id:
            unidentified_rows += 1
            continue
        if case_id in seen:
            duplicate_rows += 1
            continue
        seen.add(case_id)
        counts["cases"] += 1

        documents = [row for row in (item.get("documents") or ())
                     if isinstance(row, dict) and not row.get("excluded")]
        packages = [row for row in (item.get("packages") or ()) if isinstance(row, dict)]
        claims = [row for row in (item.get("claims") or ()) if isinstance(row, dict)]
        counts["documents"] += len(documents)
        counts["packages"] += len(packages)
        counts["claims"] += len(claims)
        counts["cases_with_documents"] += bool(documents)
        counts["cases_with_package"] += bool(packages)
        counts["cases_with_claims"] += bool(claims)

        status_code = str(item.get("queue_status") or "unknown").lower()
        if status_code in {"resolved", "buyer_closed"}:
            closed_count += 1
            continue
        if status_code != "waiting_for_you":
            groups["blocked"].append({
                "case_id": case_id,
                "status_label": str(item.get("status_label") or "Status not recognized"),
                "action_label": None,
                "action_href": None,
                "action_text": "No seller task is available from the current PayPal status.",
                "deadline_text": None,
                "missing": [],
                "record_counts": {"documents": len(documents), "packages": len(packages), "claims": len(claims)},
            })
            continue

        waiting_count += 1
        due_raw = norm.get("seller_response_due_date")
        due = _aware(due_raw, "seller_response_due_date") if due_raw else None
        if due in (None, False):
            group_key = "no_deadline"
            due_text = "No verified deadline available"
            sort_due = datetime.max.replace(tzinfo=timezone.utc)
        else:
            seconds = (due.astimezone(timezone.utc) - observed_utc).total_seconds()
            if seconds < 0:
                group_key = "overdue"
            elif seconds <= 72 * 60 * 60:
                group_key = "due_soon"
            else:
                group_key = "later"
            sort_due = due.astimezone(timezone.utc)
            due_view = item.get("due") if isinstance(item.get("due"), dict) else {}
            due_text = due_view.get("text") or due_raw

        next_step = item.get("next_step") if isinstance(item.get("next_step"), dict) else {}
        missing = []
        if not documents:
            missing.append("No documents are stored for this case.")
        if not packages:
            missing.append("No evidence package is stored for this case.")
        elif not claims:
            missing.append("The latest evidence package has no stored statements.")
        groups[group_key].append({
            "case_id": case_id,
            "status_label": str(item.get("status_label") or "Waiting for your response"),
            "action_label": next_step.get("label") if isinstance(next_step.get("label"), str) else None,
            "action_href": next_step.get("href") if isinstance(next_step.get("href"), str) else None,
            "action_text": next_step.get("text") if isinstance(next_step.get("text"), str) else None,
            "deadline_text": due_text,
            "missing": missing,
            "record_counts": {"documents": len(documents), "packages": len(packages), "claims": len(claims)},
            "_sort_due": sort_due,
        })

    labels = {"overdue": "Past due", "due_soon": "Due within 72 hours", "later": "Later deadline",
              "no_deadline": "No verified deadline", "blocked": "Waiting on PayPal or the buyer"}
    result_groups = []
    for key, rows in groups.items():
        rows.sort(key=lambda row: (row.get("_sort_due", datetime.max.replace(tzinfo=timezone.utc)), row["case_id"]))
        for row in rows:
            row.pop("_sort_due", None)
        if rows:
            result_groups.append({"key": key, "label": labels[key], "count": len(rows), "items": rows})
    return {
        "observed_at": observed.astimezone(CDMX).strftime("%Y-%m-%d %H:%M %Z"),
        "groups": result_groups,
        "task_count": waiting_count,
        "overdue_count": len(groups["overdue"]),
        "due_soon_count": len(groups["due_soon"]),
        "blocked_count": len(groups["blocked"]),
        "closed_count": closed_count,
        "progress": counts,
        "duplicate_rows": duplicate_rows,
        "unidentified_rows": unidentified_rows,
        "source": "Current PayPal dispute snapshot plus current local document, latest-package and statement records; rebuilt on each inbox read.",
        "scope": "Existing next-step actions are passed through unchanged. No deadline, task completion, outcome or priority is inferred beyond these records.",
    }


def _completed_sale_movements(transaction_rows, *, transaction_rows_complete, window_start, window_end):
    """Summarize only unique, dated T0006/S rows using the existing account-cliff sale rule."""
    from collections import defaultdict
    import re

    from copilot.core import disputes as _disputes  # ensures the project's pilot import path is initialized
    from step3_dispute import money_to_minor

    rows_by_id = defaultdict(list)
    incomplete = non_completed = unclassified = unidentified = 0
    for row in transaction_rows or ():
        info = row.get("transaction_info") if isinstance(row, dict) else None
        info = info if isinstance(info, dict) else {}
        transaction_id = info.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            if info.get("transaction_event_code") == "T0006" and info.get("transaction_status") == "S":
                unidentified += 1
                incomplete += 1
            elif info.get("transaction_event_code") == "T0006":
                non_completed += 1
            else:
                unclassified += 1
            continue
        rows_by_id[transaction_id].append(info)

    duplicates = sum(len(rows) - 1 for rows in rows_by_id.values() if len(rows) > 1)
    totals = defaultdict(lambda: {"amount_minor": 0, "transaction_count": 0, "by_date": defaultdict(
        lambda: {"amount_minor": 0, "transaction_count": 0})})
    included = 0
    for transaction_id, rows in rows_by_id.items():
        if len(rows) != 1:
            continue
        info = rows[0]
        if info.get("transaction_event_code") != "T0006":
            unclassified += 1
            continue
        if info.get("transaction_status") != "S":
            non_completed += 1
            continue
        raw_date = info.get("transaction_initiation_date")
        try:
            parsed = datetime.fromisoformat(raw_date.strip().replace("Z", "+00:00")) if isinstance(raw_date, str) else None
        except ValueError:
            parsed = None
        money = info.get("transaction_amount")
        try:
            amount_minor, currency = money_to_minor(money)
        except (ValueError, TypeError):
            amount_minor = currency = None
        if (parsed is None or parsed.tzinfo is None or amount_minor is None or type(amount_minor) is not int
                or amount_minor < 0 or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            incomplete += 1
            continue
        date_utc = parsed.astimezone(timezone.utc).date().isoformat()
        if not (window_start.isoformat() <= date_utc <= window_end.isoformat()):
            incomplete += 1
            continue
        code = currency
        totals[code]["amount_minor"] += amount_minor
        totals[code]["transaction_count"] += 1
        totals[code]["by_date"][date_utc]["amount_minor"] += amount_minor
        totals[code]["by_date"][date_utc]["transaction_count"] += 1
        included += 1

    from copilot.features.draft.facts import money_text
    by_currency = []
    for code, values in sorted(totals.items()):
        by_date = [{"date_utc": day, **day_values, "amount_text": money_text(day_values["amount_minor"], code)}
                   for day, day_values in sorted(values["by_date"].items())]
        by_currency.append({"currency": code, "amount_minor": values["amount_minor"],
                            "transaction_count": values["transaction_count"],
                            "amount_text": money_text(values["amount_minor"], code), "by_date": by_date})
    complete = (transaction_rows_complete is True and duplicates == 0 and incomplete == 0 and unidentified == 0)
    if transaction_rows_complete is not True:
        coverage = "Partial Transaction Search read; shown amounts include only returned T0006/S rows."
    elif duplicates or incomplete or unidentified:
        coverage = "Completed-sale amount coverage is incomplete; duplicate or unreadable rows were excluded."
    else:
        coverage = "Complete for the recognized T0006/S completed-sale rows in this read."
    return {
        "period_label": (f"{window_start.isoformat()} through {window_end.isoformat()} "
                         "(UTC calendar dates, inclusive; the previous complete-month window)"),
        "source": "PayPal Transaction Search transaction_info rows already read by account_cliff; completed-sale rule T0006/S.",
        "fetch_complete": transaction_rows_complete is True,
        "by_currency": by_currency,
        "included_transaction_count": included,
        "duplicate_transaction_rows": duplicates,
        "incomplete_transaction_rows": incomplete,
        "unidentified_transaction_rows": unidentified,
        "non_completed_t0006_rows": non_completed,
        "unclassified_event_rows": unclassified,
        "complete_for_returned_t0006_s": complete,
        "coverage_label": coverage,
        "scope": "Gross completed-sale transaction amounts only; no net proceeds, settlement, payout, refund, or cash-flow inference. Other event codes are not classified as money movement here.",
    }


def account_exposure_context(dispute_norms, *, sales_count, window_dispute_count, window_start, window_end,
                             sales_complete, disputes_complete, computed_at, transaction_rows=(),
                             transactions_complete=False):
    """Adapt the rows and counts already read by account_cliff.compute into a separate, auditable view.

    `sales_count` and `window_dispute_count` are the exact existing account_cliff counts; this adapter does not
    refetch or re-count sales. Current principal is limited to known active dispute statuses and integer cents.
    Historical sales/dispute counts are separate units in the same UTC calendar window; no rate is calculated here.
    """
    from collections import defaultdict
    import re

    from copilot.features.account_cliff import counting

    if not isinstance(computed_at, datetime) or computed_at.utcoffset() is None:
        raise ValueError("computed_at must be timezone-aware.")
    if type(window_start) is not date or type(window_end) is not date or window_start > window_end:
        raise ValueError("window_start and window_end must be ordered dates.")
    sales_complete = sales_complete is True
    disputes_complete = disputes_complete is True
    if sales_complete and (type(sales_count) is not int or sales_count < 0):
        raise ValueError("A complete sales read requires a non-negative integer sales_count.")
    if disputes_complete and (type(window_dispute_count) is not int or window_dispute_count < 0):
        raise ValueError("A complete dispute read requires a non-negative integer window_dispute_count.")

    unique, duplicate_rows, unidentified_rows = [], 0, 0
    seen = set()
    for norm in dispute_norms or ():
        if not isinstance(norm, dict) or not isinstance(norm.get("dispute_id"), str) or not norm["dispute_id"]:
            unidentified_rows += 1
            continue
        if norm["dispute_id"] in seen:
            duplicate_rows += 1
            continue
        seen.add(norm["dispute_id"])
        unique.append(norm)

    active_statuses = {"OPEN", "WAITING_FOR_SELLER_RESPONSE", "WAITING_FOR_BUYER_RESPONSE", "UNDER_REVIEW"}
    terminal_statuses = {"RESOLVED", "CLOSED", "DENIED", "ACCEPTED", "CANCELED", "CANCELLED"}
    totals = defaultdict(lambda: {"amount_cents": 0, "case_count": 0})
    active_count = closed_count = unclassified_status_count = incomplete_amount_count = 0
    undated_count = dated_count = 0
    for norm in unique:
        created = counting.dispute_date(norm)
        if created is None:
            undated_count += 1
        else:
            dated_count += 1
        state = str(norm.get("status") or "").upper()
        if state in terminal_statuses:
            closed_count += 1
            continue
        if state not in active_statuses:
            unclassified_status_count += 1
            continue
        active_count += 1
        cents, currency = norm.get("amount_cents"), norm.get("currency")
        if type(cents) is not int or cents < 0 or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            incomplete_amount_count += 1
            continue
        totals[currency]["amount_cents"] += cents
        totals[currency]["case_count"] += 1

    from copilot.features.draft.facts import money_text
    principal_by_currency = [
        {"currency": code, **values, "amount_text": money_text(values["amount_cents"], code)}
        for code, values in sorted(totals.items())
    ]
    amount_complete = disputes_complete and duplicate_rows == 0 and unidentified_rows == 0 \
        and unclassified_status_count == 0 and incomplete_amount_count == 0
    window_complete = sales_complete and disputes_complete and undated_count == 0 \
        and duplicate_rows == 0 and unidentified_rows == 0
    computed_cdmx = computed_at.astimezone(CDMX)
    period = (f"{window_start.isoformat()} through {window_end.isoformat()} (UTC calendar dates, inclusive; "
              "the previous complete-month window)")
    return {
        "as_of": computed_cdmx.strftime("%Y-%m-%d %H:%M %Z"),
        "period_label": period,
        "population_count": len(unique),
        "active_case_count": active_count,
        "closed_case_count": closed_count,
        "unclassified_status_count": unclassified_status_count,
        "principal_by_currency": principal_by_currency,
        "known_amount_case_count": sum(row["case_count"] for row in principal_by_currency),
        "incomplete_amount_count": incomplete_amount_count,
        "dated_dispute_count": dated_count,
        "undated_dispute_count": undated_count,
        "duplicate_rows": duplicate_rows,
        "unidentified_rows": unidentified_rows,
        "dispute_listing_complete": disputes_complete,
        "sales_read_complete": sales_complete,
        "principal_complete": amount_complete,
        "window_complete": window_complete,
        "complete": amount_complete and window_complete,
        "principal_label": "Known principal among returned open-dispute rows; coverage is partial." if not amount_complete
                           else "Known principal in the current open-dispute listing.",
        "sales_count": sales_count if sales_complete else None,
        "sales_denominator_label": "completed sales (T0006 with status S)" if sales_complete
                                  else "not available: the sales read is incomplete",
        "window_dispute_count": window_dispute_count if disputes_complete else None,
        "window_label": period,
        "source_disputes": "PayPal dispute-list rows normalized by Proofie; current status and disputed amount.",
        "source_sales": "PayPal Transaction Search rows; completed-sale rule T0006 / S.",
        "coverage_label": "Coverage complete" if amount_complete and window_complete else "Coverage incomplete",
        "scope": "Current disputed principal and separate historical counts only; no forecasting, fees, cash flow, or reconciliation.",
        "completed_sales": _completed_sale_movements(
            transaction_rows, transaction_rows_complete=transactions_complete,
            window_start=window_start, window_end=window_end),
    }
