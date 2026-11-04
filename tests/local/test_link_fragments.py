"""LINK-FIX F-04: a link to a place inside a page must land on that place.

A package already recorded as received (a "sent version") has no edit sections, but other pages still link to #shipment-details,
#attachments, #statements and their folds. Those ids are now rendered in the read-only "What was sent" section. This test
follows every link with a #fragment that the case pages draw, fetches its target page and checks that the id exists there.

Expected, written before the first run (it fails on a14ee06f for the sent version: seven ids missing):
- on the sent package page every in-page and cross-page fragment link resolves to an id on its target;
- the same holds on the pages around it (prepare, approve, send, the draft package page); the outcome page needs a feature the
  send tests' app does not register, its links to these ids are covered by the ids test above.
Offline: the send tests' fake PayPal client, synthetic rows.
"""
import re

import pytest

from test_ux_rc41_package import DID, env, page, sent_fixture, new_package  # noqa: F401  (env is a fixture)

pytestmark = [pytest.mark.usefixtures("web_mode")]

SEVEN = ("shipment-fold", "files-fold", "statements-fold", "attachments", "statements", "shipment-details", "field-tracking_number")


def _ids(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


def _fragment_links(html):
    return [h.replace("&amp;", "&") for h in re.findall(r'<a\b[^>]*?href="([^"]*#[^"]+)"', html)]


def _unresolved(c, base_path, html):
    missing, cache = [], {base_path: html}
    for href in dict.fromkeys(_fragment_links(html)):
        target, _, fragment = href.partition("#")
        if target.startswith(("http:", "https:", "mailto:")) or not fragment:
            continue
        path = target or base_path
        if path not in cache:
            response = c.get(path, follow_redirects=True)
            cache[path] = response.text if response.status_code < 400 else ""
        if fragment not in _ids(cache[path]):
            missing.append((base_path, href))
    return missing


def test_the_sent_version_page_carries_the_seven_ids_other_pages_link_to(env):
    c, db, token, _ = env
    pkg = sent_fixture(env, "Sent statement one.")
    html = page(c, pkg).text
    assert 'id="what-was-sent"' in html
    for name in SEVEN:
        assert f'id="{name}"' in html, name
    assert not re.search(r"<textarea|<form[^>]*manual-fields", html)                     # still read-only


def test_every_fragment_link_around_a_sent_package_resolves(env):
    c, db, token, _ = env
    pkg = sent_fixture(env, "Sent statement one.")
    base = f"/disputes/{DID}"
    pages = [f"{base}/packages/{pkg}", f"{base}/workspace/prepare", f"{base}/workspace/approve",
             f"{base}/workspace/send", f"{base}/workspace/review", f"{base}/workspace/records", base]
    missing = []
    for path in pages:
        response = c.get(path)
        assert response.status_code in (200, 400), path
        missing += _unresolved(c, path, response.text)
    assert missing == []


def test_every_fragment_link_around_an_editable_package_resolves(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    base = f"/disputes/{DID}"
    missing = []
    for path in (f"{base}/packages/{pkg}", f"{base}/workspace/prepare", f"{base}/workspace/approve", base):
        response = c.get(path)
        assert response.status_code in (200, 400), path
        missing += _unresolved(c, path, response.text)
    assert missing == []


# ------------------------------------------------------------------------------- ids are unique on every package variant
from test_ux_rc41_package import approved, mark_attempt, repo, routes_disputes  # noqa: E402,F401
from copilot.core.attempt_store import SQLiteAttemptStore  # noqa: E402
from copilot.core.checklist_input import extracted_docs_for  # noqa: E402
from copilot.core.send import build_request_input, pick_tracking, sha256_json  # noqa: E402


def mark_sent_without_text(db, pkg, did=DID):
    """A confirmed send whose exact request text was not kept (the 'did not keep' branch of the sent view)."""
    claims = repo.list_claims(db, pkg)
    tracking, carrier = pick_tracking(extracted_docs_for(db, did))
    sha = sha256_json(build_request_input(claims, tracking, carrier))
    approval = repo.get_latest_valid_approval(db, pkg)
    repo.log_action(db, "system", "send_attempt_registered", did,
                    {"package_id": pkg, "approval_sha256": approval["approval_sha256"] if approval else None,
                     "request_id": "req-demo-1", "attachments": []})
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(did, sha)
        store.write(did, sha, {"state": "confirmed", "request_id": "req-demo-1", "request_input": {}, "attachment_filenames": []})
    finally:
        store.close()


def _duplicates(html):
    seen = {}
    for name in re.findall(r'\bid="([^"]+)"', html):
        seen[name] = seen.get(name, 0) + 1
    return {name: count for name, count in seen.items() if count > 1}


def test_no_id_occurs_twice_on_any_package_variant_and_a_sent_version_never_draws_the_edit_sections(env):
    c, db, token, _ = env
    editable = new_package(c, token)
    ready = approved(c, db, token, "Approved statement.")
    sent = sent_fixture(env, "Sent statement one.")
    unrecorded = approved(c, db, token, "Sent but not kept.")
    mark_sent_without_text(db, unrecorded)
    pages = {"unsent editable": editable, "approved": ready, "sent with recorded text": sent,
             "sent without recorded text": unrecorded}
    for state in ("sent_unconfirmed", "failed", "blocked", "abandoned"):
        pkg = approved(c, db, token, f"Statement for {state}.")
        mark_attempt(db, pkg, state)
        pages[f"attempt {state}"] = pkg
    for label, pkg in pages.items():
        response = page(c, pkg)
        assert response.status_code == 200, label
        html = response.text
        assert _duplicates(html) == {}, (label, _duplicates(html))
        if label == "sent without recorded text":
            assert "Proofie did not keep the exact text of this send." in html       # the fallback branch really rendered
        draws_sent_block = 'id="what-was-sent"' in html
        draws_edit_sections = 'data-state="fold-shipment"' in html or 'id="package-edit-sections"' in html and "Save shipment details" in html
        assert not (draws_sent_block and draws_edit_sections), label
        if draws_sent_block and 'data-state="sent-read-only"' in html:
            for name in SEVEN:
                assert html.count(f'id="{name}"') == 1, (label, name)
