"""LINK-FIX2 (Nov 3): two links the integrated crawl of 981d955e found that a seller can click and land on an error.

F1: "Save label or parcel photos and carrier now" drew `/fulfilment/{id}/moment?from_package=N` (no case) when the sale terms were
    not saved yet; that page needs a case for a package, so it answered 404. Without a case the link now carries no query.
F2: "Continue to the sale checklist" carried the id of the latest package even when that version was already sent; the checklist
    refuses a sent version (409). The link now names a package only while the latest version can still be edited.

Expected, written before the first run (data/kits/Proofie-Kits/logs-RC44/D101/link-fix2/expected.md; all four fail on 1f38178c):
- case page with a package and no saved terms: the photo link has no query and answers 200; the checklist page opened from a
  case + package without saved terms draws the same query-less link;
- case page whose latest package was sent: the checklist link keeps `from_dispute`, drops `from_package`, answers 200 and the
  page has id="sale-readiness"; the "Review the same case package" link still names the sent version;
- case page whose latest package can be edited: the checklist link keeps `from_package` and answers 200.
Offline: the send tests' fake PayPal client, synthetic rows.
"""
import os
import re

import pytest

import test_routes_send as send_tests
from test_ux_rc41_package import DID, new_package, sent_fixture

pytestmark = [pytest.mark.usefixtures("web_mode")]

FLAGS = ("sale_readiness", "fulfilment", "ship_moment_capture")


@pytest.fixture
def flags(monkeypatch):
    """(client, db, csrf token, fake PayPal client); the three features are on before the app is built (their routes are
    registered when the app is created)."""
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    for name in FLAGS:
        monkeypatch.setenv("COPILOT_FEATURE_" + name.upper(), "true")
    return send_tests._client(None, monkeypatch)


def _href(html, text):
    found = re.findall(r'<a\b[^>]*?href="([^"]*)"[^>]*>\s*' + re.escape(text), html)
    assert found, text
    return found[0].replace("&amp;", "&")


def _capture(html):
    return re.search(r'<span translate="no">([^<]+)</span>', html).group(1)


def test_photo_link_without_saved_terms_opens_the_capture_form(flags):
    c, db, token, _ = flags
    new_package(c, token)
    page = c.get(f"/disputes/{DID}/workspace/prepare")
    assert page.status_code == 200
    link = _href(page.text, "Save label or parcel photos and carrier now")
    assert "from_package" not in link and "?" not in link, link
    assert c.get(link).status_code == 200


def test_checklist_page_with_a_package_and_no_terms_draws_the_same_photo_link(flags):
    c, db, token, _ = flags
    pkg = new_package(c, token)
    capture = _capture(c.get(f"/disputes/{DID}/workspace/prepare").text)
    page = c.get(f"/fulfilment/{capture}?from_dispute={DID}&from_package={pkg}")
    assert page.status_code == 200, page.text[:300]
    link = _href(page.text, "Save label or parcel photos and carrier now")
    assert "?" not in link, link
    assert c.get(link).status_code == 200


def test_checklist_link_of_a_sent_latest_version_names_no_package_and_lands(flags):
    c, db, token, _ = flags
    sent = sent_fixture(flags, "Sent statement one.")
    page = c.get(f"/disputes/{DID}/workspace/prepare")
    assert page.status_code == 200
    link = _href(page.text, "Continue to the sale checklist")
    assert f"from_dispute={DID}" in link and "from_package" not in link, link
    landed = c.get(link.split("#")[0])
    assert landed.status_code == 200 and 'id="sale-readiness"' in landed.text
    assert "#sale-readiness" in link
    assert _href(page.text, "Review the same case package") == f"/disputes/{DID}/packages/{sent}"


def test_checklist_link_of_an_editable_latest_version_keeps_the_package(flags):
    c, db, token, _ = flags
    pkg = new_package(c, token)
    page = c.get(f"/disputes/{DID}/workspace/prepare")
    link = _href(page.text, "Continue to the sale checklist")
    assert f"from_package={pkg}" in link, link
    assert c.get(link.split("#")[0]).status_code == 200

