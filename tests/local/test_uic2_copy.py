"""The field hints and the two selects, the session-expired text, the no-rule-set line.
(The hosted privacy line has its tests in test_hosted_demo_sentence.py; the draft button's are under tests/features/draft/.)"""
from _hosted import authenticated_client, reset_csrf
from _dom import parse
import html
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot import config
from copilot.core import checklist, checklist_input
from copilot.core.checklist import ExtractedDoc, evaluate, load_rules
from copilot.db import repo
from test_uic1_copy import DID, WAITING, Fake, _package_page, _text, _web

_spec = importlib.util.spec_from_file_location("m1_loader", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)

SIG_VALUES = ["online", "paper_only", "aggregator_portal", "none"]       # what checklist._signature compares (via _SIG)
TYPE_VALUES = ["home", "branch_pickup"]                                  # checklist docstring vocabulary; _branch compares branch_pickup


def _options(page, name):
    block = re.search(rf'<select id="field-{name}".*?</select>', page.text, flags=re.S).group(0)
    return re.findall(r'<option value="([^"]*)"([^>]*)>([^<]*)</option>', block)


# ---- final hints, verbatim ---------------------------------------------------------------------------------
FINAL_HINTS = {
    "signature": "Can the signature be viewed online on the carrier's site? Some countries' rules ask for it above a limit; check the signature row below.",
    "delivery_type": "Choose branch pickup if the parcel was collected at a carrier branch. This is Proofie's own risk flag, not a PayPal rule.",
    "capture_date": "The day you saved or photographed the proof, not the delivery date.",
}


def test_the_three_final_hints_are_verbatim_with_aria_describedby():
    _, r = _package_page()
    t = _text(r)
    for name, hint in FINAL_HINTS.items():
        assert f'class="field-hint" id="hint-{name}"' in r.text and hint in t
        assert f'id="field-{name}"' in r.text and f'aria-describedby="hint-{name}"' in r.text
    assert 'For example "delivered". "In transit" is shown as in transit, is not shown as delivered.' in t   # delivery_status unchanged


def test_signature_and_delivery_type_are_selects_with_the_empty_not_stated_option_first():
    _, r = _package_page()
    assert '<input type="text" id="field-signature"' not in r.text and '<input type="text" id="field-delivery_type"' not in r.text
    sig, typ = _options(r, "signature"), _options(r, "delivery_type")
    assert [(v, t) for v, _, t in sig][0] == ("", "Not stated") and [(v, t) for v, _, t in typ][0] == ("", "Not stated")
    assert [v for v, _, _ in sig][1:] == SIG_VALUES and [v for v, _, _ in typ][1:] == TYPE_VALUES
    assert 'selected' in sig[0][1] and 'selected' in typ[0][1]                   # nothing stored: "Not stated" is selected
    assert sig[1][2] == "Viewable online"                               # plain word first, stored value in brackets
    assert [t for _, _, t in sig[1:] + typ[1:]] == [                               # the `none` text follows the check
        "Viewable online", "Paper only", "Tracking portal only",
        "Not viewable online", "Home delivery", "Branch pickup"]
    for v, _, text in sig[1:] + typ[1:]:
        assert v in SIG_VALUES + TYPE_VALUES and not text.endswith(f"({v})")
    assert 'capture_date' in r.text and 'id="field-capture_date"' in r.text
    assert 'name="capture_date"' in r.text and 'type="date"' in r.text


def test_the_select_values_are_exactly_what_the_checks_read():
    assert set(SIG_VALUES) <= set(checklist._SIG)                                # every option maps to a viewability kind
    assert {checklist._SIG[v] for v in SIG_VALUES} == {"online", "paper", "aggregator", "none"}
    src = Path(checklist.__file__).read_text(encoding="utf-8")
    assert 'casefold() == "branch_pickup"' in src                                # _branch / _destination compare this value


def test_a_stored_value_is_the_selected_option_and_a_legacy_free_text_value_is_kept_visible():
    _, r = _package_page(manual={"signature": "online", "delivery_type": "branch_pickup"})
    assert [v for v, a, _ in _options(r, "signature") if "selected" in a] == ["online"]
    assert [v for v, a, _ in _options(r, "delivery_type") if "selected" in a] == ["branch_pickup"]
    _, legacy = _package_page(manual={"signature": "yes, signed"})
    assert [v for v, a, _ in _options(legacy, "signature") if "selected" in a] == ["yes, signed"]   # not silently dropped


# ---- round trip through the manual-fields path ------------------------------------------------------------------
def _post_fields(c, token, **fields):
    data = {"csrf_token": token, "package_id": "1", **fields}
    r = c.post(f"/disputes/{DID}/manual-fields", data=data, follow_redirects=False)
    assert r.status_code == 303
    return repo.get_manual_fields(config.db_path(), DID)


def test_a_chosen_value_is_stored_unchanged_and_an_empty_selection_stores_nothing():
    c, token = _web(Fake(detail=WAITING))
    stored = _post_fields(c, token, signature="online", delivery_type="branch_pickup", city="")
    assert stored["signature"]["value"] == "online" and stored["delivery_type"]["value"] == "branch_pickup"
    assert "city" not in stored
    stored = _post_fields(c, token, signature="", delivery_type="home")
    assert "signature" not in stored and stored["delivery_type"]["value"] == "home"
    assert "Not stated" not in str(stored)
    stored = _post_fields(c, token, signature="", delivery_type="")
    assert stored == {}


def _checks_from_stored(manual, total="12500.00", currency="MXN"):
    case = m1.load_case("c11_paper_signature_12500")
    case["docs"] = []
    ctx = m1.build_context(case)
    docs = [ExtractedDoc(doc_id="manual", fields=manual, raw_text_available=False)]
    return {c.id: c for c in evaluate(ctx, docs, load_rules(), today=m1.today_of(case))}


@pytest.mark.parametrize("value,state", [("online", "verified"), ("paper_only", "present_unverifiable"),
                                         ("aggregator_portal", "present_unverifiable"), ("none", "missing")])
def test_each_signature_option_reaches_the_check_as_the_state_the_check_gives_it(value, state):
    c, token = _web(Fake(detail=WAITING))
    stored = _post_fields(c, token, signature=value)
    checks = _checks_from_stored(stored)
    assert checks["signature"].state == state, (value, checks["signature"].seller_message)


def test_branch_pickup_through_the_select_raises_the_branch_check_and_home_does_not():
    c, token = _web(Fake(detail=WAITING))
    branch = _checks_from_stored(_post_fields(c, token, delivery_type="branch_pickup"))["branch_pickup"]
    assert branch.state == "present_unverifiable" and branch.risk
    home = _checks_from_stored(_post_fields(c, token, delivery_type="home"))["branch_pickup"]
    assert home.state == "not_applicable" and not home.risk
    none = _checks_from_stored(_post_fields(c, token, delivery_type=""))["branch_pickup"]
    assert none.state == "not_applicable"


# ---- session-expired text -----------------------------------------------------------------------------------------
def test_the_session_expired_text_has_no_loopback_literal():
    c, token = _web(Fake(detail=WAITING))
    r = c.post(f"/disputes/{DID}/manual-fields", data={"csrf_token": "wrong", "package_id": "1"})
    t = _text(r)
    assert r.status_code == 403 and "127.0.0.1" not in t
    assert ("Your session expired before the form was sent. Nothing was sent. If it keeps happening, check that your "
            "browser allows cookies for this site.") in t


# ---- no rule set for the country -------------------------------------------------------------------------------------
LINE = ("Proofie has no Seller Protection rule set for the country in this installation's setting ({}), so it cannot check these items. "
        "Everything you type or upload is still shown, but nothing here is marked as verified against a rule.")


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_an_unknown_country_renders_the_line_with_the_country_and_the_package_stays_usable(monkeypatch):
    monkeypatch.setattr(checklist_input, "RULES_COUNTRY", "ZZ")
    monkeypatch.setattr(checklist_input, "_RULES_CACHE", [])
    c, token = _web(Fake(detail=WAITING))
    db = config.db_path()
    repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    pid = repo.insert_package(db, DID, source="human_edit", validation_ok=False, review_flags=[])
    r = c.get(f"/disputes/{DID}/packages/{pid}")
    assert r.status_code == 200
    t = _text(r)
    assert LINE.format("ZZ") in t and 'data-state="no-rule-set"' in r.text
    assert "Set COPILOT_RULES_COUNTRY" not in t and "Rules:" not in t              # no 500 page, no dangling rules link
    assert 'id="shipment-details"' in r.text and 'id="next-step"' in r.text          # the page and its forms still work
    assert 'data-check="' not in r.text                                            # nothing is shown as checked


def test_the_country_in_parentheses_is_dropped_when_none_is_known(monkeypatch):
    from copilot.web import routes_package
    monkeypatch.setattr(checklist_input, "RULES_COUNTRY", "")
    assert routes_package._no_rule_set_line() == LINE.format("").replace(" ()", "")


def test_a_known_country_shows_no_such_line(monkeypatch):
    _, r = _package_page()
    assert 'data-state="no-rule-set"' not in r.text and "Rules:" in _text(r)


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
