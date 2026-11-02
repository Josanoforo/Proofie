"""The events page. Moved from tests/local/test_ux_rc41_inbox.py at integration: it imports
copilot.features.events, so it lives with that feature's tests (the flags-off public export excludes the feature together
with its tests; tools/test_export_public.py dependency closure). The assertions are unchanged.
"""

import pytest
from _dom import parse  # noqa: F401
from _hosted import authenticated_client  # noqa: F401
import _ux_lint as lint  # noqa: F401

from copilot.app import create_app  # noqa: F401
from copilot.core import disputes as core_disputes  # noqa: F401
from copilot.web.routes_disputes import get_client  # noqa: F401
from test_ux_rc41_inbox import Fake, UTC, item  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


# ----------------------------------------------------------------------------------------- events
def test_events_page_title_count_sentence_labels_and_status_vocabulary(monkeypatch):
    import copilot.features.events.routes as routes
    monkeypatch.setenv("COPILOT_FEATURE_EVENTS", "true")
    row = {"dispute_id": "PP-E1", "version": "v1", "status": "UNDER_REVIEW", "due": None, "body_hash": "h", "source": "sweep",
           "checked_at": "2026-10-07T18:00:00+00:00", "review_version": None, "appeal_until": None, "observation": None,
           "human_reviewed": 1}
    state = {"rows": [row], "counters": {}, "unknown_count": 0}
    monkeypatch.setattr(routes, "view", lambda path: state)
    app = create_app()
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    html = web.get("/events/state").text
    dom = parse(html)
    assert dom.find("h1").text() == "What PayPal last showed" and dom.find("title").text() == "What PayPal last showed - Proofie"
    assert lint.breadcrumb_items(html) == [("All disputes", "/disputes"), ("What PayPal last showed", None)]
    assert lint.back_links(html) == [] and dom.find(data_state="unknown-count") is None
    assert all("data-label" in td.attrs for td in dom.find_all("td"))
    status_cell = dom.find("td", data_label="Status read from PayPal")
    assert "Under review by PayPal" in status_cell.text() and "updating" not in status_cell.text()
    state["unknown_count"] = 2
    assert "2 cases are not confirmed by PayPal yet. Open each dispute and refresh from PayPal." in web.get("/events/state").text
    state["unknown_count"] = 1
    assert "1 case is not confirmed by PayPal yet." in web.get("/events/state").text
