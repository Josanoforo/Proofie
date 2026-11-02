"""P2: read-only case-bound gap and source projection; no parallel persistence."""
import hashlib
import json
from urllib.parse import quote


def resolve_inputs(*, dispute_id, package, capture_id, norm, manual_fields, readiness_record=None, shipment_record=None, checks=None, locale=None, editable=False):
    if not isinstance(norm, dict) or norm.get('dispute_id') != dispute_id or not isinstance(package, dict) or package.get('dispute_id') != dispute_id or type(package.get('id')) is not int:
        return {'ok': False, 'error': 'Case or package reference is unavailable.', 'fields': {}, 'gaps': []}
    transaction_ids = norm.get('seller_transaction_ids')
    linked = bool(capture_id and isinstance(transaction_ids, list) and capture_id in transaction_ids)
    if capture_id and not linked:
        return {'ok': False, 'error': 'The sale is not linked to this case.', 'fields': {}, 'gaps': []}
    if readiness_record and (not isinstance(readiness_record, dict) or not linked or readiness_record.get('dispute_id') != dispute_id or readiness_record.get('capture_id') != capture_id):
        return {'ok': False, 'error': 'The sale record is linked to a different case.', 'fields': {}, 'gaps': []}
    if shipment_record and (not isinstance(shipment_record, dict) or not linked or shipment_record.get('capture_id') != capture_id):
        return {'ok': False, 'error': 'The shipment is linked to a different sale.', 'fields': {}, 'gaps': []}
    manual_fields = manual_fields if isinstance(manual_fields, dict) else {}
    terms = readiness_record.get('terms', {}) if isinstance(readiness_record, dict) else {}
    terms = terms if isinstance(terms, dict) else {}
    shipment = shipment_record.get('tracker', {}) if isinstance(shipment_record, dict) else {}
    shipment = shipment if isinstance(shipment, dict) else {}
    base = f"/disputes/{quote(dispute_id, safe='')}/packages/{package['id']}"
    fields = {}
    for name in ('tracking_number', 'carrier', 'country', 'locale', 'amount'):
        candidates = []
        def add(value, kind, ref, status):
            if value is not None and value != '':
                candidates.append({'value': value, 'source_kind': kind, 'source_ref': ref, 'status': status})
        if name in ('tracking_number', 'carrier'):
            entry = manual_fields.get(name)
            add(entry.get('value') if isinstance(entry, dict) and entry.get('confidence') == 'read' and isinstance(entry.get('value'), str) and entry.get('value').strip() else None, 'manual_fields', f'case:{dispute_id}', 'confirmed_by_seller')
            add(terms.get(name), 'readiness', f'{dispute_id}:{capture_id}', 'confirmed_by_seller')
            add(shipment.get(name), 'shipment_request', f'capture:{capture_id}', 'suggested')
        elif name == 'country':
            address = terms.get('ship_to') if isinstance(terms.get('ship_to'), dict) else {}
            add(address.get('country'), 'readiness', f'{dispute_id}:{capture_id}', 'confirmed_by_seller')
        elif name == 'locale':
            if locale in ('en', 'es'): add(locale, 'seller_selection', f'case:{dispute_id}', 'confirmed_by_seller')
        elif name == 'amount' and type(norm.get('amount_cents')) is int and isinstance(norm.get('currency'), str) and norm['currency']:
            add({'minor_units': norm['amount_cents'], 'currency': norm['currency']}, 'case_observation', f'case:{dispute_id}', 'recorded_provider')
        distinct = {json.dumps(c['value'], sort_keys=True, ensure_ascii=False) for c in candidates}
        chosen = candidates[0] if candidates else None
        conflict = len(distinct) > 1
        manual_choice = bool(chosen and chosen['source_kind'] == 'manual_fields')
        status = 'missing' if not chosen else 'conflict' if conflict and not manual_choice else chosen['status']
        href = (base + '#field-' + name) if editable is True and name in ('tracking_number', 'carrier') else None
        fields[name] = {'value': chosen['value'] if chosen and status != 'conflict' else None,
                        'source_kind': chosen['source_kind'] if chosen else None, 'source_ref': chosen['source_ref'] if chosen else None,
                        'status': status, 'conflict': conflict, 'candidates': candidates, 'href': href}
    for name in ('tracking_number', 'carrier'):
        f = fields[name]
        if editable is True and f['status'] == 'suggested' and f['source_kind'] == 'shipment_request' and shipment_record.get('prefill_allowed') is True and not any(fields[n]['status'] in ('confirmed_by_seller', 'conflict') for n in ('tracking_number', 'carrier')):
            f['href'] = base + '?use_tracking=1#field-' + name
    gaps = [{'field': name, **row} for name, row in fields.items() if row['conflict'] or row['status'] in ('missing', 'conflict', 'suggested')]
    identity = {'case': dispute_id, 'package': package['id'], 'capture': capture_id,
                'fields': {name: {k: v for k, v in row.items() if k != 'href'} for name, row in fields.items()}}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'ok': True, 'case_id': dispute_id, 'package_id': package['id'], 'capture_id': capture_id, 'fields': fields, 'gaps': gaps, 'fingerprint': fingerprint}


def _current_case(db_path, dispute_id, package_id, norm, *, capture_id=None, checks=None, locale=None, editable=None):
    """Read local records only. Caller provides a previously observed normalized case."""
    from copilot.db import repo
    from copilot import config
    from copilot.web.security import validate_dispute_id
    if validate_dispute_id(dispute_id) or type(package_id) is not int or not isinstance(norm, dict) or norm.get('dispute_id') != dispute_id:
        return {'ok': False, 'error': 'Case or package reference is unavailable.', 'fields': {}, 'gaps': []}
    package = repo.get_package(db_path, package_id) if type(package_id) is int else None
    if not package or package.get('dispute_id') != dispute_id:
        return {'ok': False, 'error': 'Case or package reference is unavailable.', 'fields': {}, 'gaps': []}
    if editable is None:
        # Presentation only: the package POST retains all its own authority checks.
        # Pending/attempted and unrecognized records are not known unsent.
        import sqlite3
        from copilot.web import status
        try:
            record = status.send_record(db_path, dispute_id, package)
            editable = (isinstance(record, status.SendRecord)
                        and not status.is_sent_record(record)
                        and (record.state in ('refused', 'blocked')
                             or (record.state == 'none' and record.attempt is None)))
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            editable = False
    manual = repo.get_manual_fields(db_path, dispute_id)
    if not isinstance(norm, dict) or not capture_id or not isinstance(norm.get('seller_transaction_ids'), list) or capture_id not in norm['seller_transaction_ids']:
        return resolve_inputs(dispute_id=dispute_id, package=package, capture_id=capture_id, norm=norm, manual_fields=manual, checks=checks, locale=locale, editable=editable)
    readiness = None
    if config.feature_enabled('sale_readiness'):
        from copilot.features.sale_readiness.service import load_case_terms
        readiness = load_case_terms(dispute_id, capture_id)
    shipment = None
    if config.feature_enabled('fulfilment'):
        from copilot.web.routes_package import _tracker_on_file
        saved = _tracker_on_file(db_path, {**norm, 'seller_transaction_ids': [capture_id]})
        if saved:
            shipment = {'capture_id': capture_id, 'prefill_allowed': True,
                        'tracker': {'tracking_number': saved['number'], 'carrier': saved['carrier_code']}}
    return resolve_inputs(dispute_id=dispute_id, package=package, capture_id=capture_id, norm=norm, manual_fields=manual, readiness_record=readiness, shipment_record=shipment, checks=checks, locale=locale, editable=editable)


def current_case(db_path, dispute_id, package_id, norm, *, capture_id=None, checks=None, locale=None, editable=None):
    import sqlite3
    try:
        return _current_case(db_path, dispute_id, package_id, norm, capture_id=capture_id, checks=checks, locale=locale, editable=editable)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {'ok': False, 'error': 'Saved case sources are unavailable.', 'fields': {}, 'gaps': []}
