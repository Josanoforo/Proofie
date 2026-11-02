"""The markup contract the stylesheet relies on (stacked tables with visible per-cell labels, claim
cards with their source, the shell linking the local stylesheet) plus the Check again control (with nothing pending it must
not read like a consultation of PayPal). DOM and effect, fake clients only.
"""
import pytest
from _hosted import authenticated_client, reset_csrf
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_routes_disputes as disputes_tests
import test_routes_send as send_tests
from _dom import actions_in, parse, state
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.db import repo
from copilot.web.security import CSRF_COOKIE_NAME

DID = send_tests.DID


def external_resources(dom) -> list:
    """Every element that would LOAD something (stylesheet, script, image, frame, media source) from a non-local URL."""
    loaders = [n for tag in ("link", "script", "img", "iframe", "source") for n in dom.find_all(tag)]
    return [n.attrs for n in loaders
            if (n.attrs.get("href") or n.attrs.get("src") or "").startswith(("http://", "https://", "//"))]


def test_the_external_resource_check_can_actually_fail():
    bad = parse('<link rel="stylesheet" href="https://cdn.example/x.css"><script src="//cdn.example/a.js"></script>'
                '<img src="/local.png"><a href="https://example.org/just-a-link">x</a>')
    assert len(external_resources(bad)) == 2                   # the CDN stylesheet and script, not the local image or the link


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_every_page_links_the_local_stylesheet_and_script_and_no_external_resource(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    for path in (f"/disputes/{DID}", f"/disputes/{DID}/packages/{pkg_id}"):
        r = c.get(path)
        dom = parse(r.text)
        assert dom.find("link", rel="stylesheet", href="/static/app.css") is not None
        assert dom.find("meta", name="viewport") is not None
        assert external_resources(dom) == [], path          # nothing is LOADED from outside (a navigation link is fine)
    assert c.get("/static/app.css").status_code == 200


def test_inbox_rows_carry_visible_column_labels_for_the_phone_layout():
    c = disputes_tests._client(disputes_tests._app_with_fake(disputes_tests.FakeClient(list_body=disputes_tests.LIST_PAGE1)))
    dom = parse(c.get("/disputes").text)
    table = dom.find("table", data_table="inbox")
    assert table is not None and "stack" in table.attrs["class"].split()
    cells = table.find("tbody").find("tr").find_all("td")
    assert [td.attrs.get("data-label") for td in cells] == ["Dispute", "Status", "Respond by", "Amount", "Next step"]
    assert cells[1].find("span", class_="tag") is not None             # the status is a tag with its words


def test_the_checks_table_labels_every_cell_and_keeps_the_state_in_words(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    row = next(r for r in dom.find_all("tr") if "data-check-state" in r.attrs)
    assert [td.attrs.get("data-label") for td in row.find_all("td")] == ["Check", "Status", "Why", "What you can do"]
    assert row.find_all("td")[1].find("span", class_="tag").text()                  # a state word, not only an edge colour


def test_a_statement_is_a_card_with_its_basis_in_words_and_its_source(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    doc_id = repo.insert_document(db_path, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: FedEx", True)
    pkg_id = repo.insert_package(db_path, DID, source="ai", validation_ok=True, review_flags=[])
    repo.insert_claims(db_path, pkg_id, [
        {"evidence_index": 0, "claim_index": 0, "text": "Shipped via FedEx.", "source_ids": [f"doc_{doc_id}"], "basis": "verified"},
        {"evidence_index": 0, "claim_index": 1, "text": "Buyer may be confused.", "source_ids": [f"doc_{doc_id}"], "basis": "model_interpretation"},
        {"evidence_index": 0, "claim_index": 2, "text": "I packed it myself.", "source_ids": [], "basis": "seller_statement"}])
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    english_cards = [card for card in dom.find_all("li", class_="claim-card") if "data-claim" in card.attrs]
    assert len(english_cards) == 3
    assert all(card.attrs.get("id") == f"claim-{card.attrs['data-claim']}" for card in english_cards)
    assert all("data-basis" in card.attrs for card in english_cards)  # every English statement must retain its basis
    cards = {card.attrs["data-basis"]: card for card in english_cards}
    assert set(cards) == {"verified", "model_interpretation", "seller_statement"}
    assert "Source: document label.pdf" in cards["verified"].find("span", data_source="").text()
    suggested = cards["model_interpretation"]
    assert "Suggested" in suggested.text() and "check against the source" in suggested.text()
    chip = suggested.find("span", class_="chip")
    assert "chip-suggested" in chip.attrs["class"].split()               # dashed edge: the assistant's, not a fact
    assert "Source: you" in cards["seller_statement"].find("span", data_source="").text()
    assert all("doc_" not in card.text() for card in cards.values())     # the internal source id never reaches the seller's cards
    # (it may still appear inside the folded technical details, which is where raw values belong)
    for card in cards.values():                                          # every statement is editable through a labelled input
        edit = card.find("form", class_="claim-edit")
        label = edit.find("label")
        assert edit.find("textarea").attrs["id"] == label.attrs["for"]


def test_every_input_on_the_review_page_has_a_visible_label(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    r = c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)
    pkg_id = int(r.headers["location"].rsplit("/", 1)[-1])
    dom = parse(c.get(f"/disputes/{DID}/packages/{pkg_id}").text)
    labels_for = {lab.attrs.get("for") for lab in dom.find_all("label") if lab.attrs.get("for")}
    for field in dom.find_all("input"):
        if field.attrs.get("type") in ("hidden", "checkbox"):
            continue
        assert field.attrs.get("id") in labels_for or field.inside("label"), field.attrs


# ---- check again with nothing pending does not read like a paypal consultation --------------------
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_with_nothing_pending_does_not_read_like_a_paypal_consultation(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    before = list(fake.calls_log)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    assert r.status_code == 200
    dom = parse(r.text)
    box = state(dom, "nothing-pending")
    assert box is not None and "Nothing pending to check with PayPal" in box.text()
    assert dom.find("h1").text() == "Nothing pending to check with PayPal"
    assert "Checked" not in dom.find("h1").text()
    assert fake.calls_log == before                                      # the effect: PayPal was not asked at all
    assert fake.provide_evidence_calls == []
    assert not box.find_all("form")                                      # and there is nothing to "check again"


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_check_again_on_an_attempt_with_no_stored_request_says_it_could_not_compare(tmp_path, monkeypatch):
    c, db_path, token, fake = send_tests._client(tmp_path, monkeypatch)
    pkg_id = send_tests._approved_package_with_tracking(c, db_path, token)
    from copilot.core.checklist_input import extracted_docs_for
    from copilot.core.send import build_request_input, pick_tracking, sha256_json
    tn, carrier = pick_tracking(extracted_docs_for(db_path, DID))
    sha = sha256_json(build_request_input(repo.list_claims(db_path, pkg_id), tn, carrier))
    store = SQLiteAttemptStore(db_path)
    store.claim(DID, sha)
    store.write(DID, sha, {"state": "sent_unconfirmed", "request_id": "req-old"})   # no request_input stored
    store.close()
    before = list(fake.calls_log)
    r = c.post(f"/disputes/{DID}/packages/{pkg_id}/check", data={"csrf_token": token})
    box = state(parse(r.text), "cannot-check")
    assert box is not None and "No check against PayPal could be made" in box.text()
    assert "banner-warn" in box.attrs["class"] and fake.calls_log == before


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
