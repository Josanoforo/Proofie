"""The history and sent page word an attempt that was not received and a row
whose outcome is unknown without the stored "could not send" sentence. Offline: the fake PayPal of
resolution_fakes and a temp database. Red first."""
import pytest
from _dom import parse

from copilot.features.resolution import store
from resolution_fakes import DISPUTE_ID
from test_ux_rc41_fix2_sends import _history_note, _legacy_failed
from test_ux_rc41_fix_sends import _failed_message, _get, _unknown_message, _web
from test_ux_rc41_resolution import PAGE

TRIED = "Shown exactly as Proofie tried to send it. Your browser will not translate this part."
SENT = "Shown exactly as Proofie sent it. Your browser will not translate this part."


def _result_cell(hub, row_id):
    tr = next(tr for tr in parse(hub).find_all("tr") if any(
        a.attrs.get("href", "").endswith(f"/sent/{row_id}#sent") for a in tr.find_all("a")))
    return tr.find(None, data_label="Result").text()


# ---- an unconfirmed row says proofie sent it on its page ---
def test_mc2_4_an_unconfirmed_row_says_proofie_sent_it_on_its_page_and_in_the_history(monkeypatch):
    web, fake, fields, row = _unknown_message(monkeypatch)
    record = parse(_get(web, f"{PAGE}/sent/{row['id']}").text).find(None, data_state="sent-record")
    assert record.find("p", class_="exact-note").text() == SENT
    assert _history_note(_get(web, PAGE).text, row["id"]) == SENT


def test_mc2_4_a_refused_row_says_proofie_tried_to_send_it_on_its_page_and_in_the_history(monkeypatch):
    web, fake, row = _failed_message(monkeypatch)
    page = _get(web, f"{PAGE}/sent/{row['id']}").text
    record = parse(page).find(None, data_state="sent-record")
    assert record.find("p", class_="exact-note").text() == TRIED
    assert "will receive" not in page
    hub = _get(web, PAGE).text
    assert _history_note(hub, row["id"]) == TRIED and "will receive it" not in hub


def test_mc2_4_a_refused_offer_says_proofie_tried_to_send_it(monkeypatch):
    web, fake = _web(monkeypatch)
    saved, body, sha = _legacy_failed("PayPal could not send the offer (error 400).", action="offer")
    assert store.get_action(DISPUTE_ID, saved["id"])["status"] == "failed"
    page = _get(web, f"{PAGE}/sent/{saved['id']}").text
    assert parse(page).find(None, data_state="sent-record").find("p", class_="exact-note").text() == TRIED


# ---- a relabelled legacy failed row shows an unknown outcome sentence ---
@pytest.mark.parametrize("stored, expected", [
    ("PayPal could not send the message (error 503).", "PayPal answered error 503; whether it has the message is not known."),
    ("PayPal could not send the message (error 429).", "PayPal answered error 429; whether it has the message is not known."),
    ("PayPal did not answer in time when trying to send the message.", "PayPal did not answer in time; whether it has the message is not known."),
])
def test_g_sh_11_a_relabelled_legacy_failed_row_shows_an_unknown_outcome_sentence(monkeypatch, stored, expected):
    web, fake = _web(monkeypatch)
    saved, body, sha = _legacy_failed(stored)
    assert store.get_action(DISPUTE_ID, saved["id"])["status"] == "sent_unconfirmed"
    cell = _result_cell(_get(web, PAGE).text, saved["id"])
    assert "Send attempted - not confirmed by PayPal" in cell and expected in cell
    assert stored not in cell


def test_g_sh_11_an_offer_row_names_the_offer(monkeypatch):
    web, fake = _web(monkeypatch)
    saved, body, sha = _legacy_failed("PayPal could not send the offer (error 503).", action="offer")
    cell = _result_cell(_get(web, PAGE).text, saved["id"])
    assert "PayPal answered error 503; whether it has the offer is not known." in cell


def test_g_sh_11_a_real_refusal_keeps_its_stored_sentence(monkeypatch):
    web, fake, row = _failed_message(monkeypatch)
    cell = _result_cell(_get(web, PAGE).text, row["id"])
    assert row["limitation_text"] in cell and "is not known" not in cell
