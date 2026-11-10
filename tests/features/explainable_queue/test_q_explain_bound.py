"""Q (v1.1.2): the queue explains at most what build() accepts and never fails the inbox."""
import pytest

from copilot.features.explainable_queue import service as queue

NOW = "2026-11-09T20:00:00+00:00"


def _item(n, waiting=True):
    return {"norm": {"dispute_id": f"PP-R-AAA-{n:08d}", "status": "WAITING_FOR_SELLER_RESPONSE", "stage": "INQUIRY",
                     "amount_cents": 10000, "currency": "MXN", "seller_response_due_date": "2026-11-20T00:00:00Z"},
            "waiting": waiting, "documents": [], "packages": [], "claims": [], "queue_status": "waiting", "status_label": "Waiting"}


def test_explain_cases_explains_what_build_accepts_and_leaves_the_rest_without_a_note():
    items = [_item(n) for n in range(400)]
    with pytest.raises(ValueError):                       # what v1.1.1 handed to build(): refused
        queue.build({"cases": [{"id": i["norm"]["dispute_id"], "pad": "x" * 200} for i in items]})
    out = queue.explain_cases(items, NOW)
    assert 0 < len(out) <= queue.MAX_EXPLAINED_CASES
    assert list(out) == [i["norm"]["dispute_id"] for i in items[:len(out)]]        # the caller's order, most urgent first


def test_a_refusal_from_build_gives_no_explanations_not_an_error(monkeypatch):
    def refuse(_data):
        raise ValueError("Input must be an object under 50 KB.")
    monkeypatch.setattr(queue, "build", refuse)
    assert queue.explain_cases([_item(1)], NOW) == {}
