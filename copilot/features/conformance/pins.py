"""Public document identities and the minimum validation dependency roots."""
PINS = {
    "disputes": {
        "file": "live_customer-disputes_schema.json", "sha256": "fee5e04ab606f8961b95744e65c4189fc7c4613e79ba2f944d2deab79e834330", "version": "1.12",
        "url": "https://developer.paypal.com/api/customer-disputes/v1/schema.json",
        "roots": ["/components/schemas/" + n for n in ("make_offer", "evidence_list", "evidence", "tracking_info", "dispute", "dispute_search", "subsequent_action", "fund_movement")]
        + ["/components/requestBodies/evidences", "/paths/~1v1~1customer~1disputes~1{id}~1send-message/post/requestBody"],
    },
    "tracking": {
        "file": "live_tracking_schema.json", "sha256": "7d6f992eb95d6ab4be23755a00455cd3d4fbcf4da0d2abafe72e934529f5f0f1", "version": "1.10",
        "url": "https://developer.paypal.com/api/tracking/v1/schema.json",
        "roots": ["/components/schemas/tracker_collection", "/components/schemas/tracker", "/components/requestBodies/tracker_collection"],
    },
    "transactions": {
        "file": "live_transaction-search_schema.json", "sha256": "85909abeb33b12e184a11d404beb6494ad51115523e6d534ee26ef865d3f9a6d", "version": "1.10",
        "url": "https://developer.paypal.com/api/transaction-search/v1/schema.json",
        "roots": ["/components/schemas/search_response", "/paths/~1v1~1reporting~1transactions/get/parameters"],
    },
    "webhooks": {
        "file": "live_webhooks_schema.json", "sha256": "a10f9d6d44b4732393bf07001979110e0f6a584efad07e31d0d4ad76d6300ca3", "version": "1.11",
        "url": "https://developer.paypal.com/api/webhooks/v1/schema.json",
        "roots": ["/components/schemas/verify_webhook_signature", "/components/schemas/verify_webhook_signature_response", "/components/schemas/event_list", "/paths/~1v1~1notifications~1webhooks-events/get/parameters"],
    },
    "orders": {
        "file": "live_orders_schema.json", "sha256": "5bcb2242a4158a307dceff74f74352a078777f9afb9b0fb407ce10e392ee96b7", "version": "2.36",
        "url": "https://developer.paypal.com/api/orders/v2/schema.json", "roots": ["/components/schemas/order"],
    },
}
