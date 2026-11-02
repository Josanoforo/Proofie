"""The due-date urgency tiers. Moved from tests/local/test_ux_rc41_inbox.py at integration: it imports
copilot.features.deadlines, so it lives with that feature's tests (the flags-off public export excludes the feature together
with its tests; tools/test_export_public.py dependency closure). The assertions are unchanged.
"""
from datetime import datetime, timedelta

import pytest
from _dom import parse  # noqa: F401
from _hosted import authenticated_client  # noqa: F401
import _ux_lint as lint  # noqa: F401

from copilot.app import create_app  # noqa: F401
from copilot.core import disputes as core_disputes  # noqa: F401
from copilot.web.routes_disputes import get_client  # noqa: F401
from test_ux_rc41_inbox import Fake, UTC, item  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


def test_urgency_tiers_and_same_day_wording():
    from copilot.features.deadlines.service import urgency
    now = datetime(2026, 10, 7, 18, tzinfo=UTC)

    def norm(hours):
        return core_disputes.normalize(item("PP-T", due_hours=None) | {
            "seller_response_due_date": (now + timedelta(hours=hours)).isoformat()}, now)
    assert urgency(norm(-5), now) == "passed"
    assert urgency(norm(30), now) == "soon"
    assert urgency(norm(71), now) == "soon"
    assert urgency(norm(72), now) == "ok"
    assert urgency(core_disputes.normalize(item("PP-N"), now), now) == "none"
