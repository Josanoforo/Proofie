"""The package page, its write routes, the documents routes and the repo helpers.

Offline only: fake PayPal clients, a temp database per test (conftest.py), checks on the DOM and on the stored effect
(rows, audit, attempts), never only on words. The send itself is covered by tests/local/test_routes_send.py; here a
confirmed send is either written to the attempt registry the way the send path writes it, or made by the real send
route against the fake client (test_a_real_send_...).
"""
import hashlib
import json
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from _dom import parse, state
from _hosted import authenticated_client, set_test_csrf
import _ux_lint as lint

from copilot import config
from copilot.app import create_app
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.send import build_request_input, pick_tracking, sha256_json
from copilot.db import repo
from copilot.web import routes_disputes, routes_package
from copilot.web.routes_disputes import get_client

import test_routes_send as send_tests

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "copilot" / "web" / "templates" / "package_review.html"
DID = send_tests.DID
OTHER = "PP-R-DEMO-0000002"
PDF = b"%PDF-1.7\nsynthetic receipt, only the magic header matters here\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24

pytestmark = [pytest.mark.usefixtures("web_mode")]


# --------------------------------------------------------------------------------------------------- helpers
@pytest.fixture
def env(monkeypatch):
    """(client, db, csrf token, fake PayPal client) with every optional feature flag off."""
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    c, db, token, fake = send_tests._client(None, monkeypatch)
    return c, db, token, fake


def new_package(c, token, did=DID):
    r = c.post(f"/disputes/{did}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].split("?")[0].rsplit("/", 1)[-1])


def add_claims(db, pkg, *texts):
    existing = len(repo.list_claims(db, pkg))
    repo.insert_claims(db, pkg, [{"evidence_index": 0, "claim_index": existing + i, "text": t, "source_ids": [],
                                  "basis": "seller_statement"} for i, t in enumerate(texts)])
    return [c["id"] for c in repo.list_claims(db, pkg)][existing:]


def save_shipment(c, token, pkg, tracking="DEMO-FDX-000001", carrier="FEDEX", did=DID):
    r = c.post(f"/disputes/{did}/manual-fields", data={"csrf_token": token, "package_id": pkg,
                                                      "tracking_number": tracking, "carrier": carrier},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def upload(c, token, name="label.pdf", data=PDF, did=DID):
    return c.post(f"/disputes/{did}/documents", files={"file": (name, data, "application/octet-stream")},
                  data={"csrf_token": token}, follow_redirects=False)


def draft(c, db, token, *claims):
    """A manual package with statements and a tracking number: complete enough to review."""
    pkg = new_package(c, token)
    add_claims(db, pkg, *(claims or ("We shipped the parcel with FedEx.",)))
    save_shipment(c, token, pkg)
    return pkg


def approve_passive(c, token, pkg):
    r = c.post(f"/disputes/{DID}/packages/{pkg}/approve",
               data={"csrf_token": token, "sufficiency_override": "Reviewed, ready to send."},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def approved(c, db, token, *claims):
    pkg = draft(c, db, token, *claims)
    approve_passive(c, token, pkg)
    assert repo.get_latest_valid_approval(db, pkg)
    return pkg


def mark_sent(db, pkg, files=(), did=DID):
    """Writes a confirmed attempt for the package's current content, exactly as the send path would have."""
    claims = repo.list_claims(db, pkg)
    tracking, carrier = pick_tracking(extracted_docs_for(db, did))
    req = build_request_input(claims, tracking, carrier)
    sha = sha256_json(req)
    approval = repo.get_latest_valid_approval(db, pkg)
    repo.log_action(db, "system", "send_attempt_registered", did,
                    {"package_id": pkg, "approval_sha256": approval["approval_sha256"] if approval else None,
                     "request_id": "req-demo-1", "attachments": list(files)})
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(did, sha)
        store.write(did, sha, {"state": "confirmed", "request_id": "req-demo-1", "request_input": req,
                               "attachment_filenames": list(files)})
    finally:
        store.close()
    return sha


def mark_attempt(db, pkg, state_name, did=DID, reason=None):
    claims = repo.list_claims(db, pkg)
    tracking, carrier = pick_tracking(extracted_docs_for(db, did))
    req = build_request_input(claims, tracking, carrier)
    sha = sha256_json(req)
    repo.log_action(db, "system", "send_attempt_registered", did,
                    {"package_id": pkg, "approval_sha256": "x", "request_id": "req-demo-2", "attachments": []})
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(did, sha)
        store.write(did, sha, {"state": state_name, "request_id": "req-demo-2", "request_input": req,
                               "attachment_filenames": [], **({"reason": reason} if reason else {})})
    finally:
        store.close()
    return sha


def page(c, pkg, query="", did=DID):
    r = c.get(f"/disputes/{did}/packages/{pkg}{query}")
    assert r.status_code in (200, 400), r.status_code
    return r


def snapshot(db, pkg):
    """Everything a refused write must leave alone."""
    return {"claims": repo.list_claims(db, pkg), "attachments": repo.list_attachments(db, pkg),
            "manual": repo.get_manual_fields(db, DID), "audit": len(repo.list_audit(db, DID)),
            "approval": repo.get_latest_valid_approval(db, pkg), "documents": repo.list_documents(db, DID)}


def location(response):
    parts = urlsplit(response.headers["location"])
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}, parts.fragment


def done_banners(html):
    return parse(html).find_all("div", data_state="done")


SENT_SENTENCE = "This version was sent to PayPal, so it cannot change."


# ------------------------------------------------------------------------------------------------ repo helpers
def test_list_attachments_reports_excluded_and_keeps_the_old_keys(env):
    c, db, token, _ = env
    doc = repo.insert_document(db, DID, "a.pdf", "application/pdf", 10, "sha-a", "x", True)
    pkg = new_package(c, token)
    repo.attach_document(db, pkg, doc)
    before = repo.list_attachments(db, pkg)[0]
    assert before["excluded"] is False
    assert {"document_id", "filename", "content_type", "size_bytes", "sha256"} <= set(before)
    repo.set_document_excluded(db, doc, True)
    after = repo.list_attachments(db, pkg)[0]
    assert after["excluded"] is True and after["document_id"] == doc and after["sha256"] == "sha-a"


def test_delete_claim_is_scoped_to_its_package(env):
    c, db, token, _ = env
    first, second = new_package(c, token), new_package(c, token)
    mine = add_claims(db, first, "One.")[0]
    add_claims(db, second, "Two.")
    assert repo.delete_claim(db, second, mine) is False                  # not that package's claim: nothing deleted
    assert [x["text"] for x in repo.list_claims(db, first)] == ["One."]
    assert repo.delete_claim(db, first, mine) is True
    assert repo.list_claims(db, first) == [] and len(repo.list_claims(db, second)) == 1


# --- 303 to the section, with a done key
def test_every_in_page_write_answers_303_to_its_section_with_a_done_key(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    base = f"/disputes/{DID}/packages/{pkg}"
    r = save_shipment(c, token, pkg)
    assert location(r) == (base, {"done": "shipment_saved"}, "shipment-details")

    r = c.post(f"{base}/claims", data={"csrf_token": token, "text": "First statement."}, follow_redirects=False)
    assert r.status_code == 303 and location(r) == (base, {"done": "statement_added", "n": "1"}, "statements")
    cid = repo.list_claims(db, pkg)[0]["id"]

    r = c.post(f"{base}/claims/{cid}", data={"csrf_token": token, "text": "First, edited."}, follow_redirects=False)
    assert r.status_code == 303
    assert location(r) == (base, {"done": "statement_saved", "claim": str(cid)}, f"claim-{cid}")

    doc = repo.insert_document(db, DID, "a.pdf", "application/pdf", 10, "sha-a", "x", True)
    r = c.post(f"{base}/attach/{doc}", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and location(r) == (base, {"done": "attached", "doc": str(doc)}, "attachments")
    r = c.post(f"{base}/detach/{doc}", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and location(r) == (base, {"done": "detached", "doc": str(doc)}, "attachments")

    r = c.post(f"{base}/claims/{cid}/remove", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and location(r) == (base, {"done": "statement_removed", "n": "1"}, "statements")


def test_confirm_reading_answers_303_to_the_claim_card(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    doc = repo.insert_document(db, DID, "photo.png", "image/png", 10, "sha-p", None, False)
    repo.insert_claims(db, pkg, [{"evidence_index": 0, "claim_index": 0, "text": "The label shows tracking DEMO-7.",
                                  "source_ids": [f"doc_{doc}"], "basis": "model_interpretation",
                                  "suggested_field": "tracking_number", "suggested_value": "DEMO-7"}])
    cid = repo.list_claims(db, pkg)[0]["id"]
    r = c.post(f"/disputes/{DID}/documents/{doc}/confirm-reading",
               data={"csrf_token": token, "field_name": "tracking_number", "value": "DEMO-7", "package_id": str(pkg)},
               follow_redirects=False)
    assert r.status_code == 303
    assert location(r) == (f"/disputes/{DID}/packages/{pkg}", {"done": "reading_confirmed", "claim": str(cid)},
                           f"claim-{cid}")
    assert repo.get_confirmed_readings(db, doc)["tracking_number"]["value"] == "DEMO-7"


def test_exclude_and_include_go_back_to_where_they_came_from(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    doc = repo.insert_document(db, DID, "a.pdf", "application/pdf", 10, "sha-a", "x", True)
    r = c.post(f"/disputes/{DID}/documents/{doc}/exclude", data={"csrf_token": token}, follow_redirects=False)
    assert location(r) == (f"/disputes/{DID}/workspace/prepare", {"done": "document_excluded", "doc": str(doc)}, "documents")
    r = c.post(f"/disputes/{DID}/documents/{doc}/include", data={"csrf_token": token}, follow_redirects=False)
    assert location(r) == (f"/disputes/{DID}/workspace/prepare", {"done": "document_included", "doc": str(doc)}, "documents")
    # from a check row of the package: only the server-built 'package:{pid}' is honoured
    r = c.post(f"/disputes/{DID}/documents/{doc}/exclude", data={"csrf_token": token, "return_to": f"package:{pkg}"},
               follow_redirects=False)
    path, query, _fragment = location(r)
    assert path == f"/disputes/{DID}/packages/{pkg}" and query == {"done": "document_excluded", "doc": str(doc)}
    other_dispute_pkg = new_package(c, token, OTHER)
    for forged in (f"package:{other_dispute_pkg}", "package:999999", "https://evil.example/", "package:abc", "//evil"):
        r = c.post(f"/disputes/{DID}/documents/{doc}/include", data={"csrf_token": token, "return_to": forged},
                   follow_redirects=False)
        assert r.status_code == 303
        assert location(r)[0] == f"/disputes/{DID}/workspace/prepare", forged


def test_cleared_is_appended_only_when_the_write_really_cleared_an_approval(env):
    c, db, token, _ = env
    pkg = approved(c, db, token)
    base = f"/disputes/{DID}/packages/{pkg}"
    r = c.post(f"{base}/claims", data={"csrf_token": token, "text": "A second statement."}, follow_redirects=False)
    path, query, fragment = location(r)
    assert query.get("cleared") == "1" and query["done"] == "statement_added" and query["n"] == "2"
    assert repo.get_latest_valid_approval(db, pkg) is None
    # nothing to clear now: the next write carries no cleared flag
    r = c.post(f"{base}/claims", data={"csrf_token": token, "text": "A third statement."}, follow_redirects=False)
    assert r.status_code == 303
    assert location(r) == (base, {"done": "statement_added", "n": "3"}, "statements")      # exact: no cleared flag, no token


# --- the banner shows only what is stored
def test_a_done_banner_shows_once_and_only_when_the_stored_state_matches(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    base = f"/disputes/{DID}/packages/{pkg}"
    r = c.post(f"{base}/claims", data={"csrf_token": token, "text": "First statement."}, follow_redirects=False)
    follow = r.headers["location"]
    html = c.get(follow).text
    banners = done_banners(html)
    assert len(banners) == 1 and banners[0].attrs["data-done"] == "statement_added"
    assert "Statement 1 added." in banners[0].text()
    assert done_banners(c.get(base).text) == []                           # the GET after shows none
    # forged keys render nothing
    for query in ("?done=statement_saved&claim=999", "?done=statement_added&n=7", "?done=nonsense",
                  "?done=attached&doc=999", "?done=shipment_saved", "?done=statement_removed&n=1",
                  "?done=reading_confirmed&claim=1", "?done=approved"):
        assert done_banners(c.get(base + query).text) == [], query


def test_cleared_suffix_needs_an_invalidated_approval(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    add_claims(db, pkg, "Statement.")
    base = f"/disputes/{DID}/packages/{pkg}"
    save_shipment(c, token, pkg)
    approve_passive(c, token, pkg)
    approval = repo.get_latest_valid_approval(db, pkg)
    html = c.get(f"{base}?done=statement_added&n=1&cleared=1").text                # still approved: nothing was cleared
    banners = done_banners(html)
    assert len(banners) == 1 and "Your earlier approval was cleared" not in banners[0].text()
    add_claims(db, pkg, "Another.")
    repo.invalidate_approval(db, approval["id"])
    html = c.get(f"{base}?done=statement_added&n=2&cleared=1").text
    assert "Your earlier approval was cleared because the content changed. Review and approve again." in \
        done_banners(html)[0].text()


def test_a_done_banner_never_sits_next_to_an_error_summary(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    add_claims(db, pkg, "Statement.")
    r = c.post(f"/disputes/{DID}/packages/{pkg}/claims?done=statement_added&n=1", data={"csrf_token": token, "text": " "})
    assert r.status_code == 400 and "There is a problem" in r.text
    assert done_banners(r.text) == []


def test_document_banners_read_the_document_row(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    doc = repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha-a", "x", True)
    base = f"/disputes/{DID}/packages/{pkg}"
    assert "Document added: label.pdf. Next: attach it to the response." in \
        done_banners(c.get(f"{base}?done=document_added_package&doc={doc}").text)[0].text()
    assert done_banners(c.get(f"{base}?done=attached&doc={doc}").text) == []          # not attached: no claim of it
    repo.attach_document(db, pkg, doc)
    assert "label.pdf is in the response." in done_banners(c.get(f"{base}?done=attached&doc={doc}").text)[0].text()
    assert done_banners(c.get(f"{base}?done=document_excluded&doc={doc}").text) == []  # not excluded
    repo.set_document_excluded(db, doc, True)
    assert "label.pdf is left out of the checks." in \
        done_banners(c.get(f"{base}?done=document_excluded&doc={doc}").text)[0].text()
    other = repo.insert_document(db, OTHER, "theirs.pdf", "application/pdf", 10, "sha-b", "x", True)
    assert done_banners(c.get(f"{base}?done=document_added_package&doc={other}").text) == []   # another dispute's file


def test_approved_banner_needs_a_valid_approval_and_no_send_yet(env):
    c, db, token, _ = env
    pkg = approved(c, db, token)
    base = f"/disputes/{DID}/packages/{pkg}"
    banners = done_banners(c.get(f"{base}?done=approved").text)
    approval = repo.get_latest_valid_approval(db, pkg)
    assert len(banners) == 1 and "Package approved at" in banners[0].text()
    assert "Nothing has been sent. Next: send it to PayPal." in banners[0].text()
    dom = parse(c.get(f"{base}?done=approved").text)
    assert dom.find("time", datetime=lint_iso(approval["approved_at"])) is not None     # the time comes from the row
    mark_sent(db, pkg)
    assert done_banners(c.get(f"{base}?done=approved").text) == []        # something was sent: the sentence would be false


def lint_iso(value):
    from copilot.web import timefmt
    return timefmt.iso(value)


# --- upload and attach
def test_upload_goes_to_the_editable_package_attachments(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    r = upload(c, token)
    assert r.status_code == 303
    doc = repo.list_documents(db, DID)[0]["id"]
    assert location(r) == (f"/disputes/{DID}/packages/{pkg}", {"done": "document_added_package", "doc": str(doc)},
                           "attachments")
    html = c.get(r.headers["location"]).text
    assert len(done_banners(html)) == 1 and done_banners(c.get(f"/disputes/{DID}/packages/{pkg}").text) == []


def test_upload_goes_to_the_dispute_without_an_editable_package(env):
    c, db, token, _ = env
    r = upload(c, token)                                                     # no package yet
    doc = repo.list_documents(db, DID)[0]["id"]
    assert location(r) == (f"/disputes/{DID}/workspace/prepare", {"done": "document_added", "doc": str(doc)}, "documents")
    pkg = approved(c, db, token)
    mark_sent(db, pkg)                                                      # the latest package was confirmed
    r = upload(c, token, "second.pdf")
    assert location(r)[0] == f"/disputes/{DID}/workspace/prepare" and location(r)[1]["done"] == "document_added"
    other = new_package(c, token)                                           # a newer draft is editable again
    r = upload(c, token, "third.pdf")
    assert location(r)[0] == f"/disputes/{DID}/packages/{other}"


@pytest.mark.parametrize("state_name", ["pending", "sent_unconfirmed"])
def test_upload_does_not_go_to_a_package_with_an_unsettled_attempt(env, state_name):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    mark_attempt(db, pkg, state_name)
    r = upload(c, token)
    assert location(r)[0] == f"/disputes/{DID}/workspace/prepare"


def test_attach_and_detach_refuse_what_is_not_in_the_path_dispute(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    foreign_doc = repo.insert_document(db, OTHER, "theirs.pdf", "application/pdf", 10, "sha-x", "x", True)
    mine = repo.insert_document(db, DID, "mine.pdf", "application/pdf", 10, "sha-m", "x", True)
    other_pkg = new_package(c, token, OTHER)
    before = snapshot(db, pkg)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/attach/{foreign_doc}", data={"csrf_token": token})
    assert r.status_code == 404
    r = c.post(f"/disputes/{DID}/packages/{other_pkg}/attach/{mine}", data={"csrf_token": token})
    assert r.status_code == 404
    r = c.post(f"/disputes/{DID}/packages/999999/attach/{mine}", data={"csrf_token": token})
    assert r.status_code == 404
    assert repo.list_attachments(db, pkg) == [] and repo.list_attachments(db, other_pkg) == []
    assert snapshot(db, pkg) == before
    repo.attach_document(db, other_pkg, foreign_doc)
    r = c.post(f"/disputes/{DID}/packages/{other_pkg}/detach/{foreign_doc}", data={"csrf_token": token})
    assert r.status_code == 404
    assert len(repo.list_attachments(db, other_pkg)) == 1                   # not detached through the wrong dispute


# --- ownership and remove
def test_statement_writes_check_that_the_package_and_claim_are_the_callers(env):
    c, db, token, _ = env
    pkg, other = new_package(c, token), new_package(c, token)
    add_claims(db, pkg, "Mine.")
    theirs = add_claims(db, other, "Theirs.")[0]
    before = snapshot(db, other)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/claims/{theirs}", data={"csrf_token": token, "text": "Hijacked."})
    assert r.status_code == 404
    r = c.post(f"/disputes/{DID}/packages/{pkg}/claims/{theirs}/remove", data={"csrf_token": token})
    assert r.status_code == 404
    assert snapshot(db, other) == before and repo.list_claims(db, other)[0]["text"] == "Theirs."
    foreign = new_package(c, token, OTHER)
    r = c.post(f"/disputes/{DID}/packages/{foreign}/claims", data={"csrf_token": token, "text": "Planted."})
    assert r.status_code == 404 and repo.list_claims(db, foreign) == []
    r = c.post(f"/disputes/{DID}/packages/999999/claims", data={"csrf_token": token, "text": "Planted."})
    assert r.status_code == 404


def test_remove_a_statement_from_a_draft_clears_the_approval_and_says_so(env):
    c, db, token, _ = env
    pkg = approved(c, db, token, "First.", "Second.", "Third.")
    claims = repo.list_claims(db, pkg)
    base = f"/disputes/{DID}/packages/{pkg}"
    r = c.post(f"{base}/claims/{claims[1]['id']}/remove", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert location(r) == (base, {"done": "statement_removed", "n": "2", "cleared": "1"}, "statements")
    assert [x["text"] for x in repo.list_claims(db, pkg)] == ["First.", "Third."]
    assert repo.get_latest_valid_approval(db, pkg) is None
    actions = [e["action"] for e in repo.list_audit(db, DID)]
    assert "remove_claim" in actions and "invalidate_approval_after_edit" in actions
    text = done_banners(c.get(r.headers["location"]).text)[0].text()
    assert "Statement 2 removed." in text and "Your earlier approval was cleared because the content changed" in text


def test_remove_without_csrf_is_refused_and_changes_nothing(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    cid = add_claims(db, pkg, "Keep me.")[0]
    r = c.post(f"/disputes/{DID}/packages/{pkg}/claims/{cid}/remove", data={"csrf_token": "forged"})
    assert r.status_code == 403 and len(repo.list_claims(db, pkg)) == 1


# --- a sent version is read-only
def sent_fixture(env, *claims, files=()):
    c, db, token, _ = env
    pkg = approved(c, db, token, *claims)
    mark_sent(db, pkg, files)
    return pkg


def test_every_write_to_a_sent_version_is_refused_with_409_and_changes_nothing(env):
    c, db, token, _ = env
    pkg = sent_fixture(env, "Sent statement.")
    doc = repo.insert_document(db, DID, "late.pdf", "application/pdf", 10, "sha-l", "x", True)
    cid = repo.list_claims(db, pkg)[0]["id"]
    image = repo.insert_document(db, DID, "photo.png", "image/png", 10, "sha-p", None, False)
    base = f"/disputes/{DID}/packages/{pkg}"
    before = snapshot(db, pkg)
    posts = [(f"{base}/claims", {"text": "New."}),
             (f"{base}/claims/{cid}", {"text": "Changed."}),
             (f"{base}/claims/{cid}/remove", {}),
             (f"{base}/attach/{doc}", {}),
             (f"{base}/detach/{doc}", {}),
             (f"/disputes/{DID}/documents/{image}/confirm-reading",
              {"field_name": "carrier", "value": "FEDEX", "package_id": str(pkg)}),
             (f"/disputes/{DID}/manual-fields", {"package_id": str(pkg), "tracking_number": "DEMO-NEW-9",
                                                  "carrier": "UPS"}),
             (f"{base}/approve", {"sufficiency_override": "again"})]
    for url, data in posts:
        r = c.post(url, data={"csrf_token": token, **data}, follow_redirects=False)
        assert r.status_code == 409, (url, r.status_code)
        assert SENT_SENTENCE in r.text, url
    after = snapshot(db, pkg)
    assert after == before                                                   # no row, audit entry or approval changed
    assert repo.get_confirmed_readings(db, image) == {}


def test_a_sent_version_page_shows_what_was_sent_and_no_edit_control(env):
    c, db, token, _ = env
    pkg = sent_fixture(env, "Sent statement one.", "Sent statement two.")
    approval = repo.get_latest_valid_approval(db, pkg)
    html = page(c, pkg).text
    dom = parse(html)
    sent = state(dom, "what-was-sent")
    assert sent is not None and "What was sent" in sent.text()
    assert "Sent statement one." in sent.text() and "DEMO-FDX-000001" in sent.text()
    assert sent.find("h2") is not None and sent.find("h2").text() == "What was sent"
    assert "Sent - read-only." in dom.text() and "This version cannot change." in dom.text()
    assert "Approval used for the send at" in dom.text()
    posting = [f.attrs["action"] for f in dom.find_all("form")]
    for tail in ("/claims", "/attach/", "/detach/", "/confirm-reading", "/manual-fields", "/approve", "/remove", "/send"):
        assert not [a for a in posting if tail in a], (tail, posting)
    assert not dom.find_all("textarea") and approval is not None


def test_what_was_sent_comes_from_the_attempt_not_from_what_the_page_would_send_now(env):
    c, db, token, _ = env
    pkg = sent_fixture(env, "Statement as it was sent.")
    repo.mark_claim_edited(db, repo.list_claims(db, pkg)[0]["id"], "Edited directly in the database afterwards.")
    dom = parse(page(c, pkg).text)
    sent = state(dom, "what-was-sent")
    assert "Statement as it was sent." in sent.text() and "Edited directly" not in sent.text()


def test_prepare_a_new_version_is_offered_only_while_paypal_lists_provide_evidence(env):
    c, db, token, fake = env
    pkg = sent_fixture(env, "Sent.")
    dom = parse(page(c, pkg).text)
    panel = dom.find(id="next-step")
    assert panel.attrs["data-next-step"] == "confirmed"
    assert [f for f in panel.find_all("form") if f.attrs["action"] == f"/disputes/{DID}/packages"]
    fake.detail_body = send_tests.NO_LINK
    dom = parse(page(c, pkg).text)
    assert not [f for f in dom.find(id="next-step").find_all("form") if f.attrs["action"] == f"/disputes/{DID}/packages"]


# --- files and what will be sent, one source
def test_an_attached_file_that_is_left_out_of_the_checks_is_still_listed_as_sent(env):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    upload(c, token, "label.pdf")
    doc = repo.list_documents(db, DID)[0]["id"]
    c.post(f"/disputes/{DID}/packages/{pkg}/attach/{doc}", data={"csrf_token": token}, follow_redirects=False)
    # v1.1.2: the leave-out route also takes the file out of the response; this state (attached and left out) now comes
    # only from a file attached while it is left out, so the test sets it directly
    repo.set_document_excluded(db, doc, True)
    assert repo.get_document(db, doc)["excluded"] is True
    assert [a["filename"] for a in repo.list_attachments(db, pkg)] == ["label.pdf"]     # the send would still carry it
    dom = parse(page(c, pkg).text)
    will_send = dom.find("ul", data_state="will-send")
    assert will_send is not None and "label.pdf" in will_send.text()
    assert dom.find(id="attachments") is not None
    row = next(tr for tr in dom.find("details", id="files-fold").find_all("tr") if "label.pdf" in tr.text())
    assert "In the response, but left out of the checks" in row.text()
    assert "Remove from the response" in row.text()


def test_a_file_added_after_a_confirmed_send_is_not_shown_as_sent(env):
    c, db, token, _ = env
    pkg = sent_fixture(env, "Sent.")
    doc = repo.insert_document(db, DID, "late.pdf", "application/pdf", 10, "sha-l", "x", True)
    repo.attach_document(db, pkg, doc)                                       # past the route guard, as a legacy row would be
    from copilot.web import status
    package = repo.get_package(db, pkg)
    record = status.send_record(db, DID, package)
    assert record.state == "confirmed" and record.attachments_match is False
    summary = routes_package._send_summary(db, DID, package, repo.list_claims(db, pkg),
                                           [d for d in repo.list_documents(db, DID)],
                                           repo.get_latest_valid_approval(db, pkg))
    assert summary["current_confirmation"] is None
    dom = parse(page(c, pkg).text)
    chip = dom.find(data_state="package-chip")
    assert chip is not None and "Sent, a file was added afterwards" in chip.text()          # Its own state
    assert dom.find(id="next-step").attrs["data-next-step"] != "confirmed"
    assert state(dom, "readback-confirmed") is None


# ---- a confirmed package never shows the approval cleared banner ---
def test_a_confirmed_package_never_shows_the_approval_cleared_banner(env):
    c, db, token, _ = env
    pkg = sent_fixture(env, "Sent.")
    approval = repo.get_latest_valid_approval(db, pkg)
    repo.invalidate_approval(db, approval["id"])                             # a source changed after sending
    dom = parse(page(c, pkg).text)
    assert state(dom, "approval-cleared") is None
    assert state(dom, "package-sent") is not None
    assert "Approval used for the send at" in dom.text()
    assert "A source changed after sending; nothing was re-sent." in dom.text()


def test_a_real_send_through_the_route_reaches_the_sent_states(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    c, db, token, fake = send_tests._client(tmp_path, monkeypatch,
                                            post_send_get_bodies=[send_tests._confirmed_body()])
    pkg = send_tests._approved_package_with_tracking(c, db, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    # The POST answers 303 to the stored result page; the result is read from that GET
    location = r.headers.get("location", "")
    assert r.status_code == 303 and re.fullmatch(rf"/disputes/{DID}/packages/{pkg}/result/[^/]+", location), location
    assert len(fake.provide_evidence_calls) == 1
    result = c.get(location)
    assert result.status_code == 200 and state(parse(result.text), "confirmed") is not None
    dom = parse(page(c, pkg).text)
    assert state(dom, "package-sent") is not None
    assert state(dom, "approval-cleared") is None
    assert dom.find(id="next-step").attrs["data-next-step"] == "confirmed"
    assert not [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/send")]
    # the tracker the send itself registered is not a "source change": no checks-after-sending warning about it
    assert "A source changed after sending" not in dom.text()


# ---- passive approval refusal names each blocked statement ---
def test_passive_approval_refusal_names_each_blocked_statement(env):
    c, db, token, _ = env
    pkg = new_package(c, token)
    add_claims(db, pkg, "We shipped the parcel.", "The parcel was delivered to the buyer.")
    save_shipment(c, token, pkg)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/approve", data={"csrf_token": token, "sufficiency_override": "ok"})
    assert r.status_code == 400
    dom = parse(r.text)
    assert "This package cannot be approved yet" in dom.find("title").text()
    assert "Statement 2 says “delivered”, but no document it cites shows that. Change the wording" in r.text
    assert "Statement 1" not in r.text
    words = re.sub(r"<[^>]+>", "", r.text)                                                       # By its words,
    assert "These words claim the parcel arrived: delivered, signed for, received by, entregado, firmado, recibido." in words
    assert r.text.count('<span lang="es" translate="no">') >= 3                                      # the Spanish words marked
    assert repo.get_latest_valid_approval(db, pkg) is None


# ---- a conflict note longer than 500 characters is refused ---
def test_a_conflict_note_longer_than_500_characters_is_refused(env):
    c, db, token, _ = env
    pkg = draft(c, db, token)
    long_note = "x" * 501
    r = c.post(f"/disputes/{DID}/packages/{pkg}/approve",
               data={"csrf_token": token, "sufficiency_override": "ok", "conflict_note": long_note})
    assert r.status_code == 400 and "Note must be 500 characters or fewer" in r.text
    assert repo.get_latest_valid_approval(db, pkg) is None
    assert all(c_["text"] != long_note for c_ in repo.list_claims(db, pkg))     # nothing was written either
    ok = c.post(f"/disputes/{DID}/packages/{pkg}/approve",
                data={"csrf_token": token, "sufficiency_override": "ok", "conflict_note": "y" * 500},
                follow_redirects=False)
    assert ok.status_code == 303 and repo.get_latest_valid_approval(db, pkg) is not None


# ---- upload cases ------------------------------------------
UPLOAD_CASES = {
    "text": ("notes.txt", lambda: b"just text", "The selected file must be a PDF, PNG, JPEG or GIF",
             ("text/plain", "recognised", "extension")),
    "empty": ("empty.pdf", lambda: b"", "The selected file is empty", ("0 bytes",)),
    "big": ("big.pdf", lambda: PDF + b"\x00" * (10 * 1024 * 1024), "The selected file must be smaller than 10 MB",
            ("bytes",)),
}


@pytest.mark.parametrize("case", sorted(UPLOAD_CASES))
def test_upload_refusals_use_the_fixed_messages(env, case):
    name, make, expected, absent = UPLOAD_CASES[case]
    c, db, token, _ = env
    r = upload(c, token, name, make())
    assert r.status_code == 400 and expected in r.text
    for word in absent:
        assert word not in r.text, word
    assert repo.list_documents(db, DID) == []
    dom = parse(r.text)
    link = next(a for a in dom.find_all("a") if "Go back to the dispute" in a.text())
    assert link.attrs["href"] == f"/disputes/{DID}/workspace/prepare#documents"


def test_upload_without_a_selected_file_says_so(env):
    c, db, token, _ = env
    r = c.post(f"/disputes/{DID}/documents", data={"csrf_token": token}, files={"file": ("", b"", "application/octet-stream")},
               follow_redirects=False)
    assert r.status_code == 400 and "Select a document to upload" in r.text
    r = c.post(f"/disputes/{DID}/documents", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 400 and "Select a document to upload" in r.text
    assert repo.list_documents(db, DID) == []


def test_attach_refusals_use_the_fixed_messages(env, monkeypatch):
    from copilot.core import content_type
    c, db, token, _ = env
    pkg = new_package(c, token)
    big_a = repo.insert_document(db, DID, "a.pdf", "application/pdf", 30 * 1024 * 1024, "sha-a", "x", True)
    big_b = repo.insert_document(db, DID, "b.pdf", "application/pdf", 25 * 1024 * 1024, "sha-b", "x", True)
    odd = repo.insert_document(db, DID, "c.txt", "text/plain", 5, "sha-c", "x", True)
    first = c.post(f"/disputes/{DID}/packages/{pkg}/attach/{big_a}", data={"csrf_token": token}, follow_redirects=False)
    assert first.status_code == 303
    r = c.post(f"/disputes/{DID}/packages/{pkg}/attach/{big_b}", data={"csrf_token": token})
    assert r.status_code == 400 and "Files in the response must be 50 MB or less in total. Remove a file first." in r.text
    assert "bytes" not in r.text
    r = c.post(f"/disputes/{DID}/packages/{pkg}/attach/{odd}", data={"csrf_token": token})
    assert r.status_code == 400 and "text/plain" not in r.text and "PDF, PNG, JPEG or GIF" in r.text
    assert [a["filename"] for a in repo.list_attachments(db, pkg)] == ["a.pdf"]
