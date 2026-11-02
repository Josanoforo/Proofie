"""The economics line: integer cents, sources, no probability words."""
import re
from pathlib import Path

from copilot.features.account_cliff.rules import HIGH_VOLUME_FEE_CENTS, STANDARD_FEE_CENTS
from copilot.features.resolution import economics


def _line(fee=STANDARD_FEE_CENTS.value, **kw):
    args = dict(amount_cents=4000, currency="USD", fee_cents=fee, fee_label="standard fee", protection_eligible=None,
                buyer_closes_fee_known=False)
    args.update(kw)
    return economics.economics_line(**args)


def test_standard_fee_concede_5500_fight_1500_or_5500():
    line = _line()
    assert (line.concede_cents, line.fight_cents, line.fee_cents) == (5500, (1500, 5500), 1500)
    assert line.text.startswith("Concede: refund 40.00 USD + fee 15.00 USD = 55.00 USD · "
                                "Fight: fee 15.00 USD if you win, 55.00 USD if you lose · standard fee")


def test_high_volume_fee_concede_7000():
    line = _line(HIGH_VOLUME_FEE_CENTS.value, high_volume=True)
    assert (line.concede_cents, line.fight_cents) == (7000, (3000, 7000))
    assert "Help Center article 350" in " ".join(line.sources)


def test_inquiry_stage_concede_carries_no_fee_but_fight_keeps_it():
    line = _line(fee_applies_to_concede=False)
    assert (line.concede_cents, line.fight_cents) == (4000, (1500, 5500))
    assert "fee 0.00 USD = 40.00 USD" in line.text


def test_sources_are_non_empty_and_listed_in_the_text():
    line = _line()
    assert line.sources and all(s.strip() for s in line.sources)
    assert all(s in line.text for s in line.sources)


def test_buyer_closes_fee_is_not_confirmed_unless_the_caller_knows():
    assert "fee when the buyer closes: not confirmed" in _line().text
    assert "fee when the buyer closes: as per the fees page" in _line(buyer_closes_fee_known=True).text


def test_seller_protection_shows_the_code_and_no_waiver():
    text = _line(eligibility_code="01").text
    assert "Seller Protection: PayPal's eligibility code 01, fee rule as per the fees page (Oct 1, 2026)" in text
    clause = text.split("Seller Protection: ")[1].split(" · ")[0]      # the sources list says "no waiver stated"
    assert "waive" not in clause.lower()
    assert "not read" in _line().text


def test_a_dispute_in_another_currency_is_not_computed_with_usd_fees():
    line = _line(currency="MXN")
    assert line.concede_cents is None and line.fight_cents is None and "not computed" in line.text


def test_one_line_only():
    assert "\n" not in _line().text


def test_no_probability_words_and_no_float_in_the_module():
    source = Path(economics.__file__).read_text(encoding="utf-8").lower()
    for word in ("probab", "likely", "win rate", "chance"):
        assert word not in source, word
    assert not re.search(r"\bfloat\b|/ ?100\b", source)
