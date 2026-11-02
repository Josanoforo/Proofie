"""The active-approval side: the partial outcome while sources change, error links, the translate scope and the time of
the send on the approval page. Offline: fake clients, temp database."""
import pytest
from _dom import parse
from _hosted import authenticated_client, set_test_csrf

from copilot.core.send_recovery import partial_message
from copilot.core.send_service import send_package
from copilot.db import repo
from copilot.features.active_approval import service
from copilot.web.routes_disputes import get_client

from .test_guarantees import DID, decisions, ready
from .test_send_once_and_honest_status import PayPalAfterSend
from .test_ux_rc41_approval_send import _attach_file


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr("copilot.core.send_recovery.time.sleep", lambda seconds: None)


def test_sh14_statements_listed_but_file_missing_while_a_source_changed_is_the_partial_outcome(prepared):
    _, fake, db, package, _ = prepared
    _attach_file(db, package, "label-proof.pdf")
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))     # a tracker appears during the send
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1                                                         # one POST, never a second
    assert result.http_status == 206 and result.title == partial_message(["label-proof.pdf"]) and result.sent is False
    assert result.note and result.note.startswith("Changed after sending:") and result.note.endswith("nothing was re-sent.")
    assert "Sent, review required" not in result.title


def test_mu9_the_approval_error_links_to_the_first_unticked_checkbox_and_that_checkbox_names_the_error(prepared):
    web, fake, db, package, _ = prepared
    web = authenticated_client(web.app)
    set_test_csrf(web, "T")
    review = service.read(fake, db, DID, package)
    r = web.post(f"/disputes/{DID}/packages/{package}/active-approval",
                 data={"csrf_token": "T", "review_sha256": review.review_sha256})
    assert r.status_code == 400
    dom = parse(r.text)
    summary = dom.find("section", class_="error-summary")
    href = summary.find("a").attrs["href"]
    target = dom.find(id=href[1:])
    assert target is not None and target.tag == "input" and target.attrs.get("type") == "checkbox"
    assert target.attrs.get("aria-invalid") == "true"
    described = target.attrs.get("aria-describedby")
    assert described and dom.find(id=described) is not None and "Tick statement 1" in dom.find(id=described).text()


def test_mu7_only_the_literal_values_of_what_will_be_sent_are_untranslatable(prepared):
    web, fake, db, package, _ = prepared
    web = authenticated_client(web.app)
    review = service.read(fake, db, DID, package)
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    dom = parse(html)
    section = dom.find(id="send-summary")
    node = section
    while node is not None:
        assert node.attrs.get("translate") != "no", node.tag                       # not the section, not an ancestor
        node = node.parent
    frozen = [n for n in section.walk() if n.attrs.get("translate") == "no"]
    assert any("data-exact-content" in n.attrs for n in frozen)                    # the exact notes text
    assert not [n for n in frozen if n.tag in ("h2", "dt")]                         # headings and labels translate
    assert review.request_input


def test_sh14_two_attachments_missing_while_a_source_changed_is_the_partial_outcome_in_the_plural(prepared):
    """The two-file twin of the partial-outcome case: one POST, the plural partial headline, still a partial and never a success."""
    from copilot.core.send_recovery import PARTIAL_MESSAGE_MANY
    _, fake, db, package, _ = prepared
    _attach_file(db, package, "label-proof.pdf")
    _attach_file(db, package, "label-proof-2.pdf")
    review = ready(fake, db, package)
    service.approve(fake, db, DID, package, review.review_sha256, decisions(review), "", "")
    sender = PayPalAfterSend(fake, review, trackers_after=("SOMEONE-ELSE-77",))
    result = send_package(sender, db, DID, package)
    assert sender.posts == 1
    assert result.http_status == 206 and result.title == PARTIAL_MESSAGE_MANY and result.sent is False
    assert result.note and result.note.startswith("Changed after sending:")
