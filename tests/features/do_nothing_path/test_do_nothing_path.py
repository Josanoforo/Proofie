from copilot.features.do_nothing_path.service import describe


def test_path_uses_integer_cents_and_paypal_deadline_without_predicting_outcome():
    result = describe(12345, "USD", 1500, "2026-11-05T23:59:59Z")
    assert result["amount_at_stake"] == "USD 123.45"
    assert result["fee_estimate"] == "USD 15.00"
    assert result["deadline"] == "2026-11-05"
    assert "not a predicted outcome" in result["limit"]


def test_unavailable_dates_and_fees_are_reported_without_invention():
    result = describe(None, "MXN", None, None)
    assert result["deadline"] is None and result["fee_estimate"] is None
    assert "did not supply" in result["timing"]
    assert "No fee amount" in result["fee_label"]


def test_money_requires_integer_cents():
    result = describe(1.5, "USD", 0, "bad")
    assert result["amount_at_stake"] == "Unavailable from the current PayPal record"


def test_feature_registers_the_helper_in_the_existing_resolution_economics_panel():
    from copilot.features.do_nothing_path import routes  # noqa: F401
    from copilot.web.routes_disputes import templates
    from pathlib import Path

    source = Path("copilot/web/templates/features/resolution/resolution.html").read_text(encoding="utf-8")
    assert templates.env.globals["do_nothing_path"] is describe
    assert 'feature_enabled("do_nothing_path")' in source
    assert 'features/do_nothing_path/_entry.html' in source
