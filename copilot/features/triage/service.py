"""A rule-scoped recommendation, never a decision to send or concede."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from copilot import config
from copilot.core.checklist import Check, _status_category, evaluate
from copilot.core.checklist_input import cached_rules, docs_for_dispute, extracted_docs_for
from copilot.core.context import fetch_context
from copilot.core.disputes import normalize
from copilot.core.evidence_model import (find_unsupported_delivery_assertions,
    find_delivery_claims_citing_conflicting_sources, find_delivery_claims_citing_untrusted_documents,
    untrusted_document_ids)
from copilot.db import repo
from copilot.features.resolution.service import dispute_economics
from copilot.web.dispute_state import buyer_closed_raw


@dataclass(frozen=True)
class Verdict:
    state: str
    text: str
    recommendation: str
    missing: tuple
    cautions: tuple
    requests: tuple
    scope: str
    norm: dict | None = None
    buyer_closed: bool = False       # display only: PayPal says the buyer closed the case (so no resolution link)
    clauses: dict | None = None      # display only: check id -> the clause behind it (COPILOT_FEATURE_CLAUSES)


def classify(checks, rules, docs=(), context=None, unsupported=False):
    by_id = {c.id: c for c in checks}
    required = []
    for cid, rule in rules.checks.items():
        if rule['basis'] != 'published_policy' or rule['kind'] not in ('mandatory', 'conditional'):
            continue
        check = by_id.get(cid)
        if check is None:
            check = Check(cid, rule['label'], 'missing', rule['basis'], rule['citation'], [],
                          'add_document', False, 'This required proof has not been evaluated.')
        # The core deliberately calls a missing street unverifiable; it is conditional on being shown.
        if cid == 'full_address' and not any(
                (d.fields.get('street') or {}).get('confidence') == 'read'
                and (d.fields.get('street') or {}).get('value') for d in docs):
            if check.state != 'conflict':
                continue
        required.append(check)
    missing = tuple(c for c in required if c.state not in ('verified', 'not_applicable'))
    cautions = tuple(c for c in checks if c.basis == 'app_quality_rule'
                     and (c.risk or c.state not in ('verified', 'not_applicable')))
    requests = tuple(c for c in checks if c.basis == 'case_request')
    scope = (f"Seller Protection {rules.country}, effective {rules.effective_date}: the implemented "
             "proof-of-delivery checks only. This does not decide eligibility or what PayPal will accept.")
    conflicts = any(c.state == 'conflict' for c in checks)
    statuses = []
    for doc in docs:
        reading = doc.fields.get('delivery_status') or {}
        if reading.get('confidence') == 'read' and reading.get('value'):
            statuses.append(_status_category(reading['value']))
    if context and context.get('tracking_status'):
        statuses.append(_status_category(context.get('tracking_status')))
    shipping_supported = all(by_id.get(cid) and by_id[cid].state == 'verified'
                             for cid in ('tracking_number', 'carrier'))
    trusted = not untrusted_document_ids(checks, docs)
    partial = (shipping_supported and trusted and not conflicts and not unsupported
               and 'in_transit' in statuses and 'delivered' not in statuses
               and by_id.get('delivery_status') and by_id['delivery_status'].state == 'missing')
    if not missing and not conflicts and not unsupported:
        return Verdict('meets_rule', 'The current evidence meets these proof-of-delivery checks.',
                       'Review each statement and consider sending the supported evidence.',
                       missing, cautions, requests, scope)
    if partial:
        return Verdict('partial', 'The delivery rule is not met, but the shipping proof supports an honest partial reply.',
                       'Review a reply about shipment in transit. Do not state that delivery occurred.',
                       missing, cautions, requests, scope)
    return Verdict('cannot_meet_rule', 'The current evidence does not demonstrate the proof-of-delivery rule.',
                   'Your documents do not show delivery under this rule. Add or correct the proof listed below, or make an offer or message the buyer.',
                   missing, cautions, requests, scope)


def read(client, db_path, dispute_id):
    raw = client.call('GET', f'/v1/customer/disputes/{dispute_id}', 'show_dispute')
    norm = normalize(raw, datetime.now(timezone.utc))
    context = fetch_context(client, norm)
    docs = extracted_docs_for(db_path, dispute_id)
    rules = cached_rules()
    checks = evaluate(context, docs, rules)
    packages = repo.list_packages(db_path, dispute_id)
    claims = repo.list_claims(db_path, packages[-1]['id']) if packages else []
    texts, _ = docs_for_dispute(db_path, dispute_id)
    out = {'proposed_evidence': [{'notes': '', 'claims': claims}]}
    unsupported = bool(find_unsupported_delivery_assertions(out, texts, untrusted_document_ids(checks, docs))
                       or find_delivery_claims_citing_conflicting_sources(claims, checks)
                       or find_delivery_claims_citing_untrusted_documents(claims, checks, docs))
    verdict = replace(classify(checks, rules, docs, context, unsupported), norm=norm, buyer_closed=buyer_closed_raw(raw))
    if config.feature_enabled("clauses"):                            # the flag call guards the feature import (export closure)
        from copilot.features.clauses.service import clauses_for
        verdict = replace(verdict, clauses=clauses_for(rules, checks))
    economics = dispute_economics(client, norm, None)
    return verdict, checks, context.limitations, economics, rules
