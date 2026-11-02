"""Fulfilment pages: language of parts and plain refusals (offline, fake client, temp database)."""
import pytest
from _lang import spanish_outside_lang
from test_routes import CAP, _web

pytestmark = [pytest.mark.usefixtures("web_mode")]


def test_the_tracking_form_rule_title_and_citations_are_marked_as_spanish(web_mode, monkeypatch):
    web, _fake = _web(monkeypatch)
    html = web.get(f"/fulfilment/{CAP}").text
    assert "Protección al Vendedor" in html and "Comprobante de entrega" in html
    assert spanish_outside_lang(html) == []


# ---- PayPal's refusal of a tracking, in words
import re

from copilot.core.disputes import PayPalError
from copilot.features.fulfilment import store
from test_routes import FakeClient, _register_data, _sha_of_preview


def _outside_details(html):
    return re.sub(r"<details.*?</details>", "", html, flags=re.S)


def test_a_refused_tracking_is_explained_in_words_and_its_codes_stay_in_technical_details(web_mode, monkeypatch):
    body = {"details": [{"issue": "INVALID_TRACKING_NUMBER", "description": "The tracking number format is not valid."}]}
    web, _fake = _web(monkeypatch, FakeClient(post_raises=PayPalError(400, "INVALID_TRACKING_NUMBER", "dbg-9", body)))
    sha, _html = _sha_of_preview(web)
    html = web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha)).text
    saved = web.get(f"/fulfilment/{CAP}/file/{store.get_file(CAP)['id']}").text
    for page in (html, saved):
        from html import unescape
        visible = unescape(_outside_details(page))
        assert "PayPal says the tracking number format is not valid for this carrier. Check the number on the carrier's receipt." in visible
        assert not re.search(r"INVALID_TRACKING_NUMBER|error 400|\b400\b", visible)
        details = " ".join(re.findall(r"<details.*?</details>", page, flags=re.S))
        assert "INVALID_TRACKING_NUMBER" in details and "400" in details
    assert "error 400" not in store.get_file(CAP)["limitation_text"]
