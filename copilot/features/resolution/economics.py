"""The economics line: what conceding and what fighting cost, by code, each constant with its source. Money is integer
cents turned into text by code. The line states costs only; it makes no claim about how a case ends.

Rule as implemented:
* concede  = refund the amount (concede_cents = amount_cents) + the dispute fee when `fee_applies_to_concede`
             (the caller turns it off at the INQUIRY stage: the help pages list the exemption "resolved directly and not
             escalated"; fee amounts: US fees page dated Oct 1, 2026: Standard 15.00 USD, High Volume 30.00 USD);
* fight    = the fee alone if the case goes the seller's way, the fee plus the amount if it does not
             (fight_cents = (fee, fee + amount));
* Seller Protection: the fees pages state no fee waiver for an internal dispute in so many words, so none is applied; the
             line shows PayPal's eligibility code from the transaction and points to the fees page;
* fee when the buyer closes the case: the help pages do not state it, so the line says "not confirmed" unless the caller
             passes buyer_closes_fee_known=True (then it only says the fee is as per the fees page)."""
from __future__ import annotations

from dataclasses import dataclass, field

from copilot.features.account_cliff.rules import SOURCE_FEES_PAGE, SOURCE_HELP350
from copilot.features.resolution.actions import cents_to_str

SOURCE_HELP349 = ("PayPal Help Center articles 349 and 350. They tie the fee to the creation of the case and list "
                  "exemptions; they do not state the fee when the buyer closes the case.")
SOURCE_NO_WAIVER = "PayPal US business fees page. It states no Seller Protection fee waiver for a dispute resolved inside PayPal."
FEE_CURRENCY = "USD"


@dataclass(frozen=True)
class EconomicsLine:
    concede_cents: int | None
    fight_cents: tuple | None          # (fee if the case goes the seller's way, fee + amount otherwise)
    fee_cents: int | None
    text: str
    sources: tuple
    # display fields; compare=False keeps two lines equal when only their wording differs (triage compares).
    choices: tuple = field(default=(), compare=False)    # the seller-facing lines of "What each choice costs", by code
    notes: tuple = field(default=(), compare=False)      # the same facts with their sources, for the details block


def economics_line(amount_cents: int, currency: str, fee_cents: int, fee_label: str,
                   protection_eligible: bool | None, buyer_closes_fee_known: bool,
                   fee_applies_to_concede: bool = True, eligibility_code: str | None = None,
                   high_volume: bool = False, fee_level_text: str | None = None) -> EconomicsLine:
    sources = [SOURCE_FEES_PAGE, SOURCE_NO_WAIVER, SOURCE_HELP349] + ([SOURCE_HELP350] if high_volume else [])
    if currency != FEE_CURRENCY:
        text = (f"Economics not computed: the fee page amounts are in {FEE_CURRENCY} and this dispute is in "
                f"{currency} · sources: " + "; ".join(sources))
        return EconomicsLine(None, None, None, text, tuple(sources),
                             (f"Not computed: the fee page amounts are in {FEE_CURRENCY} and this dispute is in {currency}.",),
                             tuple(sources))
    concede_fee = fee_cents if fee_applies_to_concede else 0
    concede = amount_cents + concede_fee
    fight = (fee_cents, fee_cents + amount_cents)
    money = lambda c: f"{cents_to_str(c, currency)} {currency}"  # noqa: E731 - local formatting helper
    if protection_eligible is None and eligibility_code is None:
        protection = "Seller Protection: eligibility not read, fee rule as per the fees page (Oct 1, 2026)"
    elif eligibility_code is not None:
        protection = (f"Seller Protection: PayPal's eligibility code {eligibility_code}, fee rule as per the fees "
                      f"page (Oct 1, 2026)")
    else:
        protection = (f"Seller Protection: {'eligible' if protection_eligible else 'not eligible'}, fee rule as per "
                      f"the fees page (Oct 1, 2026)")
    closes = ("fee when the buyer closes: as per the fees page" if buyer_closes_fee_known
              else "fee when the buyer closes: not confirmed")
    concede_fee_text = money(concede_fee)
    refund_line = (f"Refund the buyer in full: {money(amount_cents)} + {money(concede_fee)} fee = {money(concede)}"
                   if concede_fee else f"Refund the buyer in full: {money(concede)}, with no dispute fee at this stage")
    choices = (refund_line,
               f"Defend with evidence: fee {money(fee_cents)} if PayPal decides for you; "
               f"{money(fee_cents + amount_cents)} if PayPal decides for the buyer",
               f"Fee level: {fee_level_text or fee_label}",
               "Fee if the buyer closes the case: " + ("as per PayPal's fees page" if buyer_closes_fee_known
                                                       else "not confirmed by PayPal's pages"))
    notes = (protection, f"Fee level used: {fee_label}") + tuple(sources)
    text = (f"Concede: refund {money(amount_cents)} + fee {concede_fee_text} = {money(concede)} · "
            f"Fight: fee {money(fee_cents)} if you win, {money(fee_cents + amount_cents)} if you lose · "
            f"{fee_label} · {protection} · {closes} · sources: " + "; ".join(sources))
    return EconomicsLine(concede, fight, fee_cents, text, tuple(sources), choices, notes)
