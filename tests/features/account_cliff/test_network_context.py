"""The network-context paragraph beside the account cliff: constants with sources and read dates, rendered from code."""
import pytest
from _hosted import authenticated_client, reset_csrf
import html
import re

from copilot.features.account_cliff import network_context as nc
from copilot.features.account_cliff.rules import HIGH_VOLUME_MIN_SALES, HIGH_VOLUME_RATE_BPS

from test_service_and_routes import FakeClient, _app, _web


def test_every_network_fact_has_a_source_and_an_iso_read_date():
    assert [f.name for f in nc.NETWORK_FACTS] == ["Visa VAMP", "Mastercard ECM", "PayPal high-volume dispute rule"]
    for f in nc.NETWORK_FACTS:
        assert f.display.strip() and f.source.strip() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.read_date)


def test_the_numbers_are_the_ones_the_brief_names():
    assert "150 basis points" in nc.VISA_VAMP.display and "2025-06-01" in nc.VISA_VAMP.display
    assert "100 chargebacks" in nc.MASTERCARD_ECM.display and "1.5 %" in nc.MASTERCARD_ECM.display
    assert "previous month" in nc.MASTERCARD_ECM.display
    assert f"{HIGH_VOLUME_MIN_SALES.value} sales" in nc.PAYPAL_HIGH_VOLUME.display          # one constant, two renderings
    assert f"{HIGH_VOLUME_RATE_BPS.value / 100:.2f} %" in nc.PAYPAL_HIGH_VOLUME.display
    assert "Help Center article 350" in nc.PAYPAL_HIGH_VOLUME.source
    assert nc.WON_DISPUTE_SENTENCE == "A dispute you win still counts in these ratios."


def test_flag_on_page_renders_the_paragraph_with_the_three_sources_and_dates(monkeypatch):
    from copilot.web.presentation import dates_in_words
    r = _web(_app(FakeClient(200, 2), "true", monkeypatch)).get("/account")
    assert r.status_code == 200
    page = html.unescape(r.text)
    assert page.count('data-state="network-context"') == 1 and page.count("data-network=") == 3
    for f in nc.NETWORK_FACTS:
        words = dates_in_words        # an ISO date inside a generated sentence reads as words on the page
        assert f.name in page and words(f.display) in page and f"Source: {words(f.source)}; read { {'2026-10-05':'5 October 2026','2026-10-06':'6 October 2026'}[f.read_date]}." in page
    assert nc.WON_DISPUTE_SENTENCE in page
    assert page.index("Constants and sources") < page.index("Network context")


def test_flag_off_there_is_no_page_and_so_no_paragraph(monkeypatch):
    web = _web(_app(monkeypatch=monkeypatch))
    r = web.get("/account")
    assert r.status_code == 404 and "network-context" not in r.text
    assert "network-context" not in web.get("/disputes").text


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
