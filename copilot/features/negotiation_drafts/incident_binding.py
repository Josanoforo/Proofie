"""P4-compatible deterministic binding; caller validates case membership and checks before preview."""
import hashlib
import json


def incident_fingerprint(*, gap_context, incident, locale, seller_text, readiness_record, shipment_record):
    if not isinstance(gap_context, dict) or gap_context.get('ok') is not True or not gap_context.get('fingerprint'):
        raise ValueError('A current case-bound source projection is required.')
    if incident not in ('delivered_not_received', 'delay', 'missing', 'buyer_question') or locale not in ('en', 'es'):
        raise ValueError('An explicit supported incident and language are required.')
    if not isinstance(seller_text, str) or not seller_text.strip() or len(seller_text) > 2000:
        raise ValueError('A seller-reviewed draft is required.')
    value = {'case_id': gap_context['case_id'], 'package_id': gap_context['package_id'],
             'capture_id': gap_context.get('capture_id'), 'gap_fingerprint': gap_context['fingerprint'],
             'incident': incident, 'locale': locale, 'seller_text': seller_text,
             'readiness': readiness_record, 'shipment': shipment_record}
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    except (ValueError, TypeError) as exc:
        raise ValueError('Incident inputs cannot be bound safely.') from exc
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
