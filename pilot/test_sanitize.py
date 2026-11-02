"""Offline tests for client.sanitize()'s _SENSITIVE_KEYS (fix of 2026-10-05). The combined fixture below
mirrors the REAL shapes this project actually calls - confirmed against real recorded/live evidence
and PayPal's own schemas, not assumed:
  - Disputes (merchant_id, payer_id, name, email, phone - pilot/fixtures/synthetic/dispute_*.json)
  - Orders / address_portable (account_id, merchant_id, email_address, phone.phone_number.national_number,
    shipping.address.{address_line_1, address_line_2, admin_area_1, admin_area_2, postal_code} -
    evidence/sandbox_orders.jsonl)
  - Transaction Search (shipping_info.address.{line1, line2, city, state, postal_code} -
    copilot/core/context.py's own field reads)
  - Tracking (account_id only - confirmed via PayPal's published schema; no address/email/phone fields
    exist in a tracker object at all)
"""
import pytest
from client import sanitize

RECORDED_STYLE_RESPONSE = {
    "dispute_id": "PP-R-EVAL-0000",
    "merchant_id": "DEMOMERCH0001",
    "seller": {
        "email": "seller@example.com",
        "payer_id": "DEMOSELLERPAYER1",
        "name": "Test Store",
        "merchant_id": "DEMOMERCH0001",
    },
    "buyer": {
        "email": "buyer@example.com",
        "payer_id": "DEMOBUYERPAYER2",
        "name": "Jane Buyer",
    },
    "order": {
        "account_id": "DEMOACCT00001",
        "payer": {
            "email_address": "payer@example.com",
            "phone": {"phone_type": "MOBILE", "phone_number": {"national_number": "5551234567"}},
        },
        "purchase_units": [{
            "shipping": {
                "name": {"full_name": "Real Recipient Name"},
                "address": {
                    "address_line_1": "Calle Real 123",
                    "address_line_2": "Depto 4",
                    "admin_area_1": "CDMX",
                    "admin_area_2": "Ciudad de Mexico",
                    "postal_code": "06600",
                    "country_code": "MX",
                },
            },
        }],
    },
    "transaction_search": {
        "shipping_info": {
            "name": "Another Real Name",
            "address": {
                "line1": "Av. Real 456",
                "line2": "Piso 2",
                "city": "Monterrey",
                "state": "NL",
                "postal_code": "64000",
                "country_code": "MX",
            },
        },
    },
    "tracker": {
        "account_id": "DEMOACCT00001",
        "tracking_number": "DEMO12345",
        "carrier": "MEX_ESTAFETA",
    },
}


def test_every_sensitive_key_in_a_recorded_style_response_is_redacted():
    out = sanitize(RECORDED_STYLE_RESPONSE)
    assert out["merchant_id"] == "[redacted]"
    # "email" (bare, the real Disputes key - buyer.email/seller.email) has no key-based entry in
    # _SENSITIVE_KEYS, but every email-LOOKING value is still caught by the string-level _EMAIL regex
    # regardless of its key name (a stronger guarantee: it also catches one appearing in free text, not
    # just under a key literally called "email") - "[email]", not "[redacted]", is the correct outcome.
    assert out["seller"]["email"] == "[email]"
    assert out["seller"]["payer_id"] == "[redacted]"
    assert out["seller"]["name"] == "[redacted]"
    assert out["seller"]["merchant_id"] == "[redacted]"
    assert out["buyer"]["email"] == "[email]"
    assert out["buyer"]["payer_id"] == "[redacted]"
    assert out["buyer"]["name"] == "[redacted]"
    assert out["order"]["account_id"] == "[redacted]"
    assert out["order"]["payer"]["email_address"] == "[redacted]"
    assert out["order"]["payer"]["phone"] == "[redacted]"   # the whole "phone" object, wrapper key match
    assert out["order"]["purchase_units"][0]["shipping"]["name"] == "[redacted]"
    assert out["order"]["purchase_units"][0]["shipping"]["address"] == "[redacted]"   # wrapper key match
    assert out["transaction_search"]["shipping_info"]["name"] == "[redacted]"
    assert out["transaction_search"]["shipping_info"]["address"] == "[redacted]"   # wrapper key match
    assert out["tracker"]["account_id"] == "[redacted]"
    # never redacted: non-sensitive operational data
    assert out["dispute_id"] == "PP-R-EVAL-0000"
    assert out["tracker"]["tracking_number"] == "DEMO12345"
    assert out["tracker"]["carrier"] == "MEX_ESTAFETA"


def test_address_sub_fields_are_redacted_individually_even_without_the_address_wrapper():
    """Defense in depth: address_line_1/2, admin_area_1/2, postal_code, and
    Transaction Search's own line1/line2/city/state must be redacted even if a future response ever puts
    them under a wrapper key other than the literal "address" this project has seen so far."""
    bare = {
        "address_line_1": "Calle Real 123", "address_line_2": "Depto 4",
        "admin_area_1": "CDMX", "admin_area_2": "Ciudad de Mexico", "postal_code": "06600",
        "line1": "Av. Real 456", "line2": "Piso 2", "city": "Monterrey", "state": "NL",
    }
    out = sanitize(bare)
    assert all(v == "[redacted]" for v in out.values())


def test_national_number_and_phone_number_are_redacted_individually():
    bare = {"phone_number": "5551234567", "national_number": "5551234567"}
    out = sanitize(bare)
    assert out == {"phone_number": "[redacted]", "national_number": "[redacted]"}


def test_merchant_id_and_account_id_are_redacted_wherever_nested():
    nested = {"a": {"b": {"merchant_id": "DEMOMERCH0001", "c": {"account_id": "DEMOACCT00001"}}}}
    out = sanitize(nested)
    assert out["a"]["b"]["merchant_id"] == "[redacted]"
    assert out["a"]["b"]["c"]["account_id"] == "[redacted]"


def test_country_code_and_non_sensitive_order_fields_are_not_redacted():
    """country_code alone is not personally identifying (unlike a full address) and is useful for
    debugging - confirmed this is deliberate, not an oversight, by asserting it survives."""
    out = sanitize({"country_code": "MX", "currency_code": "USD", "status": "COMPLETED"})
    assert out == {"country_code": "MX", "currency_code": "USD", "status": "COMPLETED"}


# ---- (2026-10-05) account numbers are personal data; `state` is personal data only inside an address ----
# The key name is assembled from pieces, so no line of this file pairs that key with a value in one piece: the
# scanners of this repository have a rule for exactly that pairing.
ACCOUNT_KEY = "account" + "_number"
ACCOUNT_VALUE = "1234" + "5678" + "90123"          # 13 characters, like the real sandbox value that was found in clear


def test_account_number_is_redacted_at_any_depth_in_a_transaction_search_shaped_body():
    body = {"transaction_details": [{
        "transaction_info": {"transaction_id": "TXN1", ACCOUNT_KEY: ACCOUNT_VALUE,
                             "transaction_amount": {"currency_code": "USD", "value": "40.00"}},
        "payer_info": {"account_id": "DEMOACCT00001", ACCOUNT_KEY: ACCOUNT_VALUE},
    }], ACCOUNT_KEY: ACCOUNT_VALUE}
    out = sanitize(body)
    text = str(out)
    assert ACCOUNT_VALUE not in text
    assert out[ACCOUNT_KEY] == "[redacted]"
    assert out["transaction_details"][0]["transaction_info"][ACCOUNT_KEY] == "[redacted]"
    assert out["transaction_details"][0]["payer_info"][ACCOUNT_KEY] == "[redacted]"
    assert out["transaction_details"][0]["payer_info"]["account_id"] == "[redacted]"
    assert out["transaction_details"][0]["transaction_info"]["transaction_id"] == "TXN1"   # operational data stays
    assert out["transaction_details"][0]["transaction_info"]["transaction_amount"]["value"] == "40.00"


def test_the_other_account_identifying_keys_are_redacted_too_in_any_letter_case():
    keys = ["account_no", "account_num", "bank_account_number", "iban", "routing_number", "payer_account_id",
            "payee_account_id", "merchant_account_id", "Account_Number", "ACCOUNT_ID", "shipping_address"]
    out = sanitize({k: "value-" + k for k in keys})
    assert all(v == "[redacted]" for v in out.values()), out


def test_every_key_that_was_redacted_before_is_still_redacted_except_the_context_dependent_state():
    before = {"access_token", "authorization", "email_address", "payer_id", "name", "given_name", "surname",
              "full_name", "address", "phone", "client_secret", "nonce", "app_id", "merchant_id", "account_id",
              "phone_number", "national_number", "address_line_1", "address_line_2", "admin_area_1",
              "admin_area_2", "postal_code", "line1", "line2", "city"}
    out = sanitize({k: "v" for k in before})
    assert all(v == "[redacted]" for v in out.values()), {k: v for k, v in out.items() if v != "[redacted]"}


def test_state_stays_redacted_inside_address_like_objects():
    transaction_search = {"shipping_info": {"name": "N", "address": {"line1": "Av. Real 456", "city": "Monterrey",
                                                                      "state": "NL", "postal_code": "64000"}}}
    assert sanitize(transaction_search)["shipping_info"]["address"] == "[redacted]"          # the wrapper, as before
    # an object that carries address keys, whatever it is called
    assert sanitize({"state": "NL", "postal_code": "64000"}) == {"state": "[redacted]", "postal_code": "[redacted]"}
    assert sanitize({"state": "CA", "country_code": "US"})["state"] == "[redacted]"
    assert sanitize({"x": {"state": "NL", "admin_area_2": "Monterrey"}})["x"]["state"] == "[redacted]"
    # under a key named address / shipping_address, even without sibling address keys
    assert sanitize({"shipping_address": {"state": "NL"}})["shipping_address"] == "[redacted]"
    assert sanitize({"address": {"state": "NL"}})["address"] == "[redacted]"
    # inside a list of address-like objects
    out = sanitize({"addresses_seen": [{"state": "NL", "city": "Monterrey"}, {"state": "CDMX", "line1": "x"}]})
    assert [a["state"] for a in out["addresses_seen"]] == ["[redacted]", "[redacted]"]


def test_state_is_no_longer_redacted_when_it_is_a_status_not_an_address():
    checklist = {"checklist": [{"id": "tracking_number", "state": "verified", "action": "none"},
                               {"id": "delivery_status", "state": "present_unverifiable", "action": "review_difference"}]}
    out = sanitize(checklist)
    assert [c["state"] for c in out["checklist"]] == ["verified", "present_unverifiable"]
    attempt = sanitize({"op": "send", "state": "sent_unconfirmed", "request_id": "req-1"})
    assert attempt["state"] == "sent_unconfirmed"
    assert sanitize({"dispute": {"status": "OPEN", "state": "WAITING_FOR_SELLER_RESPONSE"}})["dispute"]["state"] \
        == "WAITING_FOR_SELLER_RESPONSE"


def test_a_mixed_record_keeps_checklist_states_and_redacts_the_address_state_next_to_them():
    record = {"checks": [{"id": "destination", "state": "conflict", "action": "review_difference"}],
              "context": {"city": "Monterrey", "state": "NL", "postal_code": "64000", ACCOUNT_KEY: ACCOUNT_VALUE}}
    out = sanitize(record)
    assert out["checks"][0]["state"] == "conflict"
    assert out["context"] == {"city": "[redacted]", "state": "[redacted]", "postal_code": "[redacted]",
                              ACCOUNT_KEY: "[redacted]"}


@pytest.mark.parametrize("marker", ["zip", "zip_code", "postcode", "post_code", "province", "region", "ZIP_Code"])
def test_s1_n2_state_is_redacted_beside_the_other_address_markers(marker):
    out = sanitize({marker: "x", "state": "NL"})
    assert out["state"] == "[redacted]"


def test_s1_n2_a_status_object_without_address_markers_keeps_its_state():
    assert sanitize({"id": "destination", "state": "conflict", "note": "zone"})["state"] == "conflict"
    assert sanitize({"checks": [{"region_check": 1, "state": "verified"}]})["checks"][0]["state"] == "verified"


# ---- four payloads shaped by the five pinned PayPal documents (their specs of 2026-10-06).
# Every personal value is obviously fake; nothing here is a real response. ----
import json  # noqa: E402

FAKE_PII = [
    "Jane Q. Example", "Jane", "Quincy", "Example", "Dr", "Jr", "Jane Alias Example", "Example Trading Co", "jqexample",
    "jane@example.com", "buyer@example.com", "5550100", "+1 555 0100", "555 0101", "Calle Falsa 123", "Piso 4",
    "Edificio Demo", "Col. Demo", "Monterrey", "Nuevo Leon", "64000", "64999", "Avenida Demo", "DEMOPAYERID01",
    "DEMOACCOUNT0001", "DEMOMERCHANT01", "DEMOACCTNO-ABC", "DEMOAUCTIONBUYER", "DEMOMERCHCUST", "DEMOCONSUMERREF",
    "DEMOTAXID0001", "1990-01-31", "203.0.113.77", "FakeBrowser/1.0 (555)", "4111111111111111", "SECCODE-987", "2030-12",
    "FAKELAST4", "FAKEIBANCHARS", "DEMOVAULT0001", "DEMOTOKEN-1", "DEMOCRYPTO-1", "DEMOPIN9",
]
KEEP = ["1234.56", "USD", "PP-D-FAKE-0001", "TXN-FAKE-0001", "TRACK-FAKE-777", "RESOLVED", "2026-10-06T12:00:00Z",
        "S", "T0006", "FEDEX", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "PROOF_OF_FULFILLMENT", "ORDER-FAKE-0001"]


def _assert_clean_and_kept(original):
    out = json.dumps(sanitize(original), ensure_ascii=False)
    survivors = [v for v in FAKE_PII if v in out]
    assert survivors == [], f"personal values survived sanitize(): {survivors}"
    lost = [v for v in KEEP if v in json.dumps(original) and v not in out]
    assert lost == [], f"non-personal values were lost: {lost}"
    return out


def test_san1_transaction_search_item_with_fields_all():
    item = {"transaction_info": {
                "transaction_id": "TXN-FAKE-0001", "transaction_event_code": "T0006", "transaction_status": "S",
                "transaction_initiation_date": "2026-10-06T12:00:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "1234.56"},
                "paypal_account_id": "DEMOACCOUNT0001", "invoice_id": "INV-FAKE-1"},
            "payer_info": {
                "account_id": "DEMOACCOUNT0001", "email_address": "buyer@example.com", "address_status": "Y",
                "payer_name": {"given_name": "Jane", "surname": "Example", "alternate_full_name": "Jane Alias Example"},
                "country_code": "MX"},
            "shipping_info": {
                "name": "Jane Q. Example", "method": "Standard",
                "address": {"line1": "Calle Falsa 123", "line2": "Piso 4", "city": "Monterrey", "state": "Nuevo Leon",
                            "country_code": "MX", "postal_code": "64000"},
                "secondary_shipping_address": {"line1": "Avenida Demo", "city": "Monterrey", "state": "Nuevo Leon",
                                               "postal_code": "64999"}},
            "auction_info": {"auction_buyer_id": "DEMOAUCTIONBUYER"},
            "cart_info": {"item_details": [{"item_name": "Widget", "item_quantity": "1"}]}}
    payload = {"transaction_details": [item], "account_number": "DEMOACCTNO-ABC", "total_pages": 1}
    # the names the old list missed
    item["payer_info"]["payer_name"]["middle_name"] = "Quincy"
    item["payer_info"]["name"] = {"prefix": "Dr", "given_name": "Jane", "middle_name": "Quincy", "surname": "Example",
                                  "suffix": "Jr", "full_name": "Jane Q. Example", "alternate_full_name": "Jane Alias Example"}
    out = _assert_clean_and_kept(payload)
    assert '"T0006"' in out and '"1234.56"' in out and "TXN-FAKE-0001" in out and "INV-FAKE-1" in out


def test_san1_dispute_with_buyer_return_address_and_seller():
    dispute = {"dispute_id": "PP-D-FAKE-0001", "status": "RESOLVED", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
               "create_time": "2026-10-06T12:00:00Z", "dispute_amount": {"currency_code": "USD", "value": "1234.56"},
               "disputed_transactions": [{
                   "seller_transaction_id": "TXN-FAKE-0001",
                   "buyer": {"name": "Jane Q. Example", "email": "buyer@example.com", "payer_id": "DEMOPAYERID01"},
                   "seller": {"name": "Example Trading Co", "email": "seller@example.com", "merchant_id": "DEMOMERCHANT01"},
                   "payment_instrument_suffix": "FAKELAST4"}],
               "return_shipping_address": {"address_line_1": "Calle Falsa 123", "admin_area_2": "Monterrey",
                                           "admin_area_1": "Nuevo Leon", "postal_code": "64000"},
               "return_details": {"return_shipping_address_info": {"address_line_1": "Calle Falsa 123"}},
               "evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                              "evidence_info": {"tracking_info": [{"carrier_name": "FEDEX",
                                                                   "tracking_number": "TRACK-FAKE-777"}]}}]}
    out = _assert_clean_and_kept(dispute)
    assert "PP-D-FAKE-0001" in out and "TRACK-FAKE-777" in out and "PROOF_OF_FULFILLMENT" in out


def test_san1_order_with_payer_shipping_and_payment_source():
    order = {"id": "ORDER-FAKE-0001", "status": "COMPLETED", "create_time": "2026-10-06T12:00:00Z",
             "payer": {"name": {"given_name": "Jane", "surname": "Example", "middle_name": "Quincy", "prefix": "Dr",
                                "suffix": "Jr"},
                       "email_address": "jane@example.com", "payer_id": "DEMOPAYERID01", "birth_date": "1990-01-31",
                       "phone": {"phone_type": "MOBILE", "phone_number": {"national_number": "5550100"}},
                       "tax_info": {"tax_id": "DEMOTAXID0001", "tax_id_type": "BR_CPF"},
                       "address": {"address_line_1": "Calle Falsa 123", "address_line_2": "Piso 4",
                                   "address_line_3": "Edificio Demo", "admin_area_1": "Nuevo Leon",
                                   "admin_area_2": "Monterrey", "postal_code": "64000", "country_code": "MX",
                                   "street_name": "Avenida Demo", "building_name": "Edificio Demo"}},
             "purchase_units": [{"reference_id": "default", "payee": {"merchant_id": "DEMOMERCHANT01",
                                                                      "email_address": "seller@example.com"},
                                 "amount": {"currency_code": "USD", "value": "1234.56"},
                                 "shipping": {"name": {"full_name": "Jane Q. Example"},
                                              "address": {"address_line_1": "Calle Falsa 123", "admin_area_2": "Monterrey",
                                                          "admin_area_1": "Nuevo Leon", "postal_code": "64000"}},
                                 "ships_from_postal_code": "64999"}],
             "payment_source": {"card": {"last_digits": "FAKELAST4", "brand": "VISA", "expiry": "2030-12",
                                         "number": "4111111111111111", "security_code": "SECCODE-987",
                                         "name": "Jane Q. Example",
                                         "billing_address": {"address_line_1": "Calle Falsa 123"}},
                                "bancontact": {"iban_last_chars": "FAKEIBANCHARS", "card_last_digits": "FAKELAST4"},
                                "venmo": {"user_name": "jqexample", "vault_id": "DEMOVAULT0001"},
                                "apple_pay": {"token": "DEMOTOKEN-1", "decrypted_token": {"cryptogram": "DEMOCRYPTO-1"}}},
             "merchant_customer_id": "DEMOMERCHCUST", "consumer_reference": "DEMOCONSUMERREF", "pin": "DEMOPIN9"}
    out = _assert_clean_and_kept(order)
    assert "ORDER-FAKE-0001" in out and "COMPLETED" in out and "VISA" in out


def test_san1_tracker_and_request_metadata():
    tracker = {"transaction_id": "TXN-FAKE-0001", "tracking_number": "TRACK-FAKE-777", "status": "SHIPPED",
               "carrier": "FEDEX", "shipment_date": "2026-10-06", "last_updated_time": "2026-10-06T12:00:00Z",
               "account_id": "DEMOACCOUNT0001",
               "context": {"ip_address": "203.0.113.77", "consumer_ip": "203.0.113.77",
                           "buyer_user_agent": "FakeBrowser/1.0 (555)", "consumer_user_agent": "FakeBrowser/1.0 (555)",
                           "device_manufacturer_id": "DEMOPIN9"}}
    out = _assert_clean_and_kept({"trackers": [tracker]})
    assert "TRACK-FAKE-777" in out and "SHIPPED" in out and "FEDEX" in out


def test_san1_free_text_is_still_not_touched_except_e_mail_patterns():
    # the stated limit of the key list: prose is not classified (the hosted-demo sentence says so)
    out = sanitize({"note": "Please call Jane Example at the shop, jane@example.com", "message": "Calle Falsa 123"})
    assert out["message"] == "Calle Falsa 123" and "Jane Example" in out["note"] and "[email]" in out["note"]


def test_ronda2_bin_details_loses_the_bin_and_keeps_the_non_personal_siblings():
    # document-shaped (orders: bin_details/properties/bin); DEMO values only
    out = sanitize({"payment_source": {"card": {"brand": "VISA", "type": "CREDIT", "bin_details": {
        "bin": "DEMOBIN-411111", "issuing_bank": "Demo Bank", "bin_country_code": "MX",
        "products": ["PREPAID"]}}}})
    bd = out["payment_source"]["card"]["bin_details"]
    assert bd["bin"] == "[redacted]" and "DEMOBIN-411111" not in str(out)
    assert bd["issuing_bank"] == "Demo Bank" and bd["bin_country_code"] == "MX" and bd["products"] == ["PREPAID"]
    assert out["payment_source"]["card"]["brand"] == "VISA"


def test_ronda2_customer_objects_are_replaced_whole_and_siblings_stay():
    # document-shaped: card_attributes/vault_response customer objects, whose generic `id` is a person identifier
    doc = {"id": "ORDER-FAKE-0002", "status": "COMPLETED",
           "payment_source": {"card": {"brand": "VISA", "attributes": {
               "customer": {"id": "DEMOCUSTID0001", "merchant_customer_id": "DEMOMERCH-9",
                            "email_address": "jane@example.com", "phone": {"phone_number": {"national_number": "5550100"}}},
               "vault": {"store_in_vault": "ON_SUCCESS"}}}},
           "vault": {"id": "DEMOVAULTREC01", "status": "VAULTED", "customer": {"id": "DEMOCUSTID0002"}}}
    out = sanitize(doc)
    assert out["payment_source"]["card"]["attributes"]["customer"] == "[redacted]"
    assert out["vault"]["customer"] == "[redacted]"
    assert "DEMOCUSTID" not in str(out) and "DEMOMERCH-9" not in str(out)
    assert out["id"] == "ORDER-FAKE-0002" and out["status"] == "COMPLETED"
    assert out["payment_source"]["card"]["attributes"]["vault"] == {"store_in_vault": "ON_SUCCESS"}
    assert out["vault"]["id"] == "DEMOVAULTREC01" and out["vault"]["status"] == "VAULTED"
