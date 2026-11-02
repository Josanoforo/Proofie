import re
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient
from _hosted import authenticated_client
from jinja2 import ChoiceLoader, DictLoader

from copilot import config
from copilot.app import create_app
from copilot.core.checklist import Check, evaluate, load_rules
from copilot.db import repo
from copilot.features.resolution.economics import economics_line
from copilot.features.triage.service import classify, read
from copilot.fixtures.m1 import loader
from copilot.web.dispute_state import REVIEWING_NEUTRAL_TEXT
from copilot.web.routes_disputes import get_client, templates


def complete(country):
    case = loader.load_case('c01_complete')
    rules = load_rules(country=country)
    docs, context = loader.build_docs(case), loader.build_context(case)
    return evaluate(context, docs, rules, today=loader.today_of(case)), rules, docs, context


def _visible_text_outside_details(node):
    """Reading text on the full page, excluding each disclosure subtree structurally."""
    parts = []

    def visit(current):
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag != 'details':
                visit(child)

    visit(node)
    return ' '.join(' '.join(parts).split())


@pytest.mark.parametrize('country', ['MX', 'US'])
def test_real_complete_fixture_meets_rule(country):
    checks, rules, docs, context = complete(country)
    result = classify(checks, rules, docs, context)
    assert result.state == 'meets_rule'
    assert not result.missing
    assert country in result.scope


@pytest.mark.parametrize('country', ['MX', 'US'])
@pytest.mark.parametrize('cid', ['tracking_number', 'delivery_status', 'delivery_date', 'destination', 'full_address', 'signature'])
@pytest.mark.parametrize('state', ['verified', 'missing', 'present_unverifiable', 'conflict', 'not_applicable'])
def test_every_required_class_and_state(country, cid, state):
    checks, rules, docs, context = complete(country)
    checks = [replace(c, state=state) if c.id == cid else c for c in checks]
    result = classify(checks, rules, docs, context)
    assert result.state == ('meets_rule' if state in ('verified', 'not_applicable') else 'cannot_meet_rule')
    if state not in ('verified', 'not_applicable'):
        assert cid in [c.id for c in result.missing]


@pytest.mark.parametrize('country', ['MX', 'US'])
@pytest.mark.parametrize('name,expected', [('c05_in_transit', 'partial'), ('c03_other_order', 'cannot_meet_rule'),
                                        ('c06_illegible', 'cannot_meet_rule')])
def test_real_partial_conflicting_and_illegible_shapes(country, name, expected):
    case = loader.load_case(name)
    rules, docs, context = load_rules(country=country), loader.build_docs(case), loader.build_context(case)
    checks = evaluate(context, docs, rules, today=loader.today_of(case))
    result = classify(checks, rules, docs, context)
    assert result.state == expected
    if expected == 'partial':
        assert 'delivery rule is not met' in result.text
        assert 'delivery_status' in [c.id for c in result.missing]
        assert 'Do not state that delivery occurred' in result.recommendation


@pytest.mark.parametrize('country', ['MX', 'US'])
def test_no_street_case_request_and_quality_are_not_policy_failure(country):
    checks, rules, docs, context = complete(country)
    docs[0].fields.pop('street', None)
    checks = evaluate(context, docs, rules, today=date(2026, 10, 1))
    request = Check('case_request:PROOF_OF_DELIVERY', 'Requested proof', 'missing', 'case_request',
                    'Case response', [], 'add_document', False, 'Not yet submitted')
    quality = Check('carrier', 'Carrier', 'missing', 'app_quality_rule', 'Our caution', [],
                    'add_document', False, 'Add the carrier')
    checks = [c for c in checks if c.id != 'carrier'] + [request, quality]
    result = classify(checks, rules, docs, context)
    assert result.state == 'meets_rule'
    assert request in result.requests and quality in result.cautions


@pytest.mark.parametrize('country', ['MX', 'US'])
def test_empty_checklist_and_unsupported_claim_never_meet(country):
    checks, rules, docs, context = complete(country)
    assert classify([], rules).state == 'cannot_meet_rule'
    assert classify(checks, rules, docs, context, unsupported=True).state == 'cannot_meet_rule'


def test_partial_refuses_unsupported_assertion_and_conflicting_quality():
    case = loader.load_case('c05_in_transit')
    rules, docs, ctx = load_rules(), loader.build_docs(case), loader.build_context(case)
    checks = evaluate(ctx, docs, rules, today=loader.today_of(case))
    assert classify(checks, rules, docs, ctx, unsupported=True).state == 'cannot_meet_rule'
    conflict = Check('dates_coherent', 'Dates', 'conflict', 'app_quality_rule', 'Our caution', [],
                     'review_difference', False, 'Review the dates')
    assert classify(checks + [conflict], rules, docs, ctx).state == 'cannot_meet_rule'


class Fake:
    def __init__(self, currency='MXN'):
        self.calls = []
        self.raw = {'dispute_id': 'PP-R-DEMO-0000001', 'status': 'WAITING_FOR_SELLER_RESPONSE',
                    'reason': 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'dispute_life_cycle_stage': 'INQUIRY',
                    'dispute_amount': {'currency_code': currency, 'value': '25.00'},
                    'create_time': '2026-09-25T16:00:00Z', 'evidences': [], 'links': []}

    def call(self, method, path, op, **kwargs):
        assert method == 'GET'
        self.calls.append(path)
        return self.raw if '/customer/disputes/' in path else {}


@pytest.mark.parametrize('currency', ['USD', 'MXN'])
def test_dedicated_page_only_and_exact_existing_economics(monkeypatch, currency, web_mode):
    monkeypatch.setenv('COPILOT_FEATURE_TRIAGE', 'true')
    monkeypatch.delenv('COPILOT_FEATURE_ACCOUNT_CLIFF', raising=False)
    fake = Fake(currency)
    repo.insert_package(config.db_path(), fake.raw['dispute_id'], 'human_edit', True, [])
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=('127.0.0.1', 12345))
    response = web.get('/disputes/PP-R-DEMO-0000001/triage')
    assert response.status_code == 200
    expected = economics_line(2500, currency, 1500, 'standard fee assumed (account cliff feature off)',
                              None, False, fee_applies_to_concede=False)
    assert response.context['economics'] == expected
    assert 'data-state="cannot_meet_rule"' in response.text
    if currency == 'MXN':
        assert 'Economics not computed' in response.text and 'Concede: refund' not in response.text
    for term in ('reliable', 'likely', 'probability', '%'):
        assert term not in response.context['verdict'].text.lower()
    monkeypatch.delenv('COPILOT_FEATURE_TRIAGE')
    page = web.get('/disputes/PP-R-DEMO-0000001/workspace/other')      # v3: the entry sits in the "Other responses" view
    assert page.status_code == 200
    source = templates.env.loader.get_source(templates.env, 'case/_other_responses.html')[0]
    entry = re.search(r'\{%- if can_respond_more and feature_enabled\("triage"\) and not lead_triage %\}\s*<li><a href="/disputes/\{\{ norm.dispute_id \}\}/triage">What the evidence supports</a></li>\s*\{%- endif %\}\s*', source).group(0)
    baseline = templates.env.overlay(loader=ChoiceLoader([DictLoader({'case/_other_responses.html': source.replace(entry, '')}),
                                                          templates.env.loader]))
    assert baseline.get_template('case_task.html').render(page.context) == page.text
    before = len(fake.calls)
    monkeypatch.setenv('COPILOT_FEATURE_TRIAGE', 'true')
    on = web.get('/disputes/PP-R-DEMO-0000001/workspace/other')
    assert len(fake.calls) - before == 1  # the core GET, never a triage/context/economics read
    assert 'What the evidence supports' in on.text and 'Check whether the evidence meets the delivery rule' not in on.text


def test_latest_package_unsupported_statement_prevents_positive_advice(monkeypatch):
    from copilot.features.triage import service
    checks, rules, docs, context = complete('MX')
    fake = Fake()
    did, db = fake.raw['dispute_id'], config.db_path()
    old = repo.insert_package(db, did, 'human_edit', True, [])
    repo.insert_claims(db, old, [{'evidence_index': 0, 'claim_index': 0, 'text': 'Shipment record.',
                                'source_ids': [], 'basis': 'seller_statement'}])
    new = repo.insert_package(db, did, 'human_edit', True, [])
    repo.insert_claims(db, new, [{'evidence_index': 0, 'claim_index': 0, 'text': 'The package was delivered.',
                                'source_ids': [], 'basis': 'seller_statement'}])
    monkeypatch.setattr(service, 'fetch_context', lambda *args: context)
    monkeypatch.setattr(service, 'extracted_docs_for', lambda *args: docs)
    monkeypatch.setattr(service, 'cached_rules', lambda: rules)
    monkeypatch.setattr(service, 'docs_for_dispute', lambda *args: ({}, {}))
    result, *_ = read(fake, db, did)
    assert result.state == 'cannot_meet_rule'



def test_resolution_public_economics_preserves_contract(monkeypatch):
    from copilot.features.resolution import service as resolution
    norm = {"amount_cents": 2500, "currency": "MXN", "stage": "CLAIM"}
    monkeypatch.setattr(resolution, "_fee", lambda client, value: (1500, "DEMO fee", False))
    expected = economics_line(2500, "MXN", 1500, "DEMO fee", None,
                              resolution.BUYER_CLOSES_FEE_KNOWN,
                              fee_applies_to_concede=True, eligibility_code=None, high_volume=False)
    assert resolution.dispute_economics(object(), norm, None) == expected
    assert resolution.dispute_economics(object(), {"amount_cents": None, "currency": "MXN"}, None) is None
    assert resolution.dispute_economics(object(), {"amount_cents": 2500, "currency": ""}, None) is None
    assert norm == {"amount_cents": 2500, "currency": "MXN", "stage": "CLAIM"}


@pytest.mark.parametrize("country",["MX","US"])
def test_dr1b_b7_cannot_meet_points_to_resolution_without_permission(country):
    checks,rules,docs,context=complete(country)
    checks=[replace(c,state="missing") if c.id=="tracking_number" else c for c in checks]
    result=classify(checks,rules,docs,context)
    assert result.state == "cannot_meet_rule"
    assert result.recommendation == ("Your documents do not show delivery under this rule. Add or correct the proof listed "
                                     "below, or make an offer or message the buyer.")
    assert "does not send offers" not in result.recommendation


def test_costs_are_a_list_and_no_iso_date_is_shown_outside_technical_details(monkeypatch, web_mode):
    """The long cost line is one list item per part; ISO dates in generated sentences read as words."""
    from _dom import parse
    monkeypatch.setenv('COPILOT_FEATURE_TRIAGE', 'true')
    monkeypatch.setenv('COPILOT_FEATURE_CLAUSES', 'true')
    monkeypatch.delenv('COPILOT_FEATURE_ACCOUNT_CLIFF', raising=False)
    fake = Fake('USD')
    repo.insert_package(config.db_path(), fake.raw['dispute_id'], 'human_edit', True, [])
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=('127.0.0.1', 12345))
    page = web.get('/disputes/PP-R-DEMO-0000001/triage').text
    dom = parse(page)
    costs = dom.find('ul', data_state='economics')
    parts = [li.text() for li in costs.find_all('li')]
    assert len(parts) >= 4 and parts[0].startswith('Concede: refund') and parts[1].startswith('Fight: fee')
    assert all(' · ' not in part for part in parts)
    technical = next((node for node in dom.find_all('details')
                      if node.find('summary') and node.find('summary').text() == 'Technical details'), None)
    assert technical is not None and technical.find_all('details')  # clause disclosures are nested in technical details
    iso_date = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')
    assert iso_date.search(_visible_text_outside_details(dom)) is None

    # Guard the extractor itself: nested hidden citation dates stay excluded; visible page text stays detectable.
    nested = parse('<main><p>Reading copy.</p><details><summary>Technical details</summary>'
                   '<details><summary>Citation</summary><p>2025-01-13</p></details></details></main>')
    visible = parse('<main><p>Visible 2025-01-13 date.</p><details><p>Hidden 2025-01-14 date.</p></details></main>')
    assert iso_date.search(_visible_text_outside_details(nested)) is None
    assert iso_date.search(_visible_text_outside_details(visible))


INVITATION = re.compile(r"\b(add|upload|reply|type|choose the carrier|check it)\b", re.I)
CASES_WITH_AN_INVITATION = ('c02_missing_carrier_proof', 'c05_in_transit', 'c06_illegible')


def _triage_page(monkeypatch, status, stage, case_name='c02_missing_carrier_proof'):
    from copilot.features.triage import service
    monkeypatch.setenv('COPILOT_FEATURE_TRIAGE', 'true')
    monkeypatch.setenv('COPILOT_FEATURE_OUTCOME', 'true')
    monkeypatch.delenv('COPILOT_FEATURE_ACCOUNT_CLIFF', raising=False)
    case = loader.load_case(case_name)
    context, docs, rules = loader.build_context(case), loader.build_docs(case), load_rules(country='MX')
    monkeypatch.setattr(service, 'fetch_context', lambda *args: context)
    monkeypatch.setattr(service, 'extracted_docs_for', lambda *args: docs)
    monkeypatch.setattr(service, 'cached_rules', lambda: rules)
    monkeypatch.setattr(service, 'docs_for_dispute', lambda *args: ({}, {}))
    fake = Fake('USD')
    fake.raw['status'], fake.raw['dispute_life_cycle_stage'] = status, stage
    repo.insert_package(config.db_path(), fake.raw['dispute_id'], 'human_edit', True, [])
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=('127.0.0.1', 12345))
    response = web.get('/disputes/PP-R-DEMO-0000001/triage')
    assert response.status_code == 200
    return response.text


def _body_text(page):
    """The reading text of the triage section: no technical details, tags removed."""
    import html
    section = page.split('<section id="triage"', 1)[1].split('</section>', 1)[0]
    section = re.sub(r'<details.*?</details>', ' ', section, flags=re.S)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', section)).split())


@pytest.mark.parametrize('case_name', CASES_WITH_AN_INVITATION)
def test_a_claim_under_review_lists_the_missing_checks_without_inviting_proof(monkeypatch, web_mode, case_name):
    page = _triage_page(monkeypatch, 'UNDER_REVIEW', 'CHARGEBACK', case_name)
    assert 'Add proof' not in page
    body = _body_text(page)
    assert REVIEWING_NEUTRAL_TEXT in body
    assert not INVITATION.findall(body), INVITATION.findall(body)


def test_a_claim_still_open_keeps_the_messages_and_the_add_proof_buttons(monkeypatch, web_mode):
    page = _triage_page(monkeypatch, 'WAITING_FOR_SELLER_RESPONSE', 'INQUIRY')
    assert 'Add proof' in page and 'not checked further' not in page
    assert "Add the carrier's proof or type the number." in page.replace('&#39;', "'")
    page = _triage_page(monkeypatch, 'WAITING_FOR_SELLER_RESPONSE', 'INQUIRY', 'c05_in_transit')
    assert 'You can still reply honestly with the shipping proof' in page
