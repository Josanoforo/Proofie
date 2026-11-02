"""Approval page: closed field words instead of labels derived with replace('_', ' '), and the product voice of the
page's copy. Synthetic data only."""
import re
from pathlib import Path

import pytest
from _dom import parse
from _hosted import authenticated_client

from copilot.web import field_words

from .test_review import DID, prepared_web  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
REVIEW = ROOT / "copilot" / "web" / "templates" / "features" / "active_approval" / "review.html"


def test_field_read_value_comes_from_the_closed_table(prepared_web):
    web, fake, db, package, doc = prepared_web
    dom = parse(web.get(f"/disputes/{DID}/packages/{package}/active-approval").text)
    cells = dom.find_all("dd", class_="claim-source-field")
    assert cells                                                                   # the statement cites a field
    for cell in cells:
        assert cell.text() == field_words.field_word(cell.attrs["data-field"])
        assert "_" not in cell.text()


def test_capture_date_and_unknown_keys_read_in_the_table_words():
    assert field_words.field_word("capture_date") == "Date the proof was captured"
    assert field_words.field_word("tracking_number") == "Tracking number"
    assert field_words.field_word("account_cliff_fee_estimate_cents") == "Account Cliff fee estimate"
    assert field_words.field_word("25_to_100") == "$25 to $100"
    assert field_words.field_word("auto_parts") == "Auto parts"
    assert field_words.field_word("some_new_key") == "A field Proofie does not name"


def test_the_approval_template_derives_no_label_with_replace_and_has_no_first_person_copy():
    source = REVIEW.read_text(encoding="utf-8")
    for spelling in (".replace('_'", '.replace("_"', "|replace('_'", '|replace("_"'):
        assert spelling not in source, spelling
    assert "|field_word" in source
    assert "our own quality rule" not in source and "Proofie's own quality rule" in source


def test_the_template_renders_the_new_words_through_the_shared_environment():
    from copilot.web.routes_disputes import templates
    for env in (templates.env,):
        text = env.from_string("{{ 'capture_date'|field_word }}|{{ 'x_y'|field_word }}").render()
        assert text == "Date the proof was captured|A field Proofie does not name"


def test_the_approved_page_says_before_the_send_link_that_sending_cannot_be_undone(prepared_web):
    from test_ux_rc41_package import approve_passive  # noqa: F401  (the approval below uses the page's own service)
    from copilot.features.active_approval import service
    from .test_review import choices
    web, fake, db, package, doc = prepared_web
    review = service.read(fake, db, DID, package)
    service.approve(fake, db, DID, package, review.review_sha256, choices(review), "", "")
    html = web.get(f"/disputes/{DID}/packages/{package}/active-approval").text
    dom = parse(html)
    done = dom.find("section", id="approval-done")
    assert done is not None
    text = done.text()
    if "Continue to send evidence" in text:
        assert "Sending to PayPal cannot be undone; Proofie sends it once." in text
        assert text.index("cannot be undone") < text.index("Continue to send evidence")
    assert "Sending to PayPal cannot be undone; Proofie sends it once." in dom.find("main").text() or \
        "Continue to send evidence" not in html
