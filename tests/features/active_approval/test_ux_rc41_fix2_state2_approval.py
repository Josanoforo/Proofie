"""The active approval service and page treat a version PayPal holds statements of
(a confirmed send with a file added afterwards, or a true partial) exactly like the passive approval route does,
and they send the seller on only when PayPal lists provide-evidence. Offline: fake clients, temp database."""
import pytest
from _dom import parse
from _hosted import authenticated_client, set_test_csrf

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json
from copilot.db import repo
from copilot.features.active_approval import service

from .test_guarantees import DID, decisions, ready
from .test_ux_rc41_approval_send import USED_UP, _attach_file


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr("copilot.core.send_recovery.time.sleep", lambda seconds: None)


def _seed(db, package, review, state_name, *, files, reason=None, request_id="req-a"):
    """An attempt of this package as the send path registers it: audit entry + attempt row, files by name and sha256."""
    rows = [r for r in repo.list_attachments(db, package) if r["filename"] in files]
    names, shas = [r["filename"] for r in rows], [r["sha256"] for r in rows]
    repo.log_action(db, "system", "send_attempt_registered", DID,
                    {"package_id": package, "approval_sha256": "x" * 64, "request_id": request_id,
                     "attachments": names, "attachment_sha256": shas})
    store = SQLiteAttemptStore(db)
    try:
        sha = sha256_json(review.request_input)
        assert store.claim(DID, sha)
        store.write(DID, sha, {"state": state_name, "request_id": request_id, "request_input": review.request_input,
                               "attachment_filenames": names, "attachment_sha256": shas,
                               **({"reason": reason} if reason else {})})
    finally:
        store.close()


def _approvals(db, package):
    return [a for a in repo.list_approvals(db, package)]


def test_shf2_a_version_confirmed_with_a_file_added_afterwards_cannot_be_approved_again(prepared):
    web, fake, db, package, _ = prepared
    _attach_file(db, package, "label-proof.pdf")
    review = ready(fake, db, package)
    _seed(db, package, review, "confirmed", files=["label-proof.pdf"])
    _attach_file(db, package, "late.pdf")                                   # attached afterwards, not in PayPal
    review = service.read(fake, db, DID, package)
    before = len(_approvals(db, package))
    with pytest.raises(service.ReviewError) as caught:
        service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    assert str(caught.value) == USED_UP
    web = authenticated_client(web.app)
    set_test_csrf(web, "T")
    form = {"csrf_token": "T", "review_sha256": review.review_sha256,
            **{f"claim-{k}": v for k, v in decisions(review).items()}}
    r = web.post(f"/disputes/{DID}/packages/{package}/active-approval", data=form, follow_redirects=False)
    assert r.status_code in (400, 409) and r.status_code != 303
    assert len(_approvals(db, package)) == before                           # no approval row was written
    dom = parse(web.get(f"/disputes/{DID}/packages/{package}/active-approval").text)
    assert dom.find(id="approval").attrs["data-state"] == "sent"
    assert not [f for f in dom.find_all("form") if f.find("button")]          # no approve control on a sent version


def test_shf2_a_true_partial_version_cannot_be_approved_again(prepared):
    _, fake, db, package, _ = prepared
    _attach_file(db, package, "label-proof.pdf")
    review = ready(fake, db, package)
    _seed(db, package, review, "sent_unconfirmed", files=["label-proof.pdf"], reason="missing_attachment:label-proof.pdf")
    before = len(_approvals(db, package))
    with pytest.raises(service.ReviewError) as caught:
        service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    assert str(caught.value) == USED_UP
    assert len(_approvals(db, package)) == before


def test_shf2_an_attempt_without_a_reason_is_still_approvable_so_the_seller_can_move_on(prepared):
    _, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    _seed(db, package, review, "sent_unconfirmed", files=[])                   # timed out, nothing known yet
    assert service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")


def test_shf8_the_approval_page_links_on_to_the_send_only_when_paypal_lists_provide_evidence(prepared):
    web, fake, db, package, _ = prepared
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    web = authenticated_client(web.app)
    url = f"/disputes/{DID}/packages/{package}/active-approval"
    dom = parse(web.get(url).text)
    links = {a.text(): a.attrs.get("href") for a in dom.find(id="approval-done").find_all("a")}
    assert links.get("Continue to send evidence") == f"/disputes/{DID}/packages/{package}#next-step"
    fake.raw["links"] = [link for link in fake.raw["links"] if link["rel"] != "provide_evidence"]   # PayPal stops asking
    dom = parse(web.get(url).text)
    links = {a.text(): a.attrs.get("href") for a in dom.find(id="approval-done").find_all("a")}
    assert "Continue to send evidence" not in links
    assert links.get("Back to the package") == f"/disputes/{DID}/packages/{package}"


def test_mu9_the_error_summary_link_points_at_an_element_that_is_on_the_page_even_without_a_form(prepared):
    web, fake, db, package, _ = prepared
    _attach_file(db, package, "label-proof.pdf")
    review = ready(fake, db, package)
    _seed(db, package, review, "confirmed", files=["label-proof.pdf"])
    web = authenticated_client(web.app)
    set_test_csrf(web, "T")
    r = web.post(f"/disputes/{DID}/packages/{package}/active-approval",
                 data={"csrf_token": "T", "review_sha256": review.review_sha256})      # a stale submit, nothing ticked
    dom = parse(r.text)
    summary = dom.find("section", class_="error-summary")
    assert summary is not None
    href = summary.find("a").attrs["href"]
    target = dom.find(id=href[1:])
    assert target is not None, href                                         # a form-less page still has the target
    ids = [n.attrs["id"] for n in dom.walk() if "id" in n.attrs]
    assert ids.count(href[1:]) == 1


def test_sh12_the_approval_page_shows_the_audit_send_time_for_a_sent_version(prepared):
    import sqlite3
    from copilot.web import timefmt
    web, fake, db, package, _ = prepared
    _attach_file(db, package, "label-proof.pdf")
    review = ready(fake, db, package)
    _seed(db, package, review, "confirmed", files=["label-proof.pdf"])
    stamp = "2026-03-04T05:06:07+00:00"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE audit_log SET ts=? WHERE action='send_attempt_registered'", (stamp,))
    conn.commit()
    conn.close()
    web = authenticated_client(web.app)
    node = parse(web.get(f"/disputes/{DID}/packages/{package}/active-approval").text).find(id="approval-used")
    assert node is not None and timefmt.display(stamp) in node.text()
