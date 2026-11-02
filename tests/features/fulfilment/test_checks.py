"""pre_dispute_checks on the m1 fixtures (synthetic data): checklist items, eligibility, address comparison."""
import copy
from datetime import datetime, timezone

from copilot.core.checklist import UnknownCountryError
from copilot.fixtures.m1.loader import FakeClient, fake_client, load_case
from copilot.features.fulfilment import checks

CAP = "DEMOTXN0000000001"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
DECLARED_MATCH = {"postal_code": "06600", "city": "Ciudad de Mexico", "state": "CDMX", "country_code": "MX"}


def _case(eligibility="01"):
    case = copy.deepcopy(load_case("c01_complete"))
    if eligibility is not None:
        case["paypal"]["transactions"]["transaction_details"][0]["transaction_info"][
            "protection_eligibility"] = eligibility
    return case


def _run(case, declared=DECLARED_MATCH):
    client = fake_client(case)
    return client, checks.pre_dispute_checks(client, CAP, declared, now=NOW)


def _by_id(result):
    return {c["id"]: c for c in result.checks}


def test_eligibility_items_come_from_the_checklist_with_paypals_sources():
    _client, result = _run(_case())
    by_id = _by_id(result)
    for cid in ("tracking_number", "carrier", "delivery_status"):
        assert cid in by_id
    assert by_id["tracking_number"]["state"] == "verified" and by_id["tracking_number"]["paypal_sourced"]
    assert by_id["carrier"]["state"] == "verified"
    assert by_id["delivery_status"]["state"] == "verified"
    assert result.rules["country"] == "MX" and result.rules["rule_set_id"]


def test_protection_eligibility_is_paypals_raw_code_or_none():
    assert _run(_case("01"))[1].protection_eligibility == "01"
    assert _run(_case(None))[1].protection_eligibility is None


def test_items_that_need_documents_are_not_invented_and_dispute_only_items_are_marked_not_evaluable():
    _client, result = _run(_case())
    ids = {c["id"] for c in result.checks}
    assert not any(i.startswith("case_request") for i in ids)
    assert [n["id"] for n in result.not_evaluable] == ["case_request"]
    assert "dispute" in result.not_evaluable[0]["reason"]
    assert _by_id(result)["destination"]["state"] == "missing" and not _by_id(result)["destination"]["paypal_sourced"]


def test_address_comparison_match_mismatch_and_not_declared():
    assert _run(_case())[1].address_comparison["result"] == "match"
    assert _run(_case())[1].address_comparison["basis"] == "postal code"
    mismatch = _run(_case(), {**DECLARED_MATCH, "postal_code": "11000"})[1].address_comparison
    assert mismatch["result"] == "mismatch" and mismatch["paypal_address"]["postal_code"] == "06600"
    assert _run(_case(), None)[1].address_comparison["result"] == "not_declared"


def test_compare_addresses_falls_back_to_city_and_state_then_says_it_cannot_compare():
    paypal = {"city": "Ciudad de México", "state": "CDMX", "country_code": "MX"}
    assert checks.compare_addresses(paypal, {"city": "ciudad de mexico", "state": "cdmx"})["result"] == "match"
    assert checks.compare_addresses(paypal, {"city": "Puebla", "state": "PUE"})["result"] == "mismatch"
    assert checks.compare_addresses(paypal, {"country_code": "MX"})["result"] == "cannot_compare"
    assert checks.compare_addresses({"postal_code": "06 600"}, {"postal_code": "06600"})["result"] == "match"
    assert checks.compare_addresses(None, {"postal_code": "06600"})["result"] == "no_paypal_address"


def test_only_reads_to_the_two_allowed_paths_are_made():
    client, _result = _run(_case())
    assert client.calls and all(m == "GET" for m, _p in client.calls)
    assert all(p.startswith(("/v1/reporting/transactions", "/v1/shipping/trackers")) for _m, p in client.calls)


def test_a_denied_transaction_search_becomes_a_limitation_and_no_address():
    case = _case()
    case["paypal"]["transactions"] = {"__error": 403}
    _client, result = _run(case)
    assert any(lim["source"] == "paypal_transaction_search" for lim in result.limitations)
    assert result.address_comparison["result"] == "no_paypal_address"
    assert result.protection_eligibility is None


def test_a_missing_tracker_is_reported_by_the_checklist_not_hidden():
    case = _case()
    case["paypal"]["trackers"] = {"trackers": []}
    _client, result = _run(case)
    assert _by_id(result)["tracking_number"]["state"] == "missing"
    assert any(lim["source"] == "paypal_tracking" for lim in result.limitations)


def test_an_unknown_rules_country_is_a_limitation_not_a_crash(monkeypatch):
    def boom():
        raise UnknownCountryError("No Seller Protection rule set for country 'ZZ'.")
    monkeypatch.setattr(checks, "cached_rules", boom)
    _client, result = _run(_case())
    assert result.checks == () and result.rules["country"]
    assert any(lim["source"] == "rules" for lim in result.limitations)


def test_to_json_is_serialisable_and_complete():
    import json
    payload = _run(_case())[1].to_json()
    assert set(payload) == {"checks", "not_evaluable", "protection_eligibility", "address_comparison",
                            "limitations", "rules"}
    json.dumps(payload)


def test_the_loader_fake_client_type_is_the_one_used():
    assert isinstance(fake_client(_case()), FakeClient)
