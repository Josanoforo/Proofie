"""The draft page meets the resolution flow.

g: the draft page's hand-off form posts to the resolution preview, which answers 303 to its separate check page; the
   check page shows exactly the text the draft page's form carried, and nothing reaches PayPal.
h: the fixed template (resolution) writes the order date in words and the carrier's display name; the draft validator
   accepts exactly those words for the same facts and refuses every other spelled-out date as before.

Fake PayPal, fake model and a throwaway database only."""
import html
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests" / "local"))
from _dom import parse  # noqa: E402

from copilot.features.draft import check  # noqa: E402
from copilot.features.resolution import actions as act  # noqa: E402
from draft_fakes import DISPUTE_ID, EN_OK, ES_OK, NORM, TRACK, TXN, sheet  # noqa: E402
from draft_fakes import _no_httpx_calls  # noqa: E402,F401  (autouse: no httpx call in these tests)
from test_draft_routes import PAGE, _reset, _web  # noqa: E402

pytestmark = [pytest.mark.usefixtures("web_mode")]


# ------------------------------------------------------------------------------------------------------- item g
@pytest.mark.local_spec
def test_g_the_draft_hand_off_reaches_the_resolution_check_page_with_the_exact_text(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    web, fake, _call = _web(monkeypatch)
    page = web.get(PAGE).text
    form = re.search(r'<form method="post" action="([^"]+)" novalidate data-state="handoff-form">(.*?)</form>', page, re.S)
    assert form.group(1) == f"/disputes/{DISPUTE_ID}/resolution/preview#preview"
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(2)))
    text = html.unescape(re.search(r'<textarea name="text"[^>]*>(.*?)</textarea>', form.group(2), re.S).group(1))
    assert fields["action"] == "message" and text.startswith("Hello, this is about your order of 28 September 2026")
    # the browser posts to the form's action (the fragment is not sent) with the page's own cookie and token
    preview = web.post(form.group(1).split("#")[0], data={**fields, "text": text}, follow_redirects=False)
    location = preview.headers.get("location", "")
    assert preview.status_code == 303 and re.fullmatch(
        rf"/disputes/{DISPUTE_ID}/resolution/check/[A-Za-z0-9_-]+#preview", location), location
    _reset(web)
    check_page = web.get(location.split("#")[0])
    assert check_page.status_code == 200
    dom = parse(check_page.text)
    exact = dom.find("div", class_="exact-content")
    assert exact is not None and html.unescape(exact.text()) == text                 # exactly what the form carried
    assert 'data-state="confirm-form"' in check_page.text
    assert re.search(r'data-state="request-sha256">[0-9a-f]{64}<', check_page.text)
    assert not [c for c in fake.calls if c[0] != "GET"]                                # nothing sent: approval comes next


# ------------------------------------------------------------------------------------------------------- item h
def test_h_the_fixed_template_passes_the_draft_validator_with_its_own_words():
    template = act.message_text(NORM, TXN, TRACK)
    assert "28 September 2026" in template and "with FedEx" in template
    verdict = check.validate_draft(sheet(), template, ES_OK)
    assert verdict.ok, verdict.problems


def test_h_the_order_date_in_the_templates_words_is_the_same_fact_in_a_draft():
    en = EN_OK.replace("2026-09-28", "28 September 2026")
    assert check.validate_draft(sheet(), en, ES_OK).ok


def test_h_every_other_spelled_date_is_still_refused_and_claims_are_still_checked():
    s = sheet()
    for written in ("29 September 2026", "September 28, 2026", "28 september 2026", "28 Sep 2026"):
        verdict = check.validate_draft(s, EN_OK.replace("2026-09-28", written), ES_OK)
        assert not verdict.ok and any("spelled-out date" in p for p in verdict.problems), written
    spanish = check.validate_draft(s, EN_OK, ES_OK.replace("2026-09-28", "28 de septiembre de 2026"))
    assert not spanish.ok and any("spelled-out date" in p for p in spanish.problems)
    claim = check.validate_draft(s, EN_OK.replace("2026-09-28", "28 September 2026") + " It was delivered.", ES_OK)
    assert any("says delivered" in p for p in claim.problems)
    other_estimate = check.validate_draft(s, EN_OK.replace("2026-10-10", "10 October 2026"), ES_OK)
    assert not other_estimate.ok                                     # only the template's own words: the order date
