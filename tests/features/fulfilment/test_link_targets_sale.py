"""LINK-FIX: the link tests that need the fulfilment, sale-readiness or package-print namespaces.

They live with the feature tests because the public export copies tests/local unconditionally, and a test there may not import
files of a namespace the export leaves out (tools/test_export_public.py::test_a2_...). The helpers come from the local file.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "local"))
from test_link_targets import (CAPTURE, DID, _client, _dead_links, _hrefs, _own_data_folder)  # noqa: E402,F401


def _save_shipment():
    import hashlib
    import json
    from copilot.features.fulfilment import store
    from copilot.features.fulfilment.tracker_request import build_tracker_body
    tracker = build_tracker_body(CAPTURE, "SYNTH-123", "OTHER", "Synthetic carrier")
    digest = hashlib.sha256(json.dumps(tracker, sort_keys=True).encode()).hexdigest()
    store.save_file(CAPTURE, tracker, digest, digest, None, {}, "saved_without_registration", "Synthetic record")


NOTE_LINK = f"/fulfilment/{CAPTURE}/buyer-note?locale=en"
ISSUE_LINK = f"/fulfilment/{CAPTURE}/delivery-incident?from_dispute={DID}&incident=delivered_not_received&locale=en"


def test_prepare_page_offers_the_sale_buyer_note_and_delivery_issue_only_with_a_saved_shipment(monkeypatch):
    c = _client(monkeypatch)
    page = c.get(f"/disputes/{DID}/workspace/prepare")
    assert page.status_code == 200
    hrefs = _hrefs(page.text)
    assert NOTE_LINK not in hrefs and ISSUE_LINK not in hrefs
    assert 'data-state="shipment-needed-for-notes"' in page.text and "Save the tracking first" in page.text
    assert _dead_links(c, page.text) == []
    _save_shipment()
    page = c.get(f"/disputes/{DID}/workspace/prepare")
    hrefs = _hrefs(page.text)
    assert NOTE_LINK in hrefs and ISSUE_LINK in hrefs
    assert 'data-state="shipment-needed-for-notes"' not in page.text
    for href in (NOTE_LINK, ISSUE_LINK):
        assert c.get(href).status_code == 200, href
    assert _dead_links(c, page.text) == []


def test_photo_link_keeps_the_case_when_the_sale_terms_are_saved(monkeypatch):
    from copilot.features.sale_readiness import service as readiness
    monkeypatch.setattr(readiness, "load_case_terms", lambda dispute_id, capture_id: {
        "schema_version": 1, "dispute_id": dispute_id, "capture_id": capture_id, "terms": {}})
    c = _client(monkeypatch)
    entry = f"/fulfilment/{CAPTURE}/moment?from_dispute={DID}"
    prepare = c.get(f"/disputes/{DID}/workspace/prepare")
    assert entry in _hrefs(prepare.text) and "shipment-photos-case-link-needs-terms" not in prepare.text
    page = c.get(entry)
    assert page.status_code == 200 and f'name="from_dispute" value="{DID}"' in page.text


def test_print_refusal_names_its_reason(monkeypatch):
    from copilot.features.package_print import routes as print_routes
    from copilot.features.package_print.service import PrintError

    def refuse(*args):
        raise PrintError("A local exhibit is missing or inconsistent.")
    monkeypatch.setattr(print_routes, "read", refuse)
    c = _client(monkeypatch)
    page = c.get(f"/disputes/{DID}/packages/3/print")
    assert page.status_code == 400
    assert "Cannot print this package. Review its local sources first." in page.text
    assert "Reason: A local exhibit is missing or inconsistent." in page.text


def test_copy_buttons_use_the_guarded_handler_not_an_inline_clipboard_call(monkeypatch):
    c = _client(monkeypatch)
    for path in (f"/disputes/{DID}/buyer-note?capture_id={CAPTURE}",):
        html = c.get(path).text
        assert "navigator.clipboard" not in html and html.count("data-copy-target=") == 2
    _save_shipment()
    for path in (f"/fulfilment/{CAPTURE}/buyer-note?locale=en", ISSUE_LINK):
        html = c.get(path).text
        assert "navigator.clipboard" not in html and "data-copy-target=" in html
    script = c.get("/static/app.js").text
    assert "Copy failed: the text is selected" in script and "failed" in script and ".then(done, failed)" in script
