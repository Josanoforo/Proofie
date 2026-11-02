import pytest

from copilot.features.sale_readiness import service


def test_physical_digital_service_required_evidence_and_tracking_prep():
    physical = service.build(service.EXAMPLES["physical"])
    assert physical["tracking_payload"]["status"].startswith("prepared local")
    assert physical["gaps"] == []
    digital = service.build(service.EXAMPLES["digital"])
    assert digital["required_evidence_kinds"] == ["access", "purchase"]
    assert service.build(service.EXAMPLES["service"])["gaps"]


def test_unlinked_buyer_transaction_and_missing_acceptance_are_explicit():
    data = {**service.EXAMPLES["physical"], "acceptance_ref": "", "evidence": [{**service.EXAMPLES["physical"]["evidence"][0], "buyer_ref": "OTHER"}]}
    result = service.build(data)
    assert result["unlinked_evidence_ids"] == ["o1"]
    assert "evidence_unlinked:o1" in result["gaps"]
    assert "acceptance_ref" in result["gaps"]
    assert result["tracking_payload"] is None
    assert any(x.startswith("evidence:") for x in result["gaps"])


def test_unknown_carrier_is_prepared_as_named_other_with_warning():
    result = service.build({**service.EXAMPLES["physical"], "carrier": "Carrier Not Supported"})
    payload = result["tracking_payload"]
    assert payload["carrier"] == "OTHER"
    assert payload["carrier_name_other"] == "Carrier Not Supported"
    assert payload["carrier_warning"] == "carrier not in the PayPal Tracking catalogue; registered as OTHER"
    assert payload["carrier_risk"] is True
    assert "not independently verified or provider-accepted" in payload["source_label"]


@pytest.mark.parametrize("name,expected", [
    ("Estafeta", "MEX_ESTAFETA"),
    ("Correos de México", "CORREOS_DE_MEXICO"),
    ("Paquetexpress", "PAQUETEXPRESS"),
    ("DOMINO", "DOMINO"),
    ("UPS", "UPS"),
    ("FEDEX", "FEDEX"),
    ("DHL", "DHL"),
    ("USPS", "USPS"),
])
def test_tracking_preparation_uses_tracking_mapping_and_keeps_legacy_carriers(name, expected):
    result = service.build({**service.EXAMPLES["physical"], "carrier": name})
    assert result["tracking_payload"]["carrier"] == expected
    assert result["tracking_payload"]["status"].startswith("prepared local Tracking")
    assert "Seller-supplied" in result["tracking_payload"]["source_label"]
    if name == "USPS":
        assert result["tracking_payload"]["carrier_warning"] is None
    if name == "DOMINO":
        from copilot.core.carriers import evidence_carrier

        assert evidence_carrier("DOMINO", None) == ("OTHER", "Domino")


@pytest.mark.parametrize("name,warning", [
    ("99minutos", "not in the PayPal Tracking catalogue"),
    ("Skydropx", "underlying carrier's proof"),
])
def test_other_carriers_prepare_locally_with_name_warning_and_risk(name, warning):
    result = service.build({**service.EXAMPLES["physical"], "carrier": name})
    payload = result["tracking_payload"]
    assert payload["carrier"] == "OTHER"
    assert payload["carrier_name_other"] == name
    assert payload["carrier_warning"] and warning in payload["carrier_warning"]
    assert payload["carrier_risk"] is True
    assert "not independently verified or provider-accepted" in payload["source_label"]
    assert not any(gap.startswith("tracking:carrier warning") for gap in result["gaps"])


def test_delivery_is_later_stage_and_does_not_block_tracking_preparation():
    data = {**service.EXAMPLES["physical"], "evidence": [row for row in service.EXAMPLES["physical"]["evidence"] if row["kind"] != "delivery"]}
    result = service.build(data)
    assert "evidence:delivery" in result["gaps"]
    assert result["post_shipment_gaps"] == ["delivery"]
    assert result["tracking_payload"]["status"].startswith("prepared local")
    assert "later-stage" in result["status"]


@pytest.mark.parametrize("changes", [{"price_cents": 1.2}, {"currency": "usd"}, {"expected_date": "not-date"}, {"carrier": ""}, {"ship_to": {}}])
def test_invalid_money_date_and_tracking_inputs_fail_closed(changes):
    if "price_cents" in changes or "currency" in changes:
        with pytest.raises(ValueError): service.build({**service.EXAMPLES["physical"], **changes})
    else:
        result = service.build({**service.EXAMPLES["physical"], **changes})
        assert result["tracking_payload"] is None or "valid_expected_date" in result["gaps"]
