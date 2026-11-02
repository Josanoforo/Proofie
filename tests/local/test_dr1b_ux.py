"""Focused user-facing time and form contracts; no external services."""
from datetime import datetime, timezone
import pytest
from copilot.web import timefmt


@pytest.mark.parametrize("value", [1791299995286, "1791299995286", datetime(2026,10,6,16,39,55,tzinfo=timezone.utc)])
def test_paypal_and_stored_timestamps_share_one_explicit_city_zone(value):
    formatted = timefmt.fmt_iso(value)
    assert "UTC-6" in formatted
    assert "T16:" not in formatted and formatted != str(value)
    assert formatted.endswith("(UTC-6)")


@pytest.mark.parametrize("value", [True, -1, "-1", "253402300800000"])
def test_invalid_epoch_values_are_not_displayed_as_times(value):
    assert timefmt.fmt_iso(value) == "Not available"


def test_manual_package_can_look_complete_and_case_has_tasks_and_timeline(web_mode, monkeypatch):
    from test_routes_package import _client, DID
    from copilot.db import repo
    from copilot.web import routes_package
    from copilot.core.context import TransactionContext
    from copilot.core.checklist import Check
    web, db, _ = _client(monkeypatch=monkeypatch)
    pid = repo.insert_package(db, DID, "human_edit", False, [])
    repo.insert_claims(db,pid,[{"evidence_index":0,"claim_index":0,"text":"Synthetic seller statement.",
                              "source_ids":[],"basis":"seller_statement"}])
    repo.set_manual_fields(db,DID,{"tracking_number":{"value":"DEMO-1","confidence":"read"},
                                  "carrier":{"value":"FedEx","confidence":"read"}})
    repo.log_action(db,"vendor","set_manual_fields",DID,{"fields":["carrier","tracking_number"]})
    checks=[Check(k,k,"verified","app_quality_rule","Synthetic",[],"none",False,"Synthetic verified field.")
            for k in ("tracking_number","carrier","delivery_status","delivery_date","destination","document_integrity")]
    monkeypatch.setattr(routes_package,"_checks_for",lambda *a:(checks,TransactionContext()))
    response=web.get(f"/disputes/{DID}")
    assert response.status_code == 200
    assert "Tasks in this case" in response.text and "Cannot start yet." in response.text
    prepare = web.get(f"/disputes/{DID}/workspace/prepare").text
    records = web.get(f"/disputes/{DID}/workspace/records").text
    assert "Open package v1" in prepare and "Draft: needs attention" not in prepare      # a complete draft is not red
    assert "Saved shipment details" in records and "Mexico City time" in records
