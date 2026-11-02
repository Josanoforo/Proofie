"""Safety facts keep one stable hook per view (rulebook v2 V5, section 5): the fact is said once, short, in the page flow, and
its long wording stays in the DOM inside the toggletip. Expected before the first run: on every case view the due date and the
assistant-only line appear once; prepare, review and approve say once that nothing is sent; the send view, before anything
was sent, shows one read-back chip with the full sentence in its toggletip and no read-back panel.
"""
import pytest

from _safety import assert_safety_fact, safety_nodes
from test_case_workspace import DID, _client

VIEWS = ("overview", "prepare", "review", "approve", "send", "records", "other")


def _get(monkeypatch, view):
    path = f"/disputes/{DID}" if view == "overview" else f"/disputes/{DID}/workspace/{view}"
    r = _client(monkeypatch).get(path)
    assert r.status_code == 200
    return r.text


@pytest.mark.parametrize("view", VIEWS)
def test_due_date_and_assistant_only_appear_once_on_every_case_view(monkeypatch, view):
    html = _get(monkeypatch, view)
    assert_safety_fact(html, "due-date", short="Respond by")
    assert_safety_fact(html, "assistant-only", short="PayPal sandbox")


@pytest.mark.parametrize("view,short", [("prepare", "Preparing a package sends nothing"),
                                        ("approve", "Approving does not send anything")])
def test_nothing_is_sent_is_said_once_per_task_view(monkeypatch, view, short):
    assert_safety_fact(_get(monkeypatch, view), "nothing-sent", short=short)


def test_review_with_a_package_says_nothing_is_sent_once(monkeypatch):
    from test_case_workspace import _with_package
    c, _ = _with_package(monkeypatch, ("claims",))
    assert_safety_fact(c.get(f"/disputes/{DID}/workspace/review").text, "nothing-sent", short="Reading and checking sends nothing")   # O4 reworded the same fact; still visible exactly once


def test_send_before_sending_shows_one_chip_not_a_read_back_panel(monkeypatch):
    html = _get(monkeypatch, "send")
    assert_safety_fact(html, "read-back", short="— Read-back: not sent yet",
                       long="Nothing has been sent to PayPal from this app yet. Nothing is sent until you review, approve and send a package.")
    assert 'data-state="read-back-chip"' in html
    assert "<h2>What was sent and what PayPal confirmed</h2>" not in html
    assert "evidence id" not in html.lower()
    assert len(safety_nodes(html, "read-back")) == 1
