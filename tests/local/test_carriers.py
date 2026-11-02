"""Offline tests for copilot/core/carriers.py (Tracking API carrier catalogue mapping)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.carriers import map_carrier


@pytest.mark.parametrize("name,code", [
    ("Correos de México", "CORREOS_DE_MEXICO"), ("ESTAFETA", "MEX_ESTAFETA"),
    ("Paquete Express", "PAQUETEXPRESS"), ("Redpack", "MEX_REDPACK"), ("Hound Express", "HOUNDEXPRESS"),
    ("Autobuses Senda", "MEX_SENDA"), ("Domino", "DOMINO"), ("Grupo", "GRUPO"),
    ("DHL Express", "DHL"), ("FedEx", "FEDEX"), ("UPS", "UPS"), ("MEX_ESTAFETA", "MEX_ESTAFETA"),
])
def test_catalogue_carriers_map_to_tracking_codes_without_risk(name, code):
    m = map_carrier(name)
    assert (m.code, m.known, m.risk, m.carrier_name_other) == (code, True, False, None)


def test_global_carriers_do_not_get_country_specific_codes():
    assert map_carrier("DHL México").code == "DHL"


def test_99minutos_is_other_with_name_and_risk_not_invalid():
    m = map_carrier("99minutos")
    assert (m.code, m.carrier_name_other, m.risk, m.known) == ("OTHER", "99minutos", True, False)


def test_unknown_carrier_is_other_with_risk():
    m = map_carrier("Mensajeria Fantasma SA")
    assert m.code == "OTHER" and m.risk and m.carrier_name_other == "Mensajeria Fantasma SA"


@pytest.mark.parametrize("name", ["Skydropx", "Envia.com", "Mienvio", "ENVÍA"])
def test_aggregators_are_other_with_the_underlying_carrier_note(name):
    m = map_carrier(name)
    assert m.code == "OTHER" and m.aggregator and m.risk
    assert "underlying carrier" in m.note


def test_empty_name_gives_no_code():
    assert map_carrier(None).code is None and map_carrier("  ").code is None
