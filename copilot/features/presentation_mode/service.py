"""Build links to existing local case flows; never reads PayPal or scores an evaluation."""
import re


def moments_for_case(case_id: str, package_id: str, enabled: dict | None = None) -> list[dict]:
    enabled = enabled or {}
    safe_case = case_id if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case_id or "") else ""
    safe_package = package_id if re.fullmatch(r"[1-9][0-9]{0,9}", package_id or "") else ""
    detail = f"/disputes/{safe_case}" if safe_case else "/disputes"
    package = f"{detail}/packages/{safe_package}" if safe_case and safe_package else detail
    evaluation_enabled = bool(enabled.get("evaluation_workbench"))
    evaluation_title = "Local model evaluation and retained failures"
    evaluation_subtitle = "This optional case-bound manifest is disabled; no evaluation is run here."
    if evaluation_enabled and safe_case and safe_package:
        # Read an already-recorded historical result only. This never invokes an evaluator or model.
        from copilot.features.evaluation_workbench.service import HISTORIC
        match = re.fullmatch(r"(\d+)/(\d+)", HISTORIC.get("result", ""))
        if match and HISTORIC.get("status", "").startswith("historic"):
            passed, total = map(int, match.groups())
            if 0 <= passed <= total:
                failures = total - passed
                evaluation_title = f"Historic local-model result: {failures} failures ({passed}/{total})"
                evaluation_subtitle = (f"{HISTORIC.get('name')}: {passed}/{total}; {failures} did not pass. "
                                       f"Source SHA {HISTORIC.get('source_sha')}; historical, not this checkout. "
                                       "The current package manifest remains NOTRUN.")
    return [
        {"title": "Recorded package and its existing evidence hash", "subtitle": "Open the existing case package and hash view; no new evidence is created.", "href": package + "/print" if enabled.get("package_print") else "/disputes", "kind": "live-flow", "available": bool(enabled.get("package_print"))},
        {"title": "Re-read the case from PayPal", "subtitle": "Open the case view; current status remains PayPal-sourced.", "href": detail, "kind": "live-flow"},
        {"title": "Review the case package", "subtitle": "Open the saved package and its existing evidence; this guide does not evaluate the case.", "href": package, "kind": "live-flow"},
        {"title": evaluation_title, "subtitle": evaluation_subtitle,
         "href": package + "#evaluation-manifest" if evaluation_enabled and safe_case and safe_package else detail,
         "kind": "live-flow", "available": bool(evaluation_enabled and safe_case and safe_package)},
    ]


def progression_steps(case_id: str, package_id: str, enabled: dict | None = None) -> list[dict]:
    """Four case-bound live destinations followed by a clearly separate simulation."""
    moments = moments_for_case(case_id, package_id, enabled)
    return [*moments, {"title": "Fifth moment: simulation only", "href": "/features/local_autopilot",
                       "subtitle": "Simulation only; not an action or PayPal outcome. It does not change the case.",
                       "kind": "simulation", "available": bool((enabled or {}).get("local_autopilot"))}]


def review_snapshot(db_path, uploads_dir, case_id, package_id):
    """Project a same-case latest package and local source bytes for human review.

    This is an observation, not a lock, approval, authenticity check, or send guard.
    """
    import hashlib
    import json
    from copilot.core.raw_storage import read_raw
    from copilot.db import repo
    from copilot.web.security import validate_dispute_id
    if validate_dispute_id(case_id) or type(package_id) is not int or not 1 <= package_id <= 9999999999:
        raise ValueError('Invalid case or package reference.')
    package = repo.get_package(db_path, package_id)
    if not package or package.get('dispute_id') != case_id:
        raise ValueError('Package is not available for this case.')
    packages = repo.list_packages(db_path, case_id)
    latest = max(packages, key=lambda row: (row['version'], row['id'])) if packages else None
    claims = repo.list_claims(db_path, package_id)
    docs = {f"doc_{row['id']}": row for row in repo.list_documents(db_path, case_id)}
    sources = []
    refs = sorted({ref for claim in claims for ref in (claim.get('source_ids') or []) if isinstance(ref, str)})
    for ref in refs:
        row = docs.get(ref)
        if row is None:
            sources.append({'id': ref, 'name': 'Missing case source', 'status': 'unavailable', 'sha256': None})
            continue
        digest = row.get('sha256')
        try:
            data = read_raw(uploads_dir, digest) if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest) else None
        except OSError:
            data = None
        actual = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None
        state = 'excluded' if row.get('excluded') else ('matched' if actual and actual == digest else 'unavailable or changed')
        sources.append({'id': ref, 'name': row.get('filename') or ref, 'status': state, 'sha256': actual})
    attachments = []
    for attachment in repo.list_attachments(db_path, package_id):
        digest = attachment.get('sha256')
        try:
            data = read_raw(uploads_dir, digest) if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest) else None
        except OSError:
            data = None
        actual = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None
        attachments.append({'id': attachment['document_id'], 'name': attachment.get('filename') or 'Attachment',
                            'sha256': actual, 'status': 'matched' if actual and actual == digest else 'unavailable or changed'})
    approval = repo.get_latest_valid_approval(db_path, package_id)
    payload = {'case_id': case_id, 'package_id': package_id, 'version': package['version'],
               'latest_package_id': latest['id'] if latest else None,
               'claims': [{'text': row.get('text'), 'basis': row.get('basis'), 'source_ids': row.get('source_ids') or []} for row in claims],
               'sources': sources,
               'attachments': attachments,
               'approval_id': approval['id'] if approval else None}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'case_id': case_id, 'package_id': package_id, 'version': package['version'],
            'latest': bool(latest and latest['id'] == package_id), 'sources': sources, 'attachments': attachments,
            'claim_count': len(claims), 'approval_recorded': bool(approval), 'snapshot': fingerprint,
            'review_url': f'/disputes/{case_id}/packages/{package_id}',
            'next_action': 'Review the package and its sources in Proofie; a person decides whether to approve.',
            'limitation': 'Local package and source bytes only: PayPal status and available actions were not refreshed. This does not authorize an action, prove authenticity, or lock later changes.'}


def compare_snapshot(db_path, uploads_dir, case_id, package_id, snapshot):
    """Compare an untrusted token; this read cannot authorize a subsequent write."""
    import hmac
    if not isinstance(snapshot, str) or not re.fullmatch(r'[0-9a-f]{64}', snapshot):
        return {'status': 'invalid', 'reason': 'Invalid snapshot; open the case and review again.', 'review_url': '/disputes'}
    try:
        current = review_snapshot(db_path, uploads_dir, case_id, package_id)
    except (ValueError, OSError):
        return {'status': 'unavailable', 'reason': 'Package is unavailable for this case.', 'review_url': '/disputes'}
    if not current['latest'] or not hmac.compare_digest(snapshot, current['snapshot']):
        return {'status': 'stale', 'reason': 'Package or source changed; review the current case before continuing.',
                'review_url': f'/disputes/{case_id}/workspace/review'}
    if any(source['status'] != 'matched' for source in current['sources'] + current['attachments']):
        return {'status': 'unavailable', 'reason': 'A referenced source is missing, excluded or changed; review it first.',
                'review_url': current['review_url']}
    return {'status': 'current', 'case_id': case_id, 'package_id': package_id, 'version': current['version'],
            'source_ids': [source['id'] for source in current['sources']], 'next_action': current['next_action'],
            'review_url': current['review_url'], 'limitation': current['limitation']}
