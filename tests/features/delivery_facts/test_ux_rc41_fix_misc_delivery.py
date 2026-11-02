"""The shipment-facts page labels the file like the fulfilment pages."""
import re

import pytest
from _hosted import authenticated_client

from copilot import config
from copilot.app import create_app
from copilot.core.send import sha256_json
from copilot.features.delivery_facts import service
from copilot.features.fulfilment import store as fulfilment_store

CAP = "DEMO1234567890123"
NUMBER = "DEMO1234567890"
REQUEST = {"trackers": [{"transaction_id": CAP, "tracking_number": NUMBER, "carrier": "FEDEX", "status": "SHIPPED"}]}


def _save(status, limitation):
    fulfilment_store.save_file(CAP, REQUEST, sha256_json(REQUEST), sha256_json(REQUEST), None,
                               {"address_comparison": {"result": "match"}}, status, limitation_text=limitation)


def _file_status_cell(html):
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.search(r'<dd data-status="[^"]*">(.*?)</dd>', html, re.S).group(1))).strip()
    return re.sub(r"^[^A-Za-z]+", "", text)                      # the tag's icon glyph is not part of the label


@pytest.mark.parametrize("limitation", ["PayPal could not register the tracking (error 503).",
                                        "PayPal did not answer in time, so what happened is unknown.",
                                        "PayPal answered 429 (error 429); what happened is unknown."])
def test_sh_13_di_7_a_legacy_failed_row_with_an_unknown_outcome_reads_as_unknown(web_mode, monkeypatch, limitation):
    monkeypatch.setenv("COPILOT_FEATURE_DELIVERY_FACTS", "true")
    _save("failed", limitation)
    row = fulfilment_store.get_file(CAP)
    assert fulfilment_store.effective_status(row) == "unknown"            # the oracle the fulfilment pages use
    assert service.read(config.db_path(), CAP)["record_status"] == "unknown"
    page = authenticated_client(create_app()).get("/delivery-facts/" + CAP)
    assert page.status_code == 200
    assert _file_status_cell(page.text) == "Send attempted - not confirmed by PayPal"
    assert 'data-status="unknown"' in page.text and "Not accepted by PayPal" not in page.text


def test_sh_13_di_7_a_real_refusal_and_the_other_states_keep_their_label(web_mode, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_DELIVERY_FACTS", "true")
    _save("failed", "PayPal refused the tracking: the transaction cannot be tracked (error 422).")
    assert fulfilment_store.effective_status(fulfilment_store.get_file(CAP)) == "failed"
    assert service.read(config.db_path(), CAP)["record_status"] == "failed"
    page = authenticated_client(create_app()).get("/delivery-facts/" + CAP)
    assert _file_status_cell(page.text) == "Not accepted by PayPal"


def test_sh_13_di_7_the_reader_keeps_the_fulfilment_rule_word_for_word():
    assert service._LEGACY_UNKNOWN.pattern == fulfilment_store._LEGACY_UNKNOWN.pattern
    assert service._LEGACY_UNKNOWN.flags == fulfilment_store._LEGACY_UNKNOWN.flags
