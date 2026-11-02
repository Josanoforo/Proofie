from copilot.features.judge_walkthrough.service import synthetic_identity, _identity_from_records

def test_exact_capture_join_and_no_ambiguous_association():
    result = synthetic_identity()
    assert result["order_id"] == "8RX26735CU316042V"
    assert result["capture_id"] == "DEMOTXN0000000001"
    assert result["dispute_id"] == "PP-R-DEMO-0000001"
    order = "PayPal capture id: MATCH\n"
    assert _identity_from_records(order, {"disputed_transactions": [{"seller_transaction_id":"OTHER"}]}) is None
    assert _identity_from_records(order, {"disputed_transactions": [{"seller_transaction_id":"MATCH"}, {"seller_transaction_id":"MATCH"}]}) is None
