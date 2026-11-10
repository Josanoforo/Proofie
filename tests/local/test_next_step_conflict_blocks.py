"""v1.1.2: a check that does not match is named as the next step of the package page even when the package has no
statement or no tracking number yet (found by table 2: the model's draft left no statement and the page only asked for one)."""
from copilot.web import next_step
from copilot.web.status import SendRecord

LINE = "This answer cannot be sent while the receipt does not match PayPal's data."


def _step(**kw):
    base = dict(dispute_id="PP-R-TEST-0001", package={"id": 7, "version": 1}, norm={}, claims_count=0, files_count=1,
                has_tracking=True, carrier_missing=False, approval_valid=False, record=None, other_unconfirmed=False)
    return next_step.for_package(**{**base, **kw})


def test_no_statement_and_a_conflict_names_the_conflict():
    step = _step(conflict_line=LINE)
    assert step.key == "conflict_blocks" and step.body == LINE and step.action.href == "#checks"


def test_no_tracking_and_a_conflict_names_the_conflict():
    assert _step(conflict_line=LINE, has_tracking=False, claims_count=2).key == "conflict_blocks"


def test_without_a_conflict_the_steps_are_the_old_ones():
    assert _step().key == "no_statement" and _step(has_tracking=False).key == "no_tracking"


def test_with_statements_and_tracking_the_step_still_leads_to_the_review_where_the_block_shows():
    assert _step(conflict_line=LINE, claims_count=2).key in ("review_approve", "approve_here")
