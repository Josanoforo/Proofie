"""Local simulation UI plus a case-bound, read-only habitual-flow adapter."""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from copilot import config
from copilot.core.disputes import normalize
from copilot.db import repo
from copilot.features.local_autopilot import service
from copilot.web.routes_disputes import get_client, templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
if config.feature_enabled('local_autopilot') and config.feature_enabled('local_preparation'):
    from copilot.features.local_autopilot.preparation_routes import router as preparation_router
    router.include_router(preparation_router)
STORE_PATH = Path(__file__).resolve().parents[3] / "data" / "mejoras10" / "09" / "simulation.json"

def _store_path():
    if config.data_dir() == config.DATA_DIR:
        return STORE_PATH
    return config.data_dir() / "mejoras10" / "09" / "simulation.json"


def _evidence(dispute_id):
    rows = repo.list_documents(config.db_path(), dispute_id)
    return [{"id": f"doc_{row['id']}", "source": f"Proofie document {row['id']} ({row['filename']}); seller-supplied record",
             "content": row["extracted_text"]}
            for row in rows if not row["excluded"] and row["extraction_ok"]
            and isinstance(row.get("extracted_text"), str) and row["extracted_text"].strip()]


def _allowed_actions(norm):
    from copilot.features.resolution.order import allowed_actions
    links = allowed_actions(norm)
    return [action for action, enabled in (("offer", links.offer), ("message", links.message)) if enabled]


def handoff_for_case(dispute_id, logical_id, norm, allowed_actions):
    """Return a persisted successful result only when a pure evaluation still matches this current case view."""
    if not config.feature_enabled("local_autopilot") or not isinstance(norm, dict):
        return None
    rows = service.load_inbox(_store_path())
    stored = next((row for row in reversed(rows) if row.get("logical_id") == logical_id
                   and row.get("case_id") == dispute_id), None)
    if not stored or stored.get("decision") != "simulated_action":
        return None
    profile = stored.get("profile")
    policy = service.PROFILES.get(profile)
    deadline = norm.get("seller_response_due_date") if norm.get("deadline_parse_ok") else None
    if not policy or stored.get("action") not in {"offer", "message"} or not deadline:
        return None
    candidate = service.evaluate({
        "profile": profile, "case_id": dispute_id, "action": stored["action"],
        "amount_cents": stored.get("amount_cents"), "currency": norm.get("currency"),
        "dispute_amount_cents": norm.get("amount_cents"), "dispute_currency": norm.get("currency"),
        "policy_version": stored.get("policy_version"), "policy": policy,
        "case_state": norm.get("status"), "state_version": norm.get("update_time"),
        "observed_at": datetime.now(timezone.utc).isoformat(), "deadline": deadline,
        "recorded_allowed_actions": sorted(set(allowed_actions)), "evidence": _evidence(dispute_id),
        "conflict": False,
    })
    if candidate["decision"] != "simulated_action" or candidate["logical_id"] != logical_id:
        return None
    amount = stored["amount_cents"]
    return {"logical_id": logical_id, "action": stored["action"], "amount_cents": amount,
            "currency": stored["currency"], "policy_version": stored["policy_version"],
            "policy_sha256": stored["policy_sha256"], "state_sha256": stored["state_sha256"],
            "dossier_sha256": stored["dossier_sha256"],
            "amount_text": f"{amount // 100}.{amount % 100:02d}"}


@router.get('/features/local_autopilot', response_class=HTMLResponse)
def page(request: Request):
    inbox = service.load_inbox(_store_path())
    token = new_csrf_token()
    response = templates.TemplateResponse(request, 'features/local_autopilot/index.html',
        {'raw': json.dumps(service.EXAMPLES, indent=2), 'result': None, 'inbox': inbox, 'error': None, 'csrf': token})
    set_csrf_cookie(response, token)
    return response


@router.post('/features/local_autopilot', response_class=HTMLResponse)
def run(request: Request, data: str = Form(...), csrf_token: str = Form(default='')):
    if not verify_csrf(request, csrf_token):
        return Response('Reload this form.', 403)
    inbox = []
    try:
        rows = json.loads(data)
        if not isinstance(rows, list) or len(rows) > 50:
            raise ValueError('Use a list up to 50 policies')
        result = [service.simulate(row, _store_path()) for row in rows]
        inbox = service.load_inbox(_store_path())
        error = None
    except Exception as exc:
        result, error = None, str(exc)[:500]
    token = new_csrf_token()
    response = templates.TemplateResponse(request, 'features/local_autopilot/index.html',
        {'raw': data[:30000], 'result': result, 'inbox': inbox, 'error': error, 'csrf': token},
        status_code=400 if error else 200)
    set_csrf_cookie(response, token)
    return response


@router.post('/disputes/{dispute_id}/local-autopilot/simulate')
def simulate_case(request: Request, dispute_id: str, csrf_token: str = Form(default=''),
                  profile: str = Form(default='standard'), action: str = Form(default=''),
                  amount: str = Form(default=''), return_to: str = Form(default=''), client=Depends(get_client)):
    if not verify_csrf(request, csrf_token):
        return Response('Reload the case page before preparing a simulation.', 403)
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    destination = f'/disputes/{dispute_id}/workspace/' + ('prepare' if return_to == 'prepare' else 'hackathon')
    try:
        raw = client.call('GET', f'/v1/customer/disputes/{dispute_id}', 'show_dispute')
        norm = normalize(raw, datetime.now(timezone.utc))
        allowed = _allowed_actions(norm)
        packages = repo.list_packages(config.db_path(), dispute_id)
        conflict = False
        if packages:
            from copilot.web.routes_package import _checks_for
            from copilot.core.checklist import UnknownCountryError
            try:
                checks, _context = _checks_for(norm, dispute_id, client, config.db_path())
                conflict = any(check.state == 'conflict' for check in checks)
            except UnknownCountryError:
                conflict = True
        try:
            amount = service.parse_amount_cents(amount)
        except ValueError:
            return RedirectResponse(f'{destination}?local_autopilot_error=amount#local-autopilot', status_code=303)
        if norm.get('currency') not in service.CENT_CURRENCIES:
            raise ValueError('This local cent-based policy does not support the recorded case currency.')
        evidence = _evidence(dispute_id)
        observed_at = datetime.now(timezone.utc).isoformat()
        deadline = norm.get('seller_response_due_date') if norm.get('deadline_parse_ok') else None
        result = service.simulate({
            'profile': profile, 'case_id': dispute_id, 'action': action, 'amount_cents': amount,
            'currency': norm.get('currency'), 'dispute_amount_cents': norm.get('amount_cents'),
            'dispute_currency': norm.get('currency'), 'policy_version': 'local-policy-v1',
            'policy': service.PROFILES.get(profile), 'case_state': norm.get('status'),
            'state_version': norm.get('update_time'), 'observed_at': observed_at, 'deadline': deadline,
            'recorded_allowed_actions': allowed, 'evidence': evidence, 'conflict': conflict,
        }, _store_path())
        suffix = '?local_autopilot_done=1' if return_to == 'prepare' else ''
        return RedirectResponse(f'{destination}{suffix}#local-autopilot', status_code=303)
    except (TypeError, ValueError, PermissionError, KeyError) as exc:
        return RedirectResponse(f'{destination}?local_autopilot_error=1#local-autopilot', status_code=303)


def _current_plan(dispute_id, client):
    from copilot.features.resolution.service import page_state
    from copilot.features.local_autopilot.assistance import case_context
    state = page_state(client, dispute_id)
    if not state.norm:
        raise ValueError("Current case is unavailable; return to Prepare and try again.")
    actions = [name for name, enabled in (("offer", state.actions.offer), ("message", state.actions.message)) if enabled]
    context = case_context(config.db_path(), state.norm, actions)
    plan = context.get("attention_plan")
    if not plan:
        raise ValueError("Current plan is unavailable; return to Prepare and try again.")
    return state, actions, context


def _plan_refusal(dispute_id, message):
    from fastapi.responses import HTMLResponse
    from html import escape
    return HTMLResponse(f'<p role="alert">{escape(message)}</p><p><a href="/disputes/{escape(dispute_id)}/workspace/prepare">Return to Prepare and refresh the plan</a></p>', status_code=409)


@router.post('/disputes/{dispute_id}/local-autopilot/plan-draft', response_class=HTMLResponse)
def plan_draft(request: Request, dispute_id: str, csrf_token: str = Form(default=''), fingerprint: str = Form(default=''),
               kind: str = Form(default=''), recipient: str = Form(default=''), amount: str = Form(default=''),
               conditions: str = Form(default=''), client=Depends(get_client)):
    if not verify_csrf(request, csrf_token): return Response('Reload this form.', 403)
    bad = rejected_dispute_id(dispute_id)
    if bad: return bad
    if not (config.feature_enabled('negotiation_drafts') and config.feature_enabled('resolution')):
        return Response('Draft preparation is unavailable.', 404)
    try:
        state, actions, context = _current_plan(dispute_id, client)
        if not fingerprint or fingerprint != context['attention_plan']['fingerprint']:
            raise ValueError('The case plan changed. Refresh it before preparing a draft.')
        from copilot.features.negotiation_drafts import service as drafts
        from copilot.features.resolution.routes import parse_amount_cents
        cents = parse_amount_cents(amount, state.norm.get('currency')) if kind == 'partial_offer' else 0
        if cents is None or (kind == 'partial_offer' and (type(state.norm.get('amount_cents')) is not int or cents > state.norm['amount_cents'])):
            raise ValueError('Enter an amount within the current disputed amount.')
        result = drafts.build_case_draft(state.norm, actions, kind=kind, locale='en', recipient=recipient,
                    amount_cents=cents, conditions=conditions, history_path=drafts.case_history_path(dispute_id), source_plan=fingerprint)
        token = new_csrf_token()
        response = templates.TemplateResponse(request, 'features/local_autopilot/plan_draft.html',
                    {'dispute_id': dispute_id, 'proposal': result['proposals'][0], 'csrf_token': token})
        set_csrf_cookie(response, token)
        return response
    except (OSError, ValueError, TypeError, KeyError):
        return _plan_refusal(dispute_id, 'The case plan or draft could not be verified. Refresh before trying again.')


@router.post('/disputes/{dispute_id}/local-autopilot/plan-preview')
def plan_preview(request: Request, dispute_id: str, csrf_token: str = Form(default=''), logical_id: str = Form(default=''), client=Depends(get_client)):
    if not verify_csrf(request, csrf_token): return Response('Reload this form.', 403)
    bad = rejected_dispute_id(dispute_id)
    if bad: return bad
    if not (config.feature_enabled('resolution') and config.feature_enabled('resolution_writes') and config.feature_enabled('negotiation_drafts')):
        return Response('Preview is unavailable.', 404)
    try:
        state, actions, context = _current_plan(dispute_id, client)
        from copilot.features.negotiation_drafts.service import read_case_history
        proposal = next((p for p in read_case_history(dispute_id) if p.get('logical_id') == logical_id), None)
        if not proposal or not proposal.get('source_plan') or proposal['source_plan'] != context['attention_plan']['fingerprint'] or proposal.get('action') not in actions:
            raise ValueError('The case plan has changed.')
        from copilot.features.resolution.routes import preview
        from copilot.features.draft.facts import money_text
        amount = money_text(proposal['amount_cents'], proposal['currency']).rsplit(' ', 1)[0] if proposal['action'] == 'offer' else ''
        return preview(request, dispute_id, csrf_token=csrf_token, action=proposal['action'], amount=amount, note=proposal['draft'] if proposal['action'] == 'offer' else '', text=proposal['draft'] if proposal['action'] == 'message' else '', client=client)
    except (OSError, ValueError, TypeError, KeyError):
        return _plan_refusal(dispute_id, 'This draft is stale or unavailable. Refresh the plan; the saved draft remains in history.')


def _hint_path(dispute_id):
    from copilot.web.security import validate_dispute_id
    if validate_dispute_id(dispute_id): raise ValueError('Invalid case.')
    root = config.data_dir() / 'local_autopilot' / 'attention'
    path = root / (dispute_id + '.json')
    if path.is_symlink(): raise ValueError('Invalid hint path.')
    return path


@router.post('/disputes/{dispute_id}/local-autopilot/model-review')
def model_review(request: Request, dispute_id: str, csrf_token: str = Form(default=''), fingerprint: str = Form(default=''), client=Depends(get_client)):
    if not verify_csrf(request, csrf_token): return Response('Reload this form.', 403)
    bad = rejected_dispute_id(dispute_id)
    if bad: return bad
    if not config.feature_enabled('attention_model'): return Response('Model assistance is disabled.', 404)
    destination = f'/disputes/{dispute_id}/workspace/prepare'
    try:
        state, actions, context = _current_plan(dispute_id, client)
        if fingerprint != context['attention_plan']['fingerprint']: raise ValueError('Stale plan')
        from copilot.features.local_autopilot import resources, model_adapter
        if not resources.resources_available():
            _hint_path(dispute_id).unlink(missing_ok=True)
            return RedirectResponse(destination + '?attention_model=busy#attention-plan', status_code=303)
        from copilot.features.local_autopilot.assistance import case_context
        assisted = case_context(config.db_path(), state.norm, actions, model_backend=model_adapter.existing_model)
        if assisted.get('attention_plan', {}).get('fingerprint') != fingerprint:
            raise ValueError('Plan changed')
        hint = assisted['attention_plan'].get('model_hint')
        if not hint:
            _hint_path(dispute_id).unlink(missing_ok=True)
            return RedirectResponse(destination + '?attention_model=unavailable#attention-plan', status_code=303)
        path = _hint_path(dispute_id); path.parent.mkdir(parents=True, exist_ok=True)
        import os, tempfile
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as out:
            json.dump({'fingerprint': fingerprint, 'source_ids': hint['source_ids']}, out)
            temporary = out.name
        os.replace(temporary, path)
        return RedirectResponse(destination + '?attention_model=ready#attention-plan', status_code=303)
    except (OSError, ValueError, TypeError, KeyError):
        return RedirectResponse(destination + '?attention_model=unavailable#attention-plan', status_code=303)
