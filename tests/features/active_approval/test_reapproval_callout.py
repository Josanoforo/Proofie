"""N2 surface 2: one status callout on the review page after an edit cleared an approval. Synthetic rows only.

Expected, written before the first run:
- no earlier approval and an unsupported delivery statement: the usual "Cannot approve yet" panel, no re-review callout;
- an approval cleared by an edit and an unsupported delivery statement: ONE callout (data-reapproval="blocked") with the
  words: approval of package v1 was cleared by the edit, what the source shows (Statement 2 says "delivered", no cited
  document shows that), approval of the edited text is blocked, what to do; no "Cannot approve yet" panel next to it and no
  Approve button;
- the same approval cleared and no delivery statement left: ONE callout (data-reapproval="fixed") saying the earlier
  approval is not restored and a new review against the sources is required; the Approve button is back;
- an approval cleared for another reason (no edit entry), or a valid approval after it: no callout.
"""
import re

import pytest
from _hosted import authenticated_client

from copilot.db import repo
from copilot.features.active_approval import reapproval
from .test_ux_rc41_approval_send import _delivered_statement
from .test_guarantees import DID


def _cleared_by_edit(db, package, *, edit_entry=True):
    approval = repo.insert_approval(db, package, "a" * 64, "vendor")
    repo.invalidate_approval(db, approval)
    if edit_entry:
        repo.log_action(db, "system", "invalidate_approval_after_edit", DID,
                        {"package_id": package, "approval_id": approval})
    return approval


def _page(prepared):
    web, fake, db, package, _ = prepared
    web = authenticated_client(web.app)
    return web.get(f"/disputes/{DID}/packages/{package}/active-approval").text


def test_without_an_earlier_approval_the_usual_panel_stays(prepared):
    _delivered_statement(prepared[2], prepared[3])
    html = _page(prepared)
    assert "Cannot approve yet" in html and "data-reapproval" not in html


def test_blocked_after_an_edit_is_one_red_callout_with_the_four_facts(prepared):
    web, fake, db, package, _ = prepared
    _delivered_statement(db, package)
    _cleared_by_edit(db, package)
    html = _page(prepared)
    assert html.count('data-reapproval="blocked"') == 1 and "data-reapproval=\"fixed\"" not in html
    assert "Cannot approve yet" not in html and html.count('id="approval-block-reasons"') == 1
    block = re.search(r"<section class=\"error-summary reapproval-callout\".*?</section>", html, re.S).group(0)
    assert "tag--bad" in block and "Approval blocked" in block
    assert "was cleared when you edited the package" in block
    assert "Statement 2 says" in block and "no document it cites shows that" in block
    assert "Approving the edited text is blocked" in block and "remove or fix the statement, then review again" in block
    assert "Approve this package" not in html


def test_after_the_fix_the_same_place_says_the_approval_is_not_restored(prepared):
    web, fake, db, package, _ = prepared
    _cleared_by_edit(db, package)
    html = _page(prepared)
    assert html.count('data-reapproval="fixed"') == 1 and 'data-reapproval="blocked"' not in html
    assert "is not restored" in html and "A new review against the sources is required" in html
    assert "Approve this package" in html


def test_no_callout_when_the_approval_was_not_cleared_by_an_edit(prepared):
    web, fake, db, package, _ = prepared
    _cleared_by_edit(db, package, edit_entry=False)
    assert "data-reapproval" not in _page(prepared)


def test_earlier_is_none_once_a_valid_approval_follows(prepared):
    web, fake, db, package, _ = prepared
    _cleared_by_edit(db, package)
    assert reapproval.earlier(db, DID, package)["approved_at"]
    repo.insert_approval(db, package, "b" * 64, "vendor")
    assert reapproval.earlier(db, DID, package) is None
