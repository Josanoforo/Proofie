"""Causal exposure summary over the same cases used for queue explanations."""
from datetime import datetime, timedelta, timezone

from copilot.features.explainable_queue.service import explain_cases


def test_multi_case_exposure_is_currency_scoped_incomplete_and_tied_to_queue_actions():
    """Expected: active overdue/current/undated/blocked cases retain supplied next steps; totals are integer
    minor units grouped by currency; unresolved denominator, missing data and undated rows remain explicit.
    Resolved cases are excluded. No provider, model, DB, or writer is used by this pure adapter test.
    """
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=1)).isoformat()
    future = (now + timedelta(days=4)).isoformat()
    cases = [
        {"norm": {"dispute_id": "OVERDUE", "status": "WAITING_FOR_SELLER_RESPONSE", "amount_cents": 1250,
                  "currency": "USD", "seller_response_due_date": past}, "queue_status": "waiting_for_you",
         "status_label": "Waiting for your response", "next_step": {"label": "Upload delivery proof"}},
        {"norm": {"dispute_id": "CURRENT", "status": "WAITING_FOR_SELLER_RESPONSE", "amount_cents": 2500,
                  "currency": "USD", "seller_response_due_date": future}, "queue_status": "waiting_for_you",
         "status_label": "Waiting for your response", "next_step": {"label": "Review evidence package"}},
        {"norm": {"dispute_id": "UNDATED", "status": "WAITING_FOR_SELLER_RESPONSE", "amount_cents": 500,
                  "currency": "JPY", "seller_response_due_date": None}, "queue_status": "waiting_for_you",
         "status_label": "Waiting for your response", "next_step": {"label": "Add case details"}},
        {"norm": {"dispute_id": "BLOCKED", "status": "UNDER_REVIEW", "amount_cents": 700,
                  "currency": "MXN", "seller_response_due_date": future}, "queue_status": "under_review",
         "status_label": "Under review", "next_step": {"label": "No response task"}},
        {"norm": {"dispute_id": "PARTIAL", "status": "WAITING_FOR_SELLER_RESPONSE",
                  "seller_response_due_date": "not-a-date"}, "queue_status": "waiting_for_you",
         "status_label": "Waiting for your response", "next_step": {"label": "Add case details"}},
        {"norm": {"dispute_id": "DONE", "status": "RESOLVED", "amount_cents": 99999,
                  "currency": "USD", "seller_response_due_date": None}, "queue_status": "resolved",
         "status_label": "Resolved", "next_step": {"label": "No response task"}},
        {"norm": {"dispute_id": "CURRENT", "status": "WAITING_FOR_SELLER_RESPONSE", "amount_cents": 2500,
                  "currency": "USD", "seller_response_due_date": future}, "queue_status": "waiting_for_you",
         "status_label": "Waiting for your response", "next_step": {"label": "Review evidence package"}},
    ]

    explanations = explain_cases(cases, now.isoformat())
    assert explanations["OVERDUE"]["next_action"] == "Upload delivery proof"
    assert explanations["CURRENT"]["next_action"] == "Review evidence package"
    assert explanations["BLOCKED"]["next_action"] == "No response task"
    context = explanations["OVERDUE"]["exposure_context"]
    by_currency = {row["currency"]: row for row in context["currencies"]}
    assert context["denominator"] == 5
    assert context["duplicate_rows"] == 1
    assert by_currency["USD"]["amount_cents"] == 3750
    assert by_currency["USD"]["known_cases"] == 2
    assert by_currency["JPY"]["amount_cents"] == 500
    assert by_currency["MXN"]["amount_cents"] == 700
    assert "USD" not in by_currency or by_currency["USD"]["amount_cents"] != 4950
    assert context["incomplete_count"] == 1
    assert context["undated_count"] == 2
    assert context["complete"] is False
    assert context["completeness_label"] == "Amount coverage incomplete"
    assert "Point-in-time" in context["period_label"]
    assert "PayPal dispute snapshot" in context["source"]
    assert "reconciliation" in context["scope"].lower()
