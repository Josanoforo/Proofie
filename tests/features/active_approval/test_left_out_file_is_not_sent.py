"""v1.1.2: a file the seller left out of the checks cannot ride along to PayPal. While it is attached, approval and sending
stop; she removes it, attaches the right one, approves, and the files to send do not include the wrong one.
On v1.1.1 the first assertion of the walk fails: the approval goes through with the left-out receipt still attached."""
import pytest

from copilot.core import send_service
from copilot.db import repo
from copilot.features.active_approval import service

DID = "PP-R-DEMO-0000001"


def _choices(review):
    return {c.key: "seller_statement" if c.verdict == "unverifiable" else "confirmed" for c in review.claims}


def test_wrong_receipt_left_out_then_replaced_never_reaches_the_files_to_send(prepared):
    _, fake, db, package, _ = prepared
    wrong = repo.insert_document(db, DID, "wrong-receipt.pdf", "application/pdf", 10, "sha-wrong", "x", True)
    repo.attach_document(db, package, wrong)
    repo.set_document_excluded(db, wrong, True)                       # "Leave ... out of the checks"
    review = service.read(fake, db, DID, package)
    assert send_service.LEFT_OUT_ATTACHED_BLOCK in review.blocked     # v1.1.1: not blocked, the approval below succeeds
    with pytest.raises(service.ReviewError, match="left out of the checks is still attached"):
        service.approve(fake, db, DID, package, review.review_sha256, _choices(review), "", "")
    assert repo.get_latest_valid_approval(db, package) is None
    repo.detach_document(db, package, wrong)                          # she removes it from the package
    right = repo.insert_document(db, DID, "right-receipt.pdf", "application/pdf", 10, "sha-right", "y", True)
    repo.attach_document(db, package, right)
    review = service.read(fake, db, DID, package)
    assert send_service.LEFT_OUT_ATTACHED_BLOCK not in review.blocked
    assert service.approve(fake, db, DID, package, review.review_sha256, _choices(review), "", "")
    files = [a["filename"] for a in repo.list_attachments(db, package)]
    assert files == ["right-receipt.pdf"] and "wrong-receipt.pdf" not in files


def test_the_approval_page_names_the_block_and_links_to_the_attachments(prepared_hosted_access):
    web, fake, db, package, _ = prepared_hosted_access
    wrong = repo.insert_document(db, DID, "wrong-receipt.pdf", "application/pdf", 10, "sha-wrong", "x", True)
    repo.attach_document(db, package, wrong)
    repo.set_document_excluded(db, wrong, True)
    text = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    assert "Cannot approve yet" in text and "left out of the checks is still attached" in text
    assert f'/disputes/{DID}/packages/{package}#attachments' in text and "Remove the file" in text


def test_a_file_left_out_after_the_approval_stops_the_send(prepared):
    _, fake, db, package, _ = prepared
    review = service.read(fake, db, DID, package)
    approval = service.approve(fake, db, DID, package, review.review_sha256, _choices(review), "", "")
    doc = repo.insert_document(db, DID, "late.pdf", "application/pdf", 10, "sha-late", "x", True)
    repo.attach_document(db, package, doc)
    repo.set_document_excluded(db, doc, True)
    with pytest.raises(service.ReviewError):
        service.send_guard(fake, db, DID, package, approval)


def test_plain_route_on_one_package_leave_out_then_attach_the_right_one_sends_one_file(prepared):
    """The route a seller takes without a second package (found by table 2's walker): the wrong receipt is attached, she
    leaves it out of the checks from the page, attaches the right one, approves; the files to send are one file.
    Leaving out is what takes it out of the response: no second package, no closed fold to find."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "local"))
    from _hosted import set_test_csrf
    web, fake, db, package, _ = prepared
    set_test_csrf(web, "DEMO-CSRF")
    form = {"csrf_token": "DEMO-CSRF"}
    wrong = repo.insert_document(db, DID, "wrong-receipt.pdf", "application/pdf", 10, "sha-wrong", "x", True)
    repo.attach_document(db, package, wrong)
    assert [a["filename"] for a in repo.list_attachments(db, package)] == ["wrong-receipt.pdf"]
    r = web.post(f"/disputes/{DID}/documents/{wrong}/exclude", data={**form, "return_to": f"package:{package}"},
                 follow_redirects=False)
    assert r.status_code == 303
    assert repo.list_attachments(db, package) == []                    # left out of the checks = out of the response
    page = web.get(r.headers["location"]).text
    assert "is left out of the checks. It is not sent to PayPal either" in page
    right = repo.insert_document(db, DID, "right-receipt.pdf", "application/pdf", 10, "sha-right", "y", True)
    repo.attach_document(db, package, right)
    review = service.read(fake, db, DID, package)
    assert send_service.LEFT_OUT_ATTACHED_BLOCK not in review.blocked
    assert service.approve(fake, db, DID, package, review.review_sha256, _choices(review), "", "")
    assert [a["filename"] for a in repo.list_attachments(db, package)] == ["right-receipt.pdf"]
    text = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    assert "right-receipt.pdf" in text and "wrong-receipt.pdf" not in text


def test_leaving_a_file_out_clears_the_approval_of_the_response_it_was_part_of(prepared):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "local"))
    from _hosted import set_test_csrf
    web, fake, db, package, doc = prepared
    late = repo.insert_document(db, DID, "late.pdf", "application/pdf", 10, "sha-late", "x", True)
    repo.attach_document(db, package, late)
    review = service.read(fake, db, DID, package)
    assert service.approve(fake, db, DID, package, review.review_sha256, _choices(review), "", "")
    set_test_csrf(web, "DEMO-CSRF")
    r = web.post(f"/disputes/{DID}/documents/{late}/exclude", data={"csrf_token": "DEMO-CSRF"}, follow_redirects=False)
    assert r.status_code == 303 and repo.list_attachments(db, package) == []
    assert repo.get_latest_valid_approval(db, package) is None
