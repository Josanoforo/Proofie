"""The approval page of a version PayPal holds says what WAS sent.
Offline: fake clients, temp database."""
import pytest
from _dom import parse
from _hosted import authenticated_client

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import sha256_json
from copilot.db import repo
from copilot.features.active_approval import service

from .test_guarantees import DID, decisions, ready
from .test_ux_rc41_approval_send import _attach_file


def _seed_confirmed(db, package, review):
    rows = repo.list_attachments(db, package)
    names, shas = [r["filename"] for r in rows], [r["sha256"] for r in rows]
    request = review.request_input
    repo.log_action(db, "system", "send_attempt_registered", DID,
                    {"package_id": package, "approval_sha256": "x" * 64, "request_id": "req-v1",
                     "attachments": names, "attachment_sha256": shas})
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(DID, sha256_json(request))
        store.write(DID, sha256_json(request), {"state": "confirmed", "request_id": "req-v1", "request_input": request,
                                                "attachment_filenames": names, "attachment_sha256": shas})
    finally:
        store.close()


@pytest.mark.usefixtures("web_mode")
def test_gsh4_the_approval_page_of_a_sent_version_with_a_late_file_lists_what_was_sent_not_what_will_be(prepared):
    web, fake, db, package, _ = prepared
    _attach_file(db, package, "label-proof.pdf")
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    _seed_confirmed(db, package, review)
    _attach_file(db, package, "late-proof.pdf")                       # attached after the send
    web = authenticated_client(web.app)
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    dom = parse(html)
    assert dom.find(id="approval").attrs["data-state"] == "sent"
    section = dom.find(id="send-summary")
    text = section.text()
    assert "What was sent" in text and "What will be sent" not in text
    assert "label-proof.pdf" in text and "late-proof.pdf" not in text        # the attempt's files, not the package now
    assert "only when you press" not in html and "Send evidence to PayPal on the package page" not in html
    assert "Shown exactly as PayPal received it." in html and "will receive it" not in html
    exact = section.find(None, data_exact_content="")
    assert exact is not None and exact.attrs.get("translate") == "no"
