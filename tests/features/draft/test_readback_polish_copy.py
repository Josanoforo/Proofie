from html import unescape
"""Approved copy-only sentence has one data source, fake page/SDK and no model."""
import pytest

pytestmark = pytest.mark.usefixtures("web_mode")

from copilot.features.draft import service
from test_draft_routes import _web
from draft_fakes import FakePayPal

COPY = "PayPal offers no send-message option for this dispute. You can copy this text and send it to the buyer yourself; PayPal\'s record will not show it."


def test_copy_only_data_and_rendered_sentence_share_the_approved_source(monkeypatch):
    assert service.SENTENCE_COPY == COPY
    web, fake, _call = _web(monkeypatch, FakePayPal(rels=("provide_evidence",)))
    page = web.get('/disputes/PP-R-TST-0000001/draft')
    assert page.status_code == 200 and COPY in unescape(page.text)
    assert 'data-channel="copy_only"' in page.text and 'data-state="handoff-form"' not in page.text
    monkeypatch.setattr(service, 'SENTENCE_COPY', 'DEMO DATA COPY LINE')
    second = web.get('/disputes/PP-R-TST-0000001/draft')
    assert second.status_code == 200 and 'DEMO DATA COPY LINE' in second.text
    assert COPY not in second.text
    assert all(method == 'GET' for method, _, _ in fake.calls)
