"""One closed table of seller words for field and choice keys.

Replaces labels derived with `replace('_', ' ')`. The vocabulary is closed: shipment and delivery-fact fields plus
feature-owned reconciliation columns and seller-selected choices. A key outside the table never prints as itself: it
reads UNKNOWN_FIELD_WORD (the rule of status.py: never a raw enum). Display only. Templates get `field_word` as a Jinja
global and as a filter.
"""

UNKNOWN_FIELD_WORD = "A field Proofie does not name"

FIELD_WORDS = {
    # shipment fields of the checks and of the manual-fields form
    "tracking_number": "Tracking number", "carrier": "Carrier", "delivery_status": "Delivery status",
    "delivery_date": "Delivery date", "ship_date": "Ship date", "postal_code": "Postal code", "city": "City",
    "state": "State", "street": "Street", "receiver_name": "Receiver name", "signature": "Signature",
    "delivery_type": "Delivery type", "capture_date": "Date the proof was captured",
    # the fields the case-gap list names besides the shipment fields
    "country": "Country", "locale": "Language", "amount": "Amount",
    # keys of the saved delivery-facts records (what you entered / what PayPal returned)
    "tracking": "Tracking number", "status": "Status", "shipment": "Ship date",
    "line1": "Address line 1", "line2": "Address line 2", "country_code": "Country",
    # keys a statement source can name besides the shipment fields (the approval page's "Field read")
    "extracted_text": "Text read from the document", "dispute_id": "Dispute", "amount_cents": "Amount",
    "currency": "Currency", "reason": "Reason",
    # closed feature-owned reconciliation columns and reminder choices
    "capture_id": "Sale capture ID", "sale_amount_cents": "Sale amount", "sale_currency": "Sale currency",
    "paypal_refund_cents": "PayPal refund", "paypal_refund_currency": "Refund currency",
    "paypal_applied_dispute_fee_cents": "PayPal applied dispute fee", "applied_fee_status": "Applied fee status",
    "account_cliff_fee_estimate_cents": "Account Cliff fee estimate", "fee_estimate_currency": "Estimate currency",
    "fee_estimate_status": "Fee estimate status", "ledger_record_count": "Ledger records",
    "MX": "Mexico", "US": "United States", "unknown": "Unknown",
    "under_25": "Under 25", "25_to_100": "$25 to $100", "100_to_500": "$100 to $500",
    "over_500": "Over $500", "topic_delivery": "Delivery", "topic_tracking": "Tracking",
    "topic_packaging": "Packaging", "topic_item_detail": "Item detail", "topic_other": "Other",
    "electronics": "Electronics", "clothing": "Clothing", "crafts": "Crafts",
    "auto_parts": "Auto parts", "shelf_stable_food": "Shelf-stable food",
    "check_delivery_window": "Check delivery window", "save_tracking_receipt": "Save tracking receipt",
    "photograph_package": "Photograph package", "record_item_details": "Record item details",
    "keep_receipt": "Keep receipt",
}


def field_word(key) -> str:
    """The seller words for a field key; any unknown, missing or non-text key gives UNKNOWN_FIELD_WORD."""
    if isinstance(key, str):
        return FIELD_WORDS.get(key, UNKNOWN_FIELD_WORD)
    return UNKNOWN_FIELD_WORD
