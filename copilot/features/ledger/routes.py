"""Read-only accountability page plus the same deterministic JSON export."""
from fastapi import APIRouter, Request
from fastapi.responses import Response
import sqlite3

from copilot import config
from copilot.features.ledger import service
from copilot.web.routes_disputes import refusal_page, templates
from copilot.web.security import rejected_dispute_id

router = APIRouter()


def _read(dispute_id):
    return service.read(config.db_path(), dispute_id, service.configured_evidence())


@router.get('/disputes/{dispute_id}/ledger')
def page(request: Request, dispute_id: str):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    try:
        data = _read(dispute_id)
    except (ValueError, OSError, sqlite3.DatabaseError):
        return refusal_page(request, 400, 'Record unavailable', 'The configured local record cannot be read safely.')
    return templates.TemplateResponse(request, 'features/ledger/review.html', {'dispute_id': dispute_id, 'data': data})


@router.get('/disputes/{dispute_id}/ledger.json')
def json_export(request: Request, dispute_id: str):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    try:
        body, sha = service.export(_read(dispute_id))
    except (ValueError, OSError, sqlite3.DatabaseError):
        return refusal_page(request, 400, 'Export unavailable', 'A safe local data location is required for this export.')
    return Response(body, media_type='application/json', headers={'X-Ledger-SHA256': sha,
                    'Content-Disposition': f'attachment; filename="ledger-{sha}.json"'})
