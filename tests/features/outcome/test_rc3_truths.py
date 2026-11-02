from html import unescape
"""Synthetic claim-under-review and buyer-closed views; fake reads, zero POST/model."""
import copy
import json
from pathlib import Path

import pytest

from _hosted import authenticated_client
from copilot.app import create_app
from copilot.web.routes_disputes import get_client

pytestmark = pytest.mark.usefixtures("web_mode")
NOTICE = "PayPal is reviewing this claim. Proofie offers no seller action while the review lasts."
RAW = json.loads((Path(__file__).parent / "rc3-chargeback-demo.json").read_text(encoding="utf-8"))
DID = RAW["dispute_id"]


class Fake:
    def __init__(self, raw=None):
        self.raw, self.calls = raw or RAW, []
    def call(self, method, path, op, **kwargs):
        assert method == "GET", "the synthetic review case must never POST"
        self.calls.append((method, path, op))
        if path == '/v1/customer/disputes' or path.startswith('/v1/customer/disputes?'):
            return {"items": [copy.deepcopy(self.raw)]}
        if path.startswith('/v1/customer/disputes/'):
            return copy.deepcopy(self.raw)
        if path.startswith('/v1/reporting/transactions'):
            return {"transaction_details": []}
        if path.startswith('/v1/shipping/trackers'):
            return {"trackers": []}
        raise AssertionError("unexpected synthetic read")


def client(monkeypatch, raw=None):
    for name in ('outcome', 'triage', 'claims', 'resolution', 'fulfilment'):
        monkeypatch.setenv('COPILOT_FEATURE_' + name.upper(), 'true')
    monkeypatch.setenv('COPILOT_FEATURE_OUTCOME_APPEAL', 'false')
    for lane in ('NEGOTIATION_DRAFTS', 'LOCAL_AUTOPILOT'):      # the lane's local forms are their own flags' concern; these tests pin the core pages
        monkeypatch.delenv('COPILOT_FEATURE_' + lane, raising=False)
    from copilot.features.draft import model
    monkeypatch.setattr(model, 'default_call', lambda *a, **k: pytest.fail('model not allowed for review views'))
    fake = Fake(raw)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return authenticated_client(app), fake


@pytest.mark.parametrize('suffix', ['', '/detail', '/resolution', '/outcome', '/triage'])
def test_claim_under_review_has_truth_notice_no_offered_form(monkeypatch, suffix):
    web, fake = client(monkeypatch)
    path = '/disputes' if suffix == '' else '/disputes/' + DID + ('' if suffix == '/detail' else suffix)
    response = web.get(path)
    assert response.status_code == 200
    from _dom import parse
    visible = parse(response.text).text() if suffix == '/detail' else response.text      # the detail page's panel is the notice
    assert (NOTICE in visible, '<form' not in response.text) == (True, True)
    assert suffix == '/detail' or ('CHARGEBACK' in response.text and 'UNDER_REVIEW' in response.text)
    assert all(method == 'GET' for method, _, _ in fake.calls)


@pytest.mark.parametrize('page', ['inbox', 'detail', 'outcome'])
def test_buyer_closed_outcome_is_already_distinguished(monkeypatch, page):
    raw = copy.deepcopy(RAW)
    raw.update(status='RESOLVED', dispute_life_cycle_stage='CLAIM',
               dispute_outcome={'outcome_code': 'CANCELED_BY_BUYER'})
    web, fake = client(monkeypatch, raw)
    path = '/disputes' if page == 'inbox' else '/disputes/' + DID + ('/outcome' if page == 'outcome' else '')
    response = web.get(path)
    assert response.status_code == 200 and 'Canceled by the buyer' in response.text
    assert NOTICE not in response.text and all(method == 'GET' for method, _, _ in fake.calls)


# Causal state coverage: fake GET only, actual templates, never rawmodel output.
@pytest.mark.parametrize("state", ["meets_rule", "partial", "cannot_meet_rule"])
def test_h3_review_triage_does_not_recommend_a_reply(monkeypatch, state):
    from types import SimpleNamespace
    from copilot.features.triage import service
    from copilot.core.checklist import Check
    if state == "meets_rule":
        rules = SimpleNamespace(checks={}, country="MX", effective_date="DEMO DATA")
        verdict = service.classify([], rules)
    else:
        rules = SimpleNamespace(checks={"delivery_status": {"basis": "published_policy", "kind": "mandatory", "label": "DEMO DATA", "citation": "DEMO DATA"}}, country="MX", effective_date="DEMO DATA")
        checks = [Check(cid, "DEMO DATA", status, "published_policy", "DEMO DATA", [], "none", False, "DEMO DATA")
                  for cid, status in [("tracking_number", "verified"), ("carrier", "verified"), ("delivery_status", "missing")]]
        verdict = service.classify(checks, rules, context={"tracking_status": "SHIPPED" if state == "partial" else ""})
    assert verdict.state == state
    monkeypatch.setattr(service, 'classify', lambda *a, **k: verdict)
    web, fake = client(monkeypatch)
    response = web.get('/disputes/' + DID + '/triage')
    assert response.status_code == 200 and NOTICE in response.text
    assert verdict.recommendation not in response.text
    assert verdict.text not in response.text
    assert "Missing context is not a refusal to send" not in response.text
    assert "Add or review proof" not in response.text
    assert '<form' not in response.text and all(m == 'GET' for m, _, _ in fake.calls)


def test_h3_review_outcome_keeps_history_but_offers_no_addable_actions(monkeypatch):
    web, fake = client(monkeypatch)
    page = web.get('/disputes/' + DID + '/outcome')
    assert page.status_code == 200 and NOTICE in page.text
    assert 'An offer or a message to the buyer' not in page.text
    assert 'A tracker registered before the dispute' not in page.text
    assert 'data-state="addable"' not in page.text
    assert 'data-state="gaps"' in page.text and 'When it was sent' in page.text
    assert "Evidence checks made by Proofie" in page.text
    assert all(m == 'GET' for m, _, _ in fake.calls)


def test_h3_review_detail_does_not_offer_resolution_or_draft_entry(monkeypatch):
    monkeypatch.setenv('COPILOT_FEATURE_DRAFT', 'true')
    web, fake = client(monkeypatch)
    page = web.get('/disputes/' + DID)
    from _dom import parse
    assert page.status_code == 200 and NOTICE in parse(page.text).text()
    assert 'Resolution options and buyer messages' not in page.text
    assert 'Draft a message to the buyer' not in page.text
    assert all(m == 'GET' for m, _, _ in fake.calls)


def test_h3_direct_draft_is_truthful_about_an_existing_dispute(monkeypatch):
    monkeypatch.setenv('COPILOT_FEATURE_DRAFT', 'true')
    web, fake = client(monkeypatch)
    page = web.get('/disputes/' + DID + '/draft')
    assert page.status_code == 200
    assert 'before a dispute exists' not in page.text
    assert 'PayPal offers no send-message option for this dispute. You can copy this text and send it to the buyer yourself; PayPal\'s record will not show it.' in unescape(page.text)
    assert all(m == 'GET' for m, _, _ in fake.calls)


def test_h3_buyer_closed_detail_summary_matches_the_chip(monkeypatch):
    raw = copy.deepcopy(RAW)
    raw.update(status='RESOLVED', dispute_life_cycle_stage='CLAIM', dispute_outcome={'outcome_code': 'CANCELED_BY_BUYER'})
    web, fake = client(monkeypatch, raw)
    page = web.get('/disputes/' + DID)
    assert page.status_code == 200
    label = 'Canceled by the buyer (as stated by PayPal)'
    from _dom import parse
    strip = parse(page.text).find('section', data_state='case-strip').text()
    assert label in strip and 'Resolved' not in strip and 'PayPal says the dispute is' not in page.text
    assert all(m == 'GET' for m, _, _ in fake.calls)


@pytest.mark.parametrize("surface", ["inbox", "detail", "triage", "resolution", "outcome"])
def test_h3_copyround_exact_review_wording_and_visibility(monkeypatch, surface):
    import re
    web, fake = client(monkeypatch)
    path = '/disputes' if surface == 'inbox' else '/disputes/' + DID + ('' if surface == 'detail' else '/' + surface)
    page = web.get(path)
    assert page.status_code == 200
    if surface == 'inbox':
        cell = re.search(r'<td data-label="Next step"[^>]*>(.*?)</td>', page.text, re.S).group(1)
        assert 'PayPal is reviewing this claim' in cell
        assert 'PayPal is not waiting for you' not in cell
    elif surface == 'detail':
        assert 'not currently asking for evidence' not in page.text
        assert 'data-state="no-action-available"' not in page.text
    elif surface == 'triage':
        assert 'Missing context is not a refusal to send' not in page.text
        assert 'Add or review proof' not in page.text
    elif surface == 'outcome':
        assert '<h1>What PayPal has recorded:' in page.text
        assert '<h1>What PayPal decided:' not in page.text
        assert 'Evidence checks made by Proofie' in page.text
    from _dom import parse
    assert NOTICE in (parse(page.text).text() if surface == 'detail' else page.text)
    assert page.text.count('data-state="claim-under-review"') == 1
    assert '<form' not in page.text and all(m == 'GET' for m, _, _ in fake.calls)


def test_h3_copyround_buyer_closed_resolution_matches_existing_label(monkeypatch):
    raw = copy.deepcopy(RAW)
    raw.update(status='RESOLVED', dispute_life_cycle_stage='CLAIM', dispute_outcome={'outcome_code': 'CANCELED_BY_BUYER'})
    web, fake = client(monkeypatch, raw)
    page = web.get('/disputes/' + DID + '/resolution')
    assert page.status_code == 200
    assert '<span data-status="RESOLVED">Canceled by the buyer (as stated by PayPal)</span>' in page.text
    assert '<span data-status="RESOLVED">Resolved</span>' not in page.text
    assert all(m == 'GET' for m, _, _ in fake.calls)


# Additive namespaced regression set; the complete set above is preserved.
"""16cq buyer-closed display regression; fake reads only, merged with16cn tests after rebase."""
import copy
import json
from pathlib import Path
import re

import pytest
from _hosted import authenticated_client
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web.routes_disputes import get_client
from outcome_fakes import FakePayPal, DISPUTE_ID, dispute_raw

pytestmark = pytest.mark.usefixtures("web_mode")
POLISH_CLOSED = "Canceled by the buyer (as stated by PayPal)"
POLISH_NOTICE = "The buyer closed this case. PayPal offers no seller action, so there is nothing to do."


class PolishReadsOnly(FakePayPal):
    def call(self, method, path, op, **kw):
        assert method == 'GET', 'no PayPal write in display regression'
        if path == '/v1/customer/disputes' or path.startswith('/v1/customer/disputes?'):
            self.calls.append((method, path, op, kw))
            return {'items': [copy.deepcopy(self.raw)]}
        return super().call(method, path, op, **kw)


def polish_web_for(monkeypatch, code='CANCELED_BY_BUYER', enabled=True, shape='can_provide'):
    for name in ['outcome','draft','resolution']:
        monkeypatch.setenv('COPILOT_FEATURE_' + name.upper(), 'true' if name != 'outcome' or enabled else 'false')
    monkeypatch.setenv('COPILOT_FEATURE_OUTCOME_APPEAL', 'false')
    for lane in ('NEGOTIATION_DRAFTS', 'LOCAL_AUTOPILOT'):      # the lane's local forms are their own flags' concern; these tests pin the core pages
        monkeypatch.delenv('COPILOT_FEATURE_' + lane, raising=False)
    from copilot.features.draft import model
    monkeypatch.setattr(model, 'default_call', lambda *a, **k: pytest.fail('No model allowed'))
    raw = json.loads(Path(__file__).with_name('rc3-chargeback-demo.json').read_text(encoding='utf-8')) if shape == 'seed' else dispute_raw(status='RESOLVED', stage='CLAIM', rels=('provide_evidence','send_message'), outcome={'outcome_code':code} if code else None)
    if shape == 'seed':raw.update(status='RESOLVED',dispute_life_cycle_stage='CLAIM',dispute_outcome={'outcome_code':code})
    fake = PolishReadsOnly(raw)
    app = create_app();app.dependency_overrides[get_client] = lambda:fake
    return authenticated_client(app), fake


@pytest.mark.parametrize('shape',['can_provide','seed'])
@pytest.mark.parametrize('surface', ['detail','outcome','inbox'])
def test_polish_buyer_closed_display_is_readonly_and_uniform(monkeypatch, surface, shape):
    web, fake = polish_web_for(monkeypatch,shape=shape)
    did = fake.raw['dispute_id']
    doc = repo.insert_document(config.db_path(),did,'DEMO DATA.pdf','application/pdf',0,'d'*64,'DEMO DATA',True)
    path='/disputes' if surface=='inbox' else '/disputes/'+did+('' if surface=='detail' else '/outcome')
    response=web.get(path); assert response.status_code==200
    assert POLISH_CLOSED in response.text
    if surface=='detail':
        from _dom import parse
        shown = parse(response.text).text()
        assert POLISH_NOTICE in shown and shown.count(POLISH_NOTICE)==1
        assert '<form' not in response.text
        assert 'data-state="release-feature-links"' not in response.text
        assert 'data-state="no-action-available"' not in response.text
    if surface=='outcome':
        assert 'Decided:' not in response.text
        assert '<title>What PayPal has recorded:' in response.text and '<h1>What PayPal has recorded:' in response.text
        assert 'data-state="gaps"' not in response.text
        for rel in ('provide-evidence','send-message') if shape == 'can_provide' else ('accept-claim','provide-supporting-info'):
            assert rel in response.text
    assert not fake.posts() and repo.get_document(config.db_path(),doc)['excluded'] is False


def test_polish_outcome_flag_off_retains_original_detail_actions(monkeypatch):
    web,fake=polish_web_for(monkeypatch,enabled=False)
    page=web.get('/disputes/'+DISPUTE_ID)
    prepare=web.get('/disputes/'+DISPUTE_ID+'/workspace/prepare')      # v3: the upload form is in Prepare, the optional links in Other responses
    other=web.get('/disputes/'+DISPUTE_ID+'/workspace/other')
    assert page.status_code==200 and prepare.status_code==200 and other.status_code==200 and '<form' in prepare.text
    assert POLISH_NOTICE not in page.text and 'data-state="release-feature-links"' in other.text
    assert not fake.posts()


@pytest.mark.parametrize('code,expected', [(None,'What PayPal has recorded'),('RESOLVED_BUYER_FAVOUR','What PayPal decided')])
def test_polish_outcome_title_and_h1_match_decision_branch(monkeypatch,code,expected):
    web,fake=polish_web_for(monkeypatch,code=code)
    if code is None:fake.raw.update(status='UNDER_REVIEW',dispute_life_cycle_stage='CHARGEBACK')
    page=web.get('/disputes/'+DISPUTE_ID+'/outcome')
    assert page.status_code==200
    assert '<title>'+expected+':' in page.text and '<h1>'+expected+':' in page.text
    assert not fake.posts()


def test_the_resolution_page_renders_the_shared_buyer_closed_label(monkeypatch):
    from copilot.web import dispute_state
    from copilot.web.routes_disputes import templates
    assert templates.env.globals["BUYER_CLOSED_LABEL"] == dispute_state.BUYER_CLOSED_LABEL
    source = templates.env.loader.get_source(templates.env, 'features/resolution/resolution.html')[0]
    assert dispute_state.BUYER_CLOSED_LABEL not in source
    raw = copy.deepcopy(RAW)
    raw.update(status='RESOLVED', dispute_life_cycle_stage='CLAIM', dispute_outcome={'outcome_code': 'CANCELED_BY_BUYER'})
    web, _fake = client(monkeypatch, raw)
    monkeypatch.setitem(templates.env.globals, "BUYER_CLOSED_LABEL", "Sentinel closed label")
    page = web.get('/disputes/' + DID + '/resolution')
    assert page.status_code == 200
    assert '<span data-status="RESOLVED">Sentinel closed label</span>' in page.text


def test_the_inbox_status_cell_says_the_review_once_and_counts_pages_in_plain_words(monkeypatch):
    web, _fake = client(monkeypatch)
    page = web.get('/disputes')
    assert page.status_code == 200
    from _dom import parse
    cell = parse(page.text).find('td', data_label='Status').text()
    assert cell.count('PayPal is reviewing this claim') == 1
    assert '1 page read.' in page.text and 'page(s)' not in page.text
