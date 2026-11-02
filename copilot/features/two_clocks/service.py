from datetime import date


def compare(expected_delivery: str | None, paypal_deadline: str | None) -> dict:
    """Compare only dates supplied by existing records; never infer either date."""
    delivery = _parse(expected_delivery)
    deadline = _parse(paypal_deadline)
    if delivery is None or deadline is None:
        return {"delivery_date": delivery.isoformat() if delivery else None,
                "deadline": deadline.isoformat() if deadline else None,
                "message": "Both a recorded delivery estimate and a PayPal response deadline are needed to compare the dates.",
                "state": "incomplete"}
    if delivery < deadline:
        return {"delivery_date": delivery.isoformat(), "deadline": deadline.isoformat(),
                "message": "Recorded delivery estimate is before the PayPal deadline: you can wait and update the buyer.",
                "state": "delivery_first"}
    if delivery > deadline:
        return {"delivery_date": delivery.isoformat(), "deadline": deadline.isoformat(),
                "message": "PayPal's response deadline is before the recorded delivery estimate: review the case today.",
                "state": "deadline_first"}
    return {"delivery_date": delivery.isoformat(), "deadline": deadline.isoformat(),
            "message": "The recorded delivery estimate and PayPal deadline are the same day; review the case today.",
            "state": "same_day"}


def _parse(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
