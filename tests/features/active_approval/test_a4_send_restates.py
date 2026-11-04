"""HERO (A4-5, A4-4): the send step restates exactly what PayPal will receive, and the approval page frames it.
Offline: fake PayPal client, temp database per test (conftest.py). Expected results are in
data/kits/Proofie-Kits/logs-RC44/D101/hero/expected.md (H1, H2)."""
import html
import re

import pytest

import os

from copilot.db import repo
from copilot.features.active_approval import service as approval_service
import test_routes_send as send_tests
from test_ux_rc41_package import DID, approve_passive, draft, env, page  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


@pytest.fixture
def env_active(monkeypatch):
    """Like env, but the approval review page exists (the flag is read when the app is created)."""
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    monkeypatch.setenv("COPILOT_FEATURE_SELLER_SHORTCUTS", "true")          # the one-control approval: its label names the count
    return send_tests._client(None, monkeypatch)


def _exact(text: str, start: str = "") -> str:
    """The text inside the first data-exact-content box at or after `start`."""
    at = text.index(start) if start else 0
    m = re.search(r"data-exact-content>(.*?)</div>", text[at:], re.S)
    assert m, "no exact-content box"
    return html.unescape(m.group(1)).strip()


def _panel(text: str) -> str:
    start = text.index('id="next-step"')
    return text[start:text.index("</section>", start)]


def test_send_panel_restates_the_approved_package_in_order(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, "We shipped the parcel with FedEx.")
    approve_passive(c, token, pkg)
    panel = _panel(page(c, pkg).text)
    order = ["You are about to send this to the PayPal sandbox", "data-exact-content", "Tracking", "Files",
             "Fingerprint of the approved package", "Send evidence to PayPal", "Edit the package instead", "Editing clears your approval."]
    at = [panel.index(s) for s in order]
    assert at == sorted(at), dict(zip(order, at))
    assert f"dispute {DID}" in panel and "DEMO-FDX-000001" in panel and "SHA-256" in panel
    approval = repo.get_latest_valid_approval(db, pkg)
    assert approval["approval_sha256"][:12] + "…" in panel
    assert panel.count("btn-primary") == 1 and "Cannot be undone" not in panel


def test_panel_text_equals_the_approval_page_send_summary(env_active):
    c, db, token, fake = env_active
    pkg = draft(c, db, token, "We shipped the parcel with FedEx.", "The tracking shows delivery.")
    review = approval_service.read(fake, db, DID, pkg)
    choices = {k.key: "seller_statement" if k.verdict == "unverifiable" else "confirmed" for k in review.claims}
    approval_service.approve(fake, db, DID, pkg, review.review_sha256, choices, "", "")
    panel_text = _exact(page(c, pkg).text, 'id="next-step"')
    approval_page = c.get(f"/disputes/{DID}/packages/{pkg}/active-approval")
    assert approval_page.status_code == 200
    summary = approval_page.text[approval_page.text.index('id="send-summary"'):]
    assert _exact(summary) == panel_text and panel_text


def test_no_approval_means_no_exact_text_and_no_send_button(env):
    c, db, token, fake = env
    pkg = draft(c, db, token)
    panel = _panel(page(c, pkg).text)
    assert "You are about to send" not in panel and "data-exact-content" not in panel and f"/packages/{pkg}/send" not in panel


def test_the_approval_page_is_framed_and_names_one_statement_in_the_singular(env_active):
    c, db, token, fake = env_active
    pkg = draft(c, db, token, "We shipped the parcel with FedEx.")
    text = c.get(f"/disputes/{DID}/packages/{pkg}/active-approval").text
    assert "Check what will be sent, then approve it" in text and "Human approval review" not in text
    assert 'data-state="approval-frame"' in text and "Nothing is sent yet" in text
    assert f"PayPal sandbox, dispute {DID}, package v" in text
    assert "Approve this package: I have read the statement<" in text and "these 1" not in text


def test_the_approval_page_keeps_the_plural_for_several_statements(env_active):
    c, db, token, fake = env_active
    pkg = draft(c, db, token, "We shipped the parcel with FedEx.", "The tracking shows delivery.")
    text = c.get(f"/disputes/{DID}/packages/{pkg}/active-approval").text
    assert "I have read these 2 statements<" in text


def test_the_sent_package_page_has_no_stale_banner_and_the_new_wording(env):
    c, db, token, fake = env
    pkg = draft(c, db, token)
    approve_passive(c, token, pkg)
    before = page(c, pkg).text
    assert "your note on the open notices" in before and "sent anyway" not in before


def _sent(env):
    from test_ux_rc41_fix_state import STATEMENT, add_file, confirmed_body, LABEL_A
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    fake.post_send_get_bodies = [confirmed_body(db, pkg, ["label.pdf"])] * 12          # every later read of the dispute shows the same listed evidence
    r = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c, pkg


def _task_html(status, label):
    """Renders the task_state macro of case_task.html (the template extends the case base, so its macro text is lifted out)."""
    from pathlib import Path
    from copilot.web.routes_disputes import templates
    src = (Path(templates.env.loader.searchpath[0]) / "case_task.html").read_text(encoding="utf-8")
    macro = src[src.index("{% macro task_state"):src.index("{%- endmacro %}", src.index("{% macro task_state")) + len("{%- endmacro %}")]
    row = {"key": "approve", "status": status, "blocked": False, "label": label, "kind": "ok", "mark": "Required to send", "href": "/x",
           "blocker": ("", "", "")}
    template = ('{% from "_components.html" import tag, link_button %}' + macro +
                '{% call task_state("approve", "Approval", None, "View what was approved") %}Open package approval review{% endcall %}')
    return templates.env.from_string(template).render(tasks=[row])


def test_a_done_task_row_has_no_required_to_send_and_a_secondary_button():
    text = _task_html("done", "Done: package v1 approved")
    assert "Done:" in text and "Required to send" not in text
    assert "btn-secondary" in text and "btn-primary" not in text and "View what was approved" in text


def test_a_task_row_not_done_is_unchanged():
    text = _task_html("not_started", "Not started")
    assert "Required to send" in text and "btn-primary" in text and "Open package approval review" in text and "View what was approved" not in text


def test_the_sent_package_page_has_no_before_you_send_banner(env):
    c, pkg = _sent(env)
    text = page(c, pkg).text
    assert 'data-state="review-flags"' not in text and "sent anyway" not in text
    assert "your note on the open notices" in text and 'href="#checks"' in text


def test_the_approved_receipt_shows_the_fingerprint(env_active):
    c, db, token, fake = env_active
    pkg = draft(c, db, token, "We shipped the parcel with FedEx.")
    review = approval_service.read(fake, db, DID, pkg)
    approval_service.approve(fake, db, DID, pkg, review.review_sha256,
                             {k.key: "seller_statement" if k.verdict == "unverifiable" else "confirmed" for k in review.claims}, "", "")
    text = c.get(f"/disputes/{DID}/packages/{pkg}/active-approval").text
    sha = repo.get_latest_valid_approval(db, pkg)["approval_sha256"]
    assert f'Fingerprint of what you approved: <span translate="no">{sha[:12]}</span>' in text and "any edit clears the approval" in text
    assert "Nothing is sent yet" not in text                    # the frame belongs to the unapproved page only
