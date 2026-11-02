from copilot.features.two_clocks.service import compare


def test_delivery_estimate_before_paypal_deadline():
    result = compare("2026-11-01", "2026-11-03")
    assert result["state"] == "delivery_first"
    assert "wait and update" in result["message"]


def test_paypal_deadline_before_delivery_estimate():
    result = compare("2026-11-04", "2026-11-03")
    assert result["state"] == "deadline_first"
    assert "review the case today" in result["message"]


def test_missing_or_invalid_dates_are_not_invented():
    result = compare("invalid", None)
    assert result["state"] == "incomplete"
    assert result["delivery_date"] is None and result["deadline"] is None
