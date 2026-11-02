from datetime import date


def describe(amount_cents, currency, fee_cents, paypal_deadline):
    if type(amount_cents) is not int or amount_cents < 0 or not isinstance(currency, str) or not currency:
        amount = "Unavailable from the current PayPal record"
    else:
        amount = f"{currency} {_money(amount_cents)}"
    try:
        deadline = date.fromisoformat(paypal_deadline[:10]).isoformat() if isinstance(paypal_deadline, str) else None
    except ValueError:
        deadline = None
    fee = f"{currency} {_money(fee_cents)}" if type(fee_cents) is int and fee_cents >= 0 else None
    return {"amount_at_stake": amount, "fee_estimate": fee, "deadline": deadline,
            "fee_label": "Configured fee amount; not evidence that PayPal applied a fee" if fee else
                         "No fee amount is available for this case and currency",
            "timing": f"PayPal's response deadline is {deadline}." if deadline else
                      "PayPal did not supply a readable response deadline.",
            "limit": "This is exposure if no response is made by the deadline, not a predicted outcome. PayPal decides the case."}


def _money(cents):
    return f"{cents // 100}.{cents % 100:02d}"
