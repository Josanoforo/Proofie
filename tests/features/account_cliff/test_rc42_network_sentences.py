"""The network-context sentences are under 25 words and keep their numbers and sources (lives with the account_cliff
tests: it imports the feature, which the flags-off export omits together with its tests)."""
import re

from copilot.features.account_cliff import network_context as nc


def _sentences(text):
    return re.split(r"(?<=[.!?]) +(?=[A-Z0-9])", text)


def test_every_network_fact_sentence_is_under_25_words_and_keeps_its_numbers_and_source():
    from copilot.features.account_cliff import network_context as nc
    for fact in nc.NETWORK_FACTS:
        assert all(len(sentence.split()) < 25 for sentence in _sentences(fact.display)), fact.name
        assert fact.source and fact.read_date
    visa = nc.VISA_VAMP.display
    for number in ("150 basis points", "1.50 %", "2025-06-01"):
        assert number in visa
    assert "Latin America and the Caribbean" in visa and "settled transactions" in visa
