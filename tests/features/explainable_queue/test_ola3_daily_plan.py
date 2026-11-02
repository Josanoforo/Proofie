from datetime import datetime, timezone

from copilot.db import repo
from copilot.features.explainable_queue.service import explain_cases, exposure_context
from copilot.web import inbox_view, status
from copilot.web.routes_disputes import _with_has_claims


NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def _case(case_id, status_code="WAITING_FOR_SELLER_RESPONSE", due=None):
    return {"dispute_id": case_id, "status": status_code, "stage": "INQUIRY",
            "seller_response_due_date": due, "amount_cents": None, "currency": None,
            "action_links": {"provide-evidence": {"href": f"https://api-m.sandbox.paypal.com/{case_id}",
                                                      "method": "POST"}}
            if status_code == "WAITING_FOR_SELLER_RESPONSE" else {}}


def _item(db_path, norm, *, raw=None, prep=None):
    view = status.dispute_status(norm, raw)
    step = inbox_view._next_step(norm, view, prep, False)
    packages = repo.list_packages(db_path, norm["dispute_id"])
    latest = max(packages, key=lambda row: (row["version"], row["id"])) if packages else None
    return {"norm": norm, "raw": raw, "auto_prep": prep, "next_step": step,
            "queue_status": view.code, "status_label": view.label,
            "documents": repo.list_documents(db_path, norm["dispute_id"]),
            "packages": packages,
            "claims": repo.list_claims(db_path, latest["id"]) if latest else [],
            "due": {"text": f"Respond by {norm['seller_response_due_date']}"}
                   if norm.get("seller_response_due_date") else None}


def test_daily_plan_groups_existing_next_steps_and_recomputes_from_local_records(tmp_path):
    """Expected in expected.md: deadline buckets/statuses are factual; stored records change plan progress/action."""
    db_path = tmp_path / "daily-plan.sqlite3"
    rows = [
        _case("LNB-DAY-OVERDUE", due="2026-10-31T17:00:00Z"),
        _case("LNB-DAY-SOON", due="2026-11-02T17:00:00Z"),
        _case("LNB-DAY-LATER", due="2026-11-05T18:00:00Z"),
        _case("LNB-DAY-NONE"),
        _case("LNB-DAY-INVALID", due="not-a-date"),
        _case("LNB-DAY-BUYER", "WAITING_FOR_BUYER_RESPONSE"),
        _case("LNB-DAY-REVIEW", "UNDER_REVIEW"),
        _case("LNB-DAY-DONE", "RESOLVED"),
    ]
    before_items = [_item(db_path, norm) for norm in rows]
    before = exposure_context(before_items, NOW.isoformat())["daily_plan"]
    groups = {group["key"]: group for group in before["groups"]}
    assert [task["case_id"] for task in groups["overdue"]["items"]] == ["LNB-DAY-OVERDUE"]
    assert [task["case_id"] for task in groups["due_soon"]["items"]] == ["LNB-DAY-SOON"]
    assert [task["case_id"] for task in groups["later"]["items"]] == ["LNB-DAY-LATER"]
    assert {task["case_id"] for task in groups["no_deadline"]["items"]} == {"LNB-DAY-NONE", "LNB-DAY-INVALID"}
    assert {task["case_id"] for task in groups["blocked"]["items"]} == {"LNB-DAY-BUYER", "LNB-DAY-REVIEW"}
    assert before["closed_count"] == 1 and before["task_count"] == 5
    task = groups["overdue"]["items"][0]
    assert task["action_label"] == "Respond to this dispute"
    assert task["action_href"] == "/disputes/LNB-DAY-OVERDUE"
    assert task["record_counts"] == {"documents": 0, "packages": 0, "claims": 0}

    # Simulate a completed local preparation through real repository records in the fixture DB.
    case_id = "LNB-DAY-OVERDUE"
    document_id = repo.insert_document(db_path, case_id, "proof.pdf", "application/pdf", 4,
                                       "a" * 64, "synthetic fixture", True)
    package_id = repo.insert_package(db_path, case_id, "human_edit", True, [], effective_next_step="Review")
    repo.insert_claims(db_path, package_id, [{"evidence_index": 0, "claim_index": 0, "text": "Seller statement",
                                              "source_ids": [str(document_id)], "basis": "seller_statement"}])
    token = repo.claim_auto_prep(db_path, case_id, "version-1")
    assert token
    assert repo.set_auto_prep_result(db_path, case_id, "version-1", token, "ready", package_id=package_id)
    updated_items = [_item(db_path, norm,
                           prep=_with_has_claims(db_path, repo.get_auto_prep(db_path, norm["dispute_id"]))
                           if norm["dispute_id"] == case_id else None) for norm in rows]
    after = exposure_context(updated_items, NOW.isoformat())["daily_plan"]
    updated = next(task for group in after["groups"] for task in group["items"] if task["case_id"] == case_id)
    assert updated["action_label"] == "Review the draft"
    assert updated["action_href"] == f"/disputes/{case_id}/packages/{package_id}"
    assert updated["record_counts"] == {"documents": 1, "packages": 1, "claims": 1}
    assert after["progress"]["cases_with_documents"] == 1
    assert after["progress"]["cases_with_package"] == 1
    assert after["progress"]["cases_with_claims"] == 1

    # Same `explain_cases` handoff that the habitual inbox route supplies to its existing fragment.
    explained = explain_cases(updated_items, NOW.isoformat())
    context = next(iter(explained.values()))["exposure_context"]
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("copilot/web/templates"))
    env.globals["feature_enabled"] = lambda name: False   # O4: the partial asks the app's flag helper; a bare environment has none
    rendered = env.get_template(
        "features/explainable_queue/_exposure_context.html").render(exposure_context=context)
    assert 'data-state="daily-work-plan"' in rendered
    assert f'href="/disputes/{case_id}/packages/{package_id}"' in rendered
    assert "Current local records: 1 case with documents, 1 case with an evidence package" in rendered


def test_daily_plan_deduplicates_ids_and_does_not_invent_rows(tmp_path):
    rows = [_item(tmp_path / "dedupe.sqlite3", _case("LNB-DUP", due="2026-11-01T18:00:00Z")),
            _item(tmp_path / "dedupe.sqlite3", _case("LNB-DUP", due="2026-11-01T18:00:00Z")),
            {"norm": {"status": "WAITING_FOR_SELLER_RESPONSE"}, "queue_status": "waiting_for_you"}]
    plan = exposure_context(rows, NOW.isoformat())["daily_plan"]
    assert plan["duplicate_rows"] == 1 and plan["unidentified_rows"] == 1
    assert sum(group["count"] for group in plan["groups"]) == 1
